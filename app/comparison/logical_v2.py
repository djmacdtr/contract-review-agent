"""Logical, candidate-oriented comparison for the opt-in FINAL_COMPARE mode.

The legacy comparator is intentionally left untouched.  This module keeps the
parser's physical locations, but compares merged cells and OCR spans through a
logical-cell view.  Ambiguous structure is retained as a review candidate; it
is never silently deleted by a heuristic.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from rapidfuzz.fuzz import ratio

from app.comparison.models import ComparisonResult, DiffItem, DiffSide
from app.comparison.reliable import (
    ComparableUnit,
    _make_diff,
    _numeric_changed,
    aggregate_warnings,
    build_diff_segments,
    compare_documents_reliably,
    comparison_normalize,
)
from app.documents.models import (
    DocumentBlock,
    DocumentLocation,
    ParsedDocument,
    ProcessingWarning,
    TableCell,
)


@dataclass
class LogicalCell:
    cell_id: str
    text: str
    row: int
    column: int
    locations: tuple[DocumentLocation, ...]
    row_span: int = 1
    col_span: int = 1


@dataclass
class LogicalTable:
    block: DocumentBlock
    rows: list[list[LogicalCell]]
    cells: list[LogicalCell] = field(default_factory=list)


_DIFF_PRIORITY = {
    "NUMERIC_CHANGED": 0,
    "PAGE_MISSING": 1,
    "CONTENT_BLOCK_MISSING": 1,
    "TABLE_ROW_ADDED": 2,
    "TABLE_ROW_DELETED": 2,
    "TABLE_CELL_CHANGED": 3,
    "MODIFIED": 4,
    "ADDED": 5,
    "DELETED": 5,
    "TABLE_STRUCTURE_EXPANDED": 6,
}


def _location_key(location: DocumentLocation) -> tuple[Any, ...]:
    return (
        location.page,
        location.paragraph_index,
        location.table_index,
        location.row,
        location.column,
        location.section,
        location.source,
    )


def _unique_locations(locations: list[DocumentLocation]) -> tuple[DocumentLocation, ...]:
    result: list[DocumentLocation] = []
    seen: set[tuple[Any, ...]] = set()
    for location in locations:
        key = _location_key(location)
        if key in seen:
            continue
        seen.add(key)
        result.append(location)
    return tuple(result)


def _cell_identity(cell: TableCell, table_index: int, row: int, column: int) -> str:
    return (
        cell.logical_cell_id
        or cell.location.structure_id
        or f"table:{table_index}:physical:{row}:{column}"
    )


def _cell_column(cell: TableCell, fallback: int) -> int:
    """Return the parser/OCR column, never a sparse-row array position."""

    return cell.location.column if cell.location.column is not None else fallback


def build_logical_table(block: DocumentBlock) -> LogicalTable:
    """Collapse repeated physical references to one logical cell.

    A DOCX merged cell appears once for every covered grid position while an
    OCR span can appear as one cell with a row/column span.  Grouping by the
    parser-owned logical id handles both without discarding any physical
    locations.
    """

    assert block.table is not None
    grouped: dict[str, LogicalCell] = {}
    row_members: dict[int, list[str]] = {}
    for row in block.table.rows:
        for ordinal, cell in enumerate(row.cells):
            column = _cell_column(cell, ordinal)
            cell_id = _cell_identity(cell, block.table.table_index, row.row, column)
            existing = grouped.get(cell_id)
            if existing is None:
                grouped[cell_id] = LogicalCell(
                    cell_id=cell_id,
                    text=cell.raw_text,
                    row=row.row,
                    column=column,
                    locations=(cell.location,),
                    row_span=cell.row_span,
                    col_span=cell.col_span,
                )
            else:
                if not existing.text.strip() and cell.raw_text.strip():
                    existing.text = cell.raw_text
                row_span = row.row - existing.row + 1
                col_span = column - existing.column + 1
                existing.row = min(existing.row, row.row)
                existing.column = min(existing.column, column)
                existing.locations = _unique_locations([*existing.locations, cell.location])
                existing.row_span = max(
                    existing.row_span,
                    cell.row_span,
                    row_span,
                )
                existing.col_span = max(
                    existing.col_span,
                    cell.col_span,
                    col_span,
                )
            row_members.setdefault(row.row, []).append(cell_id)
    rows: list[list[LogicalCell]] = []
    for row_number in sorted(row_members):
        unique_ids = [
            cell_id
            for cell_id in dict.fromkeys(row_members[row_number])
            if grouped[cell_id].row == row_number
        ]
        if not unique_ids:
            continue
        rows.append(sorted((grouped[item] for item in unique_ids), key=lambda c: c.column))
    return LogicalTable(block=block, rows=rows, cells=list(grouped.values()))


def _logical_tables(document: ParsedDocument) -> list[LogicalTable]:
    return [
        build_logical_table(block)
        for block in sorted(document.blocks, key=lambda item: item.order)
        if block.type == "TABLE" and block.table is not None
    ]


def logical_cell_count(document: ParsedDocument) -> int:
    """Return the number of parser-owned logical cells, not physical repeats."""

    return sum(
        len({cell.cell_id for row in table.rows for cell in row})
        for table in _logical_tables(document)
    )


def _table_pair_map(
    baseline: ParsedDocument, target: ParsedDocument
) -> dict[int, int]:
    """Recreate the deterministic table pairing used by the V2 comparator."""

    left_tables, right_tables = _logical_tables(baseline), _logical_tables(target)
    unused = set(range(len(right_tables)))
    pairs: dict[int, int] = {}
    for left in left_tables:
        best = max(
            unused,
            key=lambda index: _table_match_score(left, right_tables[index]),
            default=None,
        )
        if best is None:
            continue
        if _table_match_score(left, right_tables[best]) < 0.65:
            continue
        unused.remove(best)
        pairs[left.block.table.table_index] = right_tables[best].block.table.table_index
    return pairs


def _cell_lookup(document: ParsedDocument) -> dict[tuple[int, int, int], TableCell]:
    lookup: dict[tuple[int, int, int], TableCell] = {}
    for table in _logical_tables(document):
        assert table.block.table is not None
        for row in table.block.table.rows:
            for ordinal, cell in enumerate(row.cells):
                column = _cell_column(cell, ordinal)
                lookup[(table.block.table.table_index, row.row, column)] = cell
    return lookup


def build_logical_area_resolver(
    baseline: ParsedDocument, target: ParsedDocument
) -> Callable[[DiffItem], str | None]:
    """Build a conservative resolver for one matched table pair.

    The resolver only returns an area for cells with an explicit parser-owned
    logical ID.  When that identity is absent we deliberately return ``None``;
    the caller then falls back to exact physical evidence instead of making an
    unsafe cross-cell or cross-table merge.
    """

    table_pairs = _table_pair_map(baseline, target)
    left_lookup, right_lookup = _cell_lookup(baseline), _cell_lookup(target)

    def side_area(
        side: DiffSide | None,
        lookup: dict[tuple[int, int, int], TableCell],
    ) -> tuple[Any, ...] | None:
        if side is None:
            return ()
        cells: list[tuple[DocumentLocation, TableCell]] = []
        for location in side.locations or [side.location]:
            if (
                location.table_index is None
                or location.row is None
                or location.column is None
            ):
                return None
            cell = lookup.get(
                (location.table_index, location.row, location.column)
            )
            if cell is None:
                return None
            cells.append((location, cell))
        if not cells:
            return None
        table_indexes = {location.table_index for location, _cell in cells}
        if len(table_indexes) != 1:
            return None
        table_index = next(iter(table_indexes))
        logical_ids = {
            cell.logical_cell_id for _location, cell in cells if cell.logical_cell_id
        }
        if len(logical_ids) == len(cells):
            return ("ID", table_index, tuple(sorted(logical_ids)))

        # Older OCR cache entries may not carry logical IDs.  Only use an
        # explicit span as a fallback; a plain physical coordinate is never
        # promoted into a logical identity.
        if any(cell.row_span <= 1 and cell.col_span <= 1 for _location, cell in cells):
            return None
        occupied: set[tuple[int, int]] = set()
        pages: set[int] = set()
        for location, cell in cells:
            assert location.row is not None and location.column is not None
            occupied.update(
                (row, column)
                for row in range(location.row, location.row + cell.row_span)
                for column in range(location.column, location.column + cell.col_span)
            )
            if location.page is not None:
                pages.add(location.page)
        return (
            "SPAN",
            table_index,
            tuple(sorted(occupied)),
            tuple(sorted(pages)),
        )

    def resolve(diff: DiffItem) -> str | None:
        left = side_area(diff.baseline, left_lookup)
        right = side_area(diff.target, right_lookup)
        if left is None or right is None:
            return None
        left_tables = {left[1]} if left else set()
        right_tables = {right[1]} if right else set()
        if any(table_pairs.get(table_index) not in right_tables for table_index in left_tables):
            return None
        pair_tokens = tuple(
            sorted(
                (table_index, table_pairs.get(table_index))
                for table_index in left_tables
            )
        )
        return repr((pair_tokens, left, right))

    return resolve


def _table_header(table: LogicalTable) -> str:
    if not table.rows:
        return ""
    return "|".join(
        comparison_normalize(cell.text)[1]
        for cell in sorted(table.rows[0], key=lambda item: item.column)
    )


def _table_match_score(left: LogicalTable, right: LogicalTable) -> float:
    left_header, right_header = _table_header(left), _table_header(right)
    if left_header and right_header:
        return ratio(left_header, right_header) / 100
    left_text = comparison_normalize(left.block.raw_text)[1]
    right_text = comparison_normalize(right.block.raw_text)[1]
    return ratio(left_text, right_text) / 100 if left_text and right_text else 0.0


def _unit(
    document: ParsedDocument,
    table: LogicalTable,
    cells: list[LogicalCell],
    suffix: str,
) -> ComparableUnit:
    locations = _unique_locations([location for cell in cells for location in cell.locations])
    if not locations:
        locations = (table.block.location,)
    raw = " | ".join(cell.text for cell in cells)
    normalized, match_text = comparison_normalize(raw)
    return ComparableUnit(
        unit_id=f"{table.block.block_id}_{suffix}",
        kind="TABLE_CELL" if len(cells) == 1 else "TABLE_ROW",
        order=table.block.order + (cells[0].row + 1) / 1000,
        raw_text=raw,
        normalized_text=normalized,
        match_text=match_text,
        clause_key=None,
        locations=locations,
        confidence=min(
            [location.confidence for location in locations if location.confidence is not None]
            or [1.0]
        ),
        page=next((location.page for location in locations if location.page is not None), None),
    )


def _table_rows_as_blocks(document: ParsedDocument) -> list[DocumentBlock]:
    blocks: list[DocumentBlock] = []
    for table in _logical_tables(document):
        for row_index, cells in enumerate(table.rows):
            if not cells:
                continue
            raw = " | ".join(cell.text for cell in cells)
            locations = [location for cell in cells for location in cell.locations]
            blocks.append(
                DocumentBlock(
                    block_id=f"{table.block.block_id}_logical_row_{row_index}",
                    type="PARAGRAPH",
                    order=table.block.order + (row_index + 1) / 1000,
                    raw_text=raw,
                    normalized_text=comparison_normalize(raw)[0],
                    location=locations[0] if locations else table.block.location,
                )
            )
    return blocks


def _paragraph_document(document: ParsedDocument, *, include_table_rows: bool) -> ParsedDocument:
    blocks = [block for block in document.blocks if block.type == "PARAGRAPH"]
    if include_table_rows:
        blocks.extend(_table_rows_as_blocks(document))
    return document.model_copy(update={"blocks": sorted(blocks, key=lambda b: b.order)})


def _paragraph_location(diff: DiffItem, *, baseline: bool) -> DocumentLocation | None:
    side = diff.baseline if baseline else diff.target
    if side is None:
        return None
    return side.location


def _adjacent_paragraphs(left: DiffItem, right: DiffItem) -> bool:
    left_location = _paragraph_location(left, baseline=left.diff_type == "DELETED")
    right_location = _paragraph_location(right, baseline=right.diff_type == "DELETED")
    if left_location is None or right_location is None:
        return False
    if left_location.table_index is not None or right_location.table_index is not None:
        return False
    if left_location.paragraph_index is None or right_location.paragraph_index is None:
        return False
    return (
        abs(left_location.paragraph_index - right_location.paragraph_index) <= 1
        and (
            (left.baseline and right.baseline and left.baseline.file_id == right.baseline.file_id)
            or (left.target and right.target and left.target.file_id == right.target.file_id)
        )
    )


def _merge_side_text(left: DiffSide | None, right: DiffSide | None) -> DiffSide | None:
    if left is None:
        return right
    if right is None:
        return left
    locations = _unique_locations(
        [*(left.locations or [left.location]), *(right.locations or [right.location])]
    )
    return left.model_copy(
        update={
            "location": locations[0],
            "locations": list(locations) if len(locations) > 1 else [],
            "text": "\n".join(value for value in (left.text, right.text) if value),
        }
    )


def _merge_consecutive_deletions(differences: list[DiffItem]) -> tuple[list[DiffItem], int]:
    merged: list[DiffItem] = []
    count = 0
    for diff in differences:
        if (
            merged
            and diff.diff_type == merged[-1].diff_type == "DELETED"
            and _adjacent_paragraphs(merged[-1], diff)
        ):
            previous = merged.pop()
            merged.append(
                previous.model_copy(
                    update={"baseline": _merge_side_text(previous.baseline, diff.baseline)}
                )
            )
            count += 1
        else:
            merged.append(diff)
    return merged, count


def _recompose_adjacent_add_delete(
    differences: list[DiffItem],
) -> tuple[list[DiffItem], int]:
    result: list[DiffItem] = []
    count = 0
    index = 0
    while index < len(differences):
        if index + 1 < len(differences):
            first, second = differences[index : index + 2]
            deleted, added = (
                (first, second)
                if first.diff_type == "DELETED" and second.diff_type == "ADDED"
                else (second, first)
                if first.diff_type == "ADDED" and second.diff_type == "DELETED"
                else (None, None)
            )
            if (
                deleted is not None
                and added is not None
                and _adjacent_paragraphs(deleted, added)
                and deleted.baseline is not None
                and added.target is not None
                and comparison_normalize(deleted.baseline.text)[1]
                != comparison_normalize(added.target.text)[1]
                and ratio(
                    comparison_normalize(deleted.baseline.text)[1],
                    comparison_normalize(added.target.text)[1],
                )
                >= 72
            ):
                segments, _before, _after = build_diff_segments(
                    deleted.baseline.text, added.target.text
                )
                result.append(
                    deleted.model_copy(
                        update={
                            "diff_type": "MODIFIED",
                            "title": "文字内容发生变化",
                            "target": added.target,
                            "segments": segments,
                            "confidence": min(deleted.confidence, added.confidence),
                            "candidate_id": None,
                            "logical_area_key": None,
                        }
                    )
                )
                count += 1
                index += 2
                continue
        result.append(differences[index])
        index += 1
    return result, count


_ROW_IDENTITY_WORDS = {
    "序号",
    "编号",
    "编码",
    "名称",
    "项目",
    "设备",
    "型号",
    "字段",
    "科目",
    "类别",
    "代码",
    "条款",
}
_FIELD_ALIASES = {
    "租赁期间": "租赁期限",
    "租赁期": "租赁期限",
    "租期": "租赁期限",
    "项目名称": "名称",
    "设备名称": "名称",
}
_TABLE_HEADER_KEYS = {
    "序号",
    "编号",
    "代码",
    "名称",
    "项目",
    "项目名称",
    "设备名称",
    "租赁物名称",
    "材质/型号",
    "材质型号",
    "型号",
    "位置",
    "单位",
    "数量",
    "金额",
    "资产价值",
    "期数",
    "租金支付日",
    "每期租金",
    "本金",
    "利息",
    "合计",
    "总计",
}


def _field_key(text: str) -> str:
    normalized = comparison_normalize(text)[1]
    return _FIELD_ALIASES.get(normalized, normalized)


def _is_header_cell(cell: LogicalCell) -> bool:
    normalized = comparison_normalize(cell.text)[1]
    compact = re.sub(r"[\s，。；：、,.!?！？()（）【】\[\]{}]", "", normalized)
    if _field_key(compact) in _TABLE_HEADER_KEYS:
        return True
    # Header cells may carry a display unit, such as ``每期租金（元）``.
    # Restrict prefix matching to known multi-character business headers so
    # ordinary values in the first row cannot become structural evidence.
    return any(
        compact.startswith(header)
        for header in _TABLE_HEADER_KEYS
        if len(header) >= 3
    )


def _is_header_row(row: list[LogicalCell]) -> bool:
    """Recognize a header row without treating key/value records as headers."""

    nonempty = [cell for cell in row if comparison_normalize(cell.text)[1]]
    if not nonempty:
        return False
    header_count = sum(_is_header_cell(cell) for cell in nonempty)
    return header_count >= 2 and header_count * 2 >= len(nonempty)


def _data_rows(table: LogicalTable) -> tuple[list[LogicalCell], ...]:
    rows = table.rows
    start = 0
    # Skip a primary header and any immediately following multi-level header.
    while start < len(rows) and _is_header_row(rows[start]):
        start += 1
    return tuple(rows[start:])


def _row_cells_by_column(row: list[LogicalCell]) -> dict[int, LogicalCell]:
    return {cell.column: cell for cell in row}


def _merge_logical_rows(left: list[LogicalCell], right: list[LogicalCell]) -> list[LogicalCell]:
    """Combine a physically wrapped key/value row without losing locations."""

    merged = _row_cells_by_column(left)
    for cell in right:
        previous = merged.get(cell.column)
        if previous is None:
            merged[cell.column] = cell
            continue
        text = "\n".join(value for value in (previous.text, cell.text) if value)
        merged[cell.column] = replace(
            previous,
            text=text,
            locations=_unique_locations([*previous.locations, *cell.locations]),
            row_span=max(previous.row_span, cell.row_span),
            col_span=max(previous.col_span, cell.col_span),
        )
    return sorted(merged.values(), key=lambda cell: cell.column)


def _coalesce_key_value_rows(
    table: LogicalTable,
    rows: tuple[list[LogicalCell], ...],
) -> tuple[list[LogicalCell], ...]:
    """Join wrapped rows sharing a label when no sub-item number separates them."""

    result: list[list[LogicalCell]] = []
    keys: list[str] = []
    for row in rows:
        key = _row_key(row, context=_context_row(table, row), key_value=True)
        label_present = any(
            cell.column == 0 and comparison_normalize(cell.text)[1] for cell in row
        )
        has_subnumber = bool(re.search(r"\|\d", key))
        if result and not label_present and not has_subnumber and key == keys[-1]:
            result[-1] = _merge_logical_rows(result[-1], row)
            continue
        result.append(row)
        keys.append(key)
    return tuple(result)


def _identity_columns(headers: list[LogicalCell]) -> set[int]:
    return {
        cell.column
        for cell in headers
        if any(word in _field_key(cell.text) for word in _ROW_IDENTITY_WORDS)
    }


def _context_row(table: LogicalTable, row: list[LogicalCell]) -> list[LogicalCell]:
    """Include vertically merged context cells only for row identity matching."""

    if not row:
        return []
    row_number = min(cell.row for cell in row)
    row_columns = {
        item.column
        for item in row
        if comparison_normalize(item.text)[1]
    }
    inherited = [
        cell
        for cell in table.cells
        if cell.row < row_number
        and cell.row <= row_number < cell.row + max(cell.row_span, 1)
        and cell.column not in row_columns
    ]
    # OCR commonly omits an empty continuation of the first label column
    # instead of emitting a row-spanning cell.  Inherit only that leading
    # business-group label for row-key construction; other omitted columns
    # (for example a location column) are handled by the comparison gate and
    # must never be fabricated here.
    if 0 not in row_columns and not any(cell.column == 0 for cell in inherited):
        previous = [
            cell
            for cell in table.cells
            if cell.column == 0 and cell.row < row_number
            and comparison_normalize(cell.text)[1]
        ]
        if previous:
            inherited.append(max(previous, key=lambda cell: cell.row))
    return sorted([*inherited, *row], key=lambda cell: cell.column)


def _vertical_merge_continuation(
    table: LogicalTable,
    row: list[LogicalCell],
    column: int,
    expected_text: str,
    *,
    rows_reliable: bool,
) -> bool:
    """Check an omitted OCR cell against a prior same-column value.

    This is deliberately a comparison-only inference.  It does not return a
    synthetic cell or copy its location into the result.  A continuation is
    accepted only after the row keys have been proven unique and the nearest
    available OCR value in that column is text-identical to the baseline.
    """

    if not rows_reliable:
        return False
    row_number = min((cell.row for cell in row), default=None)
    if row_number is None:
        return False
    prior = [
        cell
        for candidate_row in table.rows
        if (
            candidate_row_number := min(
                (cell.row for cell in candidate_row), default=None
            )
        )
        is not None
        and candidate_row_number < row_number
        for cell in candidate_row
        if cell.column == column and comparison_normalize(cell.text)[1]
    ]
    if not prior:
        return False
    nearest = max(prior, key=lambda cell: cell.row)
    return comparison_normalize(nearest.text)[1] == comparison_normalize(expected_text)[1]


def _row_key(
    row: list[LogicalCell],
    *,
    headers: list[LogicalCell] | None = None,
    context: list[LogicalCell] | None = None,
    key_value: bool = False,
) -> str:
    cells = context or row
    if not cells:
        return ""
    ordered = sorted(cells, key=lambda cell: cell.column)
    if key_value:
        nonempty = [cell for cell in ordered if comparison_normalize(cell.text)[1]]
        if not nonempty:
            return ""
        label = nonempty[0]
        label_text = comparison_normalize(label.text)[1]
        value_text = "|".join(
            comparison_normalize(cell.text)[1]
            for cell in nonempty
            if cell.column > label.column
        )
        sub_number = re.match(r"^[（(]?\d+[）)、．.、]?", value_text)
        suffix = sub_number.group(0) if sub_number else ""
        return "KV:" + label_text + ("|" + suffix if suffix else "")
    identity_columns = _identity_columns(headers or [])
    selected = [cell for cell in cells if cell.column in identity_columns]
    if not selected:
        selected = [cell for cell in cells if comparison_normalize(cell.text)[1]]
        selected = selected[:1]
    values = [comparison_normalize(cell.text)[1] for cell in selected]
    return "|".join(value for value in values if value)


def _column_mapping(left: LogicalTable, right: LogicalTable) -> tuple[dict[int, int], bool]:
    left_header = left.rows[0] if left.rows else []
    right_header = right.rows[0] if right.rows else []
    left_has_header = _is_header_row(left_header)
    right_has_header = _is_header_row(right_header)
    if not left_has_header or not right_has_header:
        left_columns = {
            cell.column for row in left.rows for cell in row
        }
        right_columns = {
            cell.column for row in right.rows for cell in row
        }
        mapping = {column: column for column in left_columns & right_columns}
        return mapping, left_columns == right_columns

    left_keys = {
        cell.column: _field_key(cell.text)
        for cell in left_header
        if _field_key(cell.text)
    }
    right_keys = {
        cell.column: _field_key(cell.text)
        for cell in right_header
        if _field_key(cell.text)
    }
    keyed = (
        bool(left_keys)
        and bool(right_keys)
        and len(set(left_keys.values())) == len(left_keys)
        and len(set(right_keys.values())) == len(right_keys)
    )
    if keyed:
        right_by_key = {key: column for column, key in right_keys.items()}
        mapping = {
            column: right_by_key[key]
            for column, key in left_keys.items()
            if key in right_by_key
        }
        return mapping, len(mapping) == max(len(left_keys), len(right_keys))
    left_columns = {cell.column for cell in left_header}
    right_columns = {cell.column for cell in right_header}
    mapping = {column: column for column in left_columns & right_columns}
    return mapping, left_columns == right_columns


def _row_pairs(
    left: LogicalTable, right: LogicalTable
) -> tuple[list[tuple[list[LogicalCell] | None, list[LogicalCell] | None]], bool]:
    left_data, right_data = _data_rows(left), _data_rows(right)
    left_header = left.rows[0] if left.rows else []
    right_header = right.rows[0] if right.rows else []
    key_value = not _is_header_row(left_header) and not _is_header_row(right_header)
    if key_value:
        left_data = _coalesce_key_value_rows(left, left_data)
        right_data = _coalesce_key_value_rows(right, right_data)
    left_keys, right_keys = (
        [
            _row_key(
                row,
                headers=left_header,
                context=_context_row(left, row),
                key_value=key_value,
            )
            for row in left_data
        ],
        [
            _row_key(
                row,
                headers=right_header,
                context=_context_row(right, row),
                key_value=key_value,
            )
            for row in right_data
        ],
    )
    keyed = (
        bool(left_data)
        and bool(right_data)
        and all(left_keys)
        and all(right_keys)
        and len(set(left_keys)) == len(left_keys)
        and len(set(right_keys)) == len(right_keys)
    )
    pairs: list[tuple[list[LogicalCell] | None, list[LogicalCell] | None]] = []
    if keyed:
        right_by_key = dict(zip(right_keys, right_data, strict=True))
        left_keys_set = set(left_keys)
        pairs.extend(
            (row, right_by_key.get(key)) for key, row in zip(left_keys, left_data, strict=True)
        )
        pairs.extend(
            (None, row)
            for key, row in zip(right_keys, right_data, strict=True)
            if key not in left_keys_set
        )
        return pairs, True
    for index in range(max(len(left_data), len(right_data))):
        pairs.append(
            (
                left_data[index] if index < len(left_data) else None,
                right_data[index] if index < len(right_data) else None,
            )
        )
    return pairs, False


def _candidate_id(diff: DiffItem) -> str:
    parts = [diff.diff_type, diff.title]
    for side in (diff.baseline, diff.target):
        if side is None:
            parts.append("-")
            continue
        parts.append(side.file_id)
        parts.append(side.text)
        parts.extend(
            repr(_location_key(location)) for location in side.locations or [side.location]
        )
    digest_input = "\x1f".join(parts).encode()
    return f"candidate_{hashlib.sha256(digest_input).hexdigest()[:16]}"


def _mark_candidate(
    diff: DiffItem,
    *,
    reason_code: str,
) -> DiffItem:
    candidate_id = _candidate_id(diff)
    return diff.model_copy(
        update={
            "candidate_id": candidate_id,
            "validation_status": "REVIEW_REQUIRED",
            "validation_source": "RULE",
            "validation_reason_code": reason_code,
        }
    )


def _compare_tables(
    baseline: ParsedDocument,
    target: ParsedDocument,
    options: Any,
    start_index: int,
) -> tuple[list[DiffItem], list[dict[str, Any]], int, dict[str, int]]:
    left_tables, right_tables = _logical_tables(baseline), _logical_tables(target)
    unused = set(range(len(right_tables)))
    diffs: list[DiffItem] = []
    ambiguous_count = 0
    sparse_column_alignment_count = 0
    vertical_merge_continuation_count = 0
    key_value_row_alignment_count = 0

    def add(diff: DiffItem, *, ambiguous: bool = False) -> None:
        nonlocal ambiguous_count
        if ambiguous:
            diff = _mark_candidate(diff, reason_code="TABLE_STRUCTURE_AMBIGUOUS")
            ambiguous_count += 1
        diffs.append(diff)

    for _left_index, left in enumerate(left_tables):
        best = max(
            unused, key=lambda index: _table_match_score(left, right_tables[index]), default=None
        )
        score = _table_match_score(left, right_tables[best]) if best is not None else 0.0
        if best is None or score < 0.65:
            cells = [cell for row in left.rows for cell in row]
            missing_table = _make_diff(
                start_index + len(diffs),
                "TABLE_STRUCTURE_EXPANDED",
                baseline,
                target,
                (_unit(baseline, left, cells, "all"),) if cells else (),
                (),
                score,
                options,
            )
            # A multi-row baseline table with no compatible target table is
            # one missing content block.  This is distinct from a low-score
            # single-row or partially changed table, which remains an
            # ambiguous structure candidate for review.
            recognized_header_count = (
                sum(_is_header_cell(cell) for cell in left.rows[0])
                if left.rows
                else 0
            )
            if len(left.rows) >= 2 and recognized_header_count >= 3:
                add(
                    missing_table.model_copy(
                        update={
                            "diff_type": "CONTENT_BLOCK_MISSING",
                            "title": "连续内容缺失",
                            "certainty": "CONFIRMED",
                            "validation_status": "CONFIRMED",
                            "validation_source": "RULE",
                            "validation_reason_code": "TABLE_CONTENT_BLOCK_MISSING",
                        }
                    )
                )
                continue
            add(missing_table, ambiguous=True)
            continue
        right = right_tables[best]
        unused.remove(best)
        column_map, columns_reliable = _column_mapping(left, right)
        pairs, rows_reliable = _row_pairs(left, right)
        key_value_table = not _is_header_row(
            left.rows[0] if left.rows else []
        ) and not _is_header_row(right.rows[0] if right.rows else [])
        ambiguous = not columns_reliable or not rows_reliable
        for left_row, right_row in pairs:
            if left_row is None and right_row is not None:
                add(
                    _make_diff(
                        start_index + len(diffs),
                        "TABLE_ROW_ADDED",
                        baseline,
                        target,
                        (),
                        (_unit(target, right, right_row, f"row_{right_row[0].row}"),),
                        0.9,
                        options,
                    ),
                    ambiguous=ambiguous,
                )
                continue
            if right_row is None and left_row is not None:
                add(
                    _make_diff(
                        start_index + len(diffs),
                        "TABLE_ROW_DELETED",
                        baseline,
                        target,
                        (_unit(baseline, left, left_row, f"row_{left_row[0].row}"),),
                        (),
                        0.9,
                        options,
                    ),
                    ambiguous=ambiguous,
                )
                continue
            if left_row is None or right_row is None:
                continue
            if {
                cell.column for cell in left_row
            } != {cell.column for cell in right_row}:
                sparse_column_alignment_count += 1
            if key_value_table:
                key_value_row_alignment_count += 1
            right_by_column = _row_cells_by_column(right_row)
            for left_cell in left_row:
                left_column = left_cell.column
                right_column = column_map.get(left_column)
                right_cell = right_by_column.get(right_column) if right_column is not None else None
                if right_cell is None:
                    if _vertical_merge_continuation(
                        right,
                        right_row,
                        right_column if right_column is not None else left_column,
                        left_cell.text,
                        rows_reliable=rows_reliable,
                    ):
                        vertical_merge_continuation_count += 1
                        continue
                    add(
                        _make_diff(
                            start_index + len(diffs),
                            "TABLE_CELL_CHANGED",
                            baseline,
                            target,
                            (
                                _unit(
                                    baseline,
                                    left,
                                    [left_cell],
                                    f"r{left_cell.row}c{left_cell.column}",
                                ),
                            ),
                            (),
                            0.8,
                            options,
                        ),
                        ambiguous=True,
                    )
                    continue
                if (
                    comparison_normalize(left_cell.text)[1]
                    == comparison_normalize(right_cell.text)[1]
                ):
                    continue
                kind = (
                    "NUMERIC_CHANGED"
                    if options.numeric_sensitive
                    and _numeric_changed(left_cell.text, right_cell.text)
                    else "TABLE_CELL_CHANGED"
                )
                add(
                    _make_diff(
                        start_index + len(diffs),
                        kind,
                        baseline,
                        target,
                        (
                            _unit(
                                baseline, left, [left_cell], f"r{left_cell.row}c{left_cell.column}"
                            ),
                        ),
                        (
                            _unit(
                                target,
                                right,
                                [right_cell],
                                f"r{right_cell.row}c{right_cell.column}",
                            ),
                        ),
                        min(score, 0.95),
                        options,
                    ),
                    ambiguous=ambiguous,
                )
        for right_header_cell in right.rows[0] if right.rows else []:
            right_column = right_header_cell.column
            if right_column not in column_map.values() and right.rows[1:]:
                add(
                    _make_diff(
                        start_index + len(diffs),
                        "TABLE_CELL_CHANGED",
                        baseline,
                        target,
                        (),
                        (_unit(target, right, [right_header_cell], f"header_c{right_column}"),),
                        0.8,
                        options,
                    ),
                    ambiguous=True,
                )
    for right_index in sorted(unused):
        right = right_tables[right_index]
        cells = [cell for row in right.rows for cell in row]
        add(
            _make_diff(
                start_index + len(diffs),
                "TABLE_STRUCTURE_EXPANDED",
                baseline,
                target,
                (),
                (_unit(target, right, cells, "all"),) if cells else (),
                0.0,
                options,
            ),
            ambiguous=True,
        )
    records = [_candidate_record(diff) for diff in diffs if diff.candidate_id]
    return diffs, records, ambiguous_count, {
        "sparse_column_alignment_count": sparse_column_alignment_count,
        "vertical_merge_continuation_count": vertical_merge_continuation_count,
        "key_value_row_alignment_count": key_value_row_alignment_count,
    }


def _candidate_record(diff: DiffItem) -> dict[str, Any]:
    def side_payload(side: DiffSide | None) -> dict[str, Any] | None:
        if side is None:
            return None
        return {
            "file_id": side.file_id,
            "text": side.text,
            "locations": [
                {
                    "page": location.page,
                    "table_index": location.table_index,
                    "row": location.row,
                    "column": location.column,
                }
                for location in (side.locations or [side.location])
            ],
        }

    return {
        "candidate_id": diff.candidate_id,
        "diff_id": diff.diff_id,
        "diff_type": diff.diff_type,
        "logical_area_key": diff.logical_area_key,
        "baseline": side_payload(diff.baseline),
        "target": side_payload(diff.target),
    }


def _safe_text_metadata(text: str | None) -> dict[str, Any]:
    value = text or ""
    return {
        "chars": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16],
    }


def _safe_location_metadata(side: DiffSide | None) -> list[dict[str, Any]]:
    if side is None:
        return []
    return [
        {
            "page": location.page,
            "table_index": location.table_index,
            "row": location.row,
            "column": location.column,
        }
        for location in side.locations or [side.location]
    ]


def _evidence_key(
    diff: DiffItem,
    logical_area_resolver: Callable[[DiffItem], str | None] | None = None,
) -> tuple[Any, ...]:
    logical_area_key = diff.logical_area_key or (
        logical_area_resolver(diff) if logical_area_resolver is not None else None
    )

    def side_key(side: DiffSide | None) -> tuple[Any, ...]:
        if side is None:
            return ()
        normalized_text = comparison_normalize(side.text)[1]
        if logical_area_key is not None:
            return (side.file_id, normalized_text)
        locations = tuple(
            sorted(_location_key(location) for location in side.locations or [side.location])
        )
        return (side.file_id, locations, normalized_text)

    return (
        "LOGICAL_AREA" if logical_area_key is not None else "PHYSICAL_EVIDENCE",
        logical_area_key,
        side_key(diff.baseline),
        side_key(diff.target),
    )


def _merge_side_locations(
    left: DiffSide | None, right: DiffSide | None
) -> DiffSide | None:
    if left is None:
        return right
    if right is None:
        return left
    locations = _unique_locations(
        [*(left.locations or [left.location]), *(right.locations or [right.location])]
    )
    return left.model_copy(
        update={
            "location": locations[0],
            "locations": list(locations) if len(locations) > 1 else [],
        }
    )


def _merge_diff_evidence(left: DiffItem, right: DiffItem) -> DiffItem:
    return left.model_copy(
        update={
            "baseline": _merge_side_locations(left.baseline, right.baseline),
            "target": _merge_side_locations(left.target, right.target),
        }
    )


def deduplicate_diff_candidates_with_audit(
    differences: list[DiffItem],
    *,
    logical_area_resolver: Callable[[DiffItem], str | None] | None = None,
) -> tuple[list[DiffItem], dict[str, int], list[dict[str, Any]]]:
    """Deduplicate logical evidence and return a safe merge audit trail."""

    kept: dict[tuple[Any, ...], DiffItem] = {}
    exact_removed = 0
    logical_area_merged = 0
    cross_type_merged = 0
    groups: list[dict[str, Any]] = []
    for diff in differences:
        key = _evidence_key(diff, logical_area_resolver)
        previous = kept.get(key)
        if previous is None:
            kept[key] = diff
            continue

        logical = key[0] == "LOGICAL_AREA"
        if previous.diff_type == diff.diff_type:
            exact_removed += 1
            logical_area_merged += int(logical)
            kept[key] = _merge_diff_evidence(previous, diff)
            groups.append(
                {
                    "reason_code": "LOGICAL_DUPLICATE" if logical else "EVIDENCE_DUPLICATE",
                    "kept_diff_id": previous.diff_id,
                    "removed_diff_id": diff.diff_id,
                    "kept_diff_type": previous.diff_type,
                    "removed_diff_type": diff.diff_type,
                    "baseline_text": _safe_text_metadata(
                        previous.baseline.text if previous.baseline else None
                    ),
                    "target_text": _safe_text_metadata(
                        previous.target.text if previous.target else None
                    ),
                    "baseline_locations": _safe_location_metadata(previous.baseline),
                    "target_locations": _safe_location_metadata(previous.target),
                }
            )
            continue

        chosen = previous
        if _DIFF_PRIORITY.get(diff.diff_type, 99) < _DIFF_PRIORITY.get(
            previous.diff_type, 99
        ):
            chosen = diff
        cross_type_merged += 1
        logical_area_merged += int(logical)
        kept[key] = _merge_diff_evidence(chosen, diff if chosen is previous else previous)
        groups.append(
            {
                "reason_code": "CROSS_TYPE_MERGED",
                "kept_diff_id": chosen.diff_id,
                "removed_diff_id": diff.diff_id if chosen is previous else previous.diff_id,
                "kept_diff_type": chosen.diff_type,
                "removed_diff_type": diff.diff_type if chosen is previous else previous.diff_type,
                "baseline_text": _safe_text_metadata(
                    chosen.baseline.text if chosen.baseline else None
                ),
                "target_text": _safe_text_metadata(
                    chosen.target.text if chosen.target else None
                ),
                "baseline_locations": _safe_location_metadata(kept[key].baseline),
                "target_locations": _safe_location_metadata(kept[key].target),
            }
        )

    ordered = list(kept.values())
    return ordered, {
        "rule_deduplicated_count": exact_removed,
        "logical_area_merged_count": logical_area_merged,
        "cross_type_merged_count": cross_type_merged,
    }, groups


def deduplicate_diff_candidates(
    differences: list[DiffItem],
    *,
    logical_area_resolver: Callable[[DiffItem], str | None] | None = None,
) -> tuple[list[DiffItem], dict[str, int]]:
    """Compatibility wrapper returning only the V2 result and counters."""

    ordered, stats, _groups = deduplicate_diff_candidates_with_audit(
        differences, logical_area_resolver=logical_area_resolver
    )
    return ordered, stats


def _renumber(differences: list[DiffItem]) -> list[DiffItem]:
    return [
        diff.model_copy(update={"diff_id": f"diff_{index:06d}"})
        for index, diff in enumerate(differences, start=1)
    ]


def _attach_logical_area_keys(
    differences: list[DiffItem],
    resolver: Callable[[DiffItem], str | None],
) -> list[DiffItem]:
    enriched: list[DiffItem] = []
    for diff in differences:
        area_key = resolver(diff)
        enriched.append(
            diff.model_copy(update={"logical_area_key": area_key})
            if area_key is not None
            else diff
        )
    return enriched


def compare_documents_logical_v2(
    baseline: ParsedDocument,
    target: ParsedDocument,
    options: Any,
) -> ComparisonResult:
    has_tables = any(block.type == "TABLE" for block in baseline.blocks) or any(
        block.type == "TABLE" for block in target.blocks
    )
    both_have_tables = any(block.type == "TABLE" for block in baseline.blocks) and any(
        block.type == "TABLE" for block in target.blocks
    )
    paragraph_options = replace(
        options,
        comparison_mode="LEGACY",
        semantic_clause_alignment=True,
    )
    paragraph_comparison = compare_documents_reliably(
        _paragraph_document(baseline, include_table_rows=has_tables and not both_have_tables),
        _paragraph_document(target, include_table_rows=has_tables and not both_have_tables),
        paragraph_options,
    )
    differences = list(paragraph_comparison.diff_items)
    differences, consecutive_deletion_merge_count = _merge_consecutive_deletions(
        differences
    )
    differences, add_delete_recomposition_count = _recompose_adjacent_add_delete(
        differences
    )
    records: list[dict[str, Any]] = []
    ambiguous_count = 0
    table_alignment_stats = {
        "sparse_column_alignment_count": 0,
        "vertical_merge_continuation_count": 0,
        "key_value_row_alignment_count": 0,
    }
    if both_have_tables:
        table_diffs, _table_records, table_ambiguous, table_stats = _compare_tables(
            baseline, target, paragraph_options, len(differences) + 1
        )
        differences.extend(table_diffs)
        ambiguous_count += table_ambiguous
        for key in table_alignment_stats:
            table_alignment_stats[key] += table_stats[key]
    area_resolver = build_logical_area_resolver(baseline, target)
    differences = _attach_logical_area_keys(differences, area_resolver)
    raw_candidate_count = len(differences)
    differences, dedup_stats, dedup_groups = deduplicate_diff_candidates_with_audit(
        differences, logical_area_resolver=area_resolver
    )
    pre_renumber = list(differences)
    differences = _renumber(differences)
    old_to_new_id = {
        old.diff_id: new.diff_id for old, new in zip(pre_renumber, differences, strict=True)
    }
    for group in dedup_groups:
        if group.get("kept_diff_id") in old_to_new_id:
            group["kept_diff_id"] = old_to_new_id[group["kept_diff_id"]]
    records = [_candidate_record(diff) for diff in differences if diff.candidate_id]
    warning_items = list(paragraph_comparison.warnings)
    if ambiguous_count:
        warning_items.append(
            ProcessingWarning(
                code="FINAL_COMPARE_CANDIDATES_REVIEW_REQUIRED",
                message="部分表格结构存在歧义，已保留差异并标记待人工复核。",
                requires_manual_review=False,
                details={"candidate_count": ambiguous_count},
            )
        )
    diagnostics = paragraph_comparison.diagnostics.model_copy(
        update={
            "candidate_diff_count": len(differences),
            "emitted_diff_count": len(differences),
            "compatible_table_count": len(_logical_tables(baseline)) if both_have_tables else 0,
            "fallback_mode": "FINAL_LOGICAL_V2",
            "reasons": list(
                dict.fromkeys(
                    [*paragraph_comparison.diagnostics.reasons]
                    + (["TABLE_CANDIDATES_REVIEW_REQUIRED"] if ambiguous_count else [])
                )
            ),
        }
    )
    return ComparisonResult(
        diff_items=differences,
        warnings=aggregate_warnings(warning_items),
        diagnostics=diagnostics,
        candidate_records=records,
        validation_stats={
            "raw_candidate_count": raw_candidate_count,
            "logical_cell_count": logical_cell_count(baseline)
            + logical_cell_count(target),
            "rule_deduplicated_count": dedup_stats["rule_deduplicated_count"],
            "logical_area_merged_count": dedup_stats["logical_area_merged_count"],
            "cross_type_merged_count": dedup_stats["cross_type_merged_count"],
            "number_shift_merged_count": paragraph_comparison.validation_stats.get(
                "semantic_number_shift_merged_count", 0
            ),
            "consecutive_deletion_merge_count": consecutive_deletion_merge_count,
            "add_delete_recomposition_count": add_delete_recomposition_count,
            "table_mismatch_excluded_count": ambiguous_count,
            **table_alignment_stats,
            "confirmed_change_count": sum(
                diff.validation_status == "CONFIRMED" for diff in differences
            ),
            "llm_reviewed_count": 0,
            "llm_duplicate_removed_count": 0,
            "review_required_count": sum(
                diff.validation_status == "REVIEW_REQUIRED" for diff in differences
            ),
            "final_diff_count": len(differences),
            "candidate_validation_failures": 0,
        },
        dedup_groups=dedup_groups,
    )
