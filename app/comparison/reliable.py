from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Protocol

from rapidfuzz.fuzz import ratio

from app.comparison.models import (
    ComparisonDiagnostics,
    ComparisonResult,
    DiffItem,
    DiffSegment,
    DiffSide,
)
from app.documents.models import (
    DocumentBlock,
    DocumentLocation,
    ParsedDocument,
    ProcessingWarning,
)

NUMBER_PATTERN = re.compile(
    r"(?:\d{4}[年./-]\d{1,2}(?:[月./-]\d{1,2}日?)?|\d[\d,]*(?:\.\d+)?%?|"
    r"[一二三四五六七八九十百千万]+(?:年|个月|月|日))"
)
CLAUSE_PATTERN = re.compile(
    r"^(第[一二三四五六七八九十百千万0-9]+条|\d+(?:\.\d+)*(?:、|\.(?!\d)))"
)
CLAUSE_SPLIT_PATTERN = re.compile(
    r"(?=(?:第[一二三四五六七八九十百千万0-9]+条|"
    r"(?<!\S)\d+(?:\.\d+)*(?:、|\.(?!\d))))"
)
INTER_CJK_SPACE = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")
HTML_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
LATEX_ROMAN = re.compile(r"\\mathrm\s*\{([^{}]*)\}")
IGNORABLE_PUNCTUATION = str.maketrans("", "", "，,。；;：:“”\"‘’'（）()【】[]、 *|~")
TABLE_CONTINUATION_HEADERS = (
    "名称",
    "描述",
    "说明",
    "备注",
    "约定",
    "用途",
    "地点",
    "地址",
    "规格",
)
TABLE_PROTECTED_HEADERS = (
    "序号",
    "编号",
    "编码",
    "型号",
    "数量",
    "单价",
    "金额",
    "日期",
    "期限",
    "比例",
    "利率",
    "税率",
    "期数",
    "账号",
    "合计",
)
PLACEHOLDER_CONTEXT = ("金额", "价款", "大写", "小写", "人民币", "____")


class CompareOptionsLike(Protocol):
    ignore_formatting: bool
    ignore_headers_footers: bool
    numeric_sensitive: bool
    ocr_low_confidence_threshold: float


@dataclass(frozen=True)
class ComparableUnit:
    unit_id: str
    kind: str
    order: float
    raw_text: str
    normalized_text: str
    match_text: str
    clause_key: str | None
    locations: tuple[DocumentLocation, ...]
    confidence: float
    page: int | None


@dataclass(frozen=True)
class ComparableDocument:
    source: ParsedDocument
    units: tuple[ComparableUnit, ...]


@dataclass(frozen=True)
class AlignmentStep:
    baseline: tuple[ComparableUnit, ...]
    target: tuple[ComparableUnit, ...]
    similarity: float


@dataclass(frozen=True)
class AlignmentOutcome:
    steps: tuple[AlignmentStep, ...]
    coverage_baseline: float
    coverage_target: float
    unmatched_baseline: int
    unmatched_target: int


@dataclass(frozen=True)
class TableMatch:
    baseline: DocumentBlock
    target: DocumentBlock


@dataclass(frozen=True)
class ComparableTableCell:
    raw_text: str
    locations: tuple[DocumentLocation, ...]


@dataclass(frozen=True)
class ComparableTableRow:
    row: int
    cells: tuple[ComparableTableCell, ...]
    source_rows: tuple[int, ...]


def comparison_normalize(text: str) -> tuple[str, str]:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("\u200b", "").replace("\u00ad", "")
    normalized = HTML_BREAK.sub(" ", normalized)
    normalized = LATEX_ROMAN.sub(r"\1", normalized)
    normalized = normalized.replace("\\sim", "~").replace("$", "")
    normalized = INTER_CJK_SPACE.sub("", normalized)
    normalized = " ".join(normalized.split())
    normalized = normalized.replace("：", ":").replace("，", ",")
    match_text = normalized.translate(IGNORABLE_PUNCTUATION).replace(":", "")
    return normalized, match_text


def _clause_key(text: str) -> str | None:
    match = CLAUSE_PATTERN.match(text)
    return match.group(1) if match else None


def _confidence(locations: tuple[DocumentLocation, ...]) -> float:
    values = [location.confidence for location in locations if location.confidence is not None]
    return min(values) if values else 1.0


def _paragraph_units(block: DocumentBlock) -> list[ComparableUnit]:
    normalized, _ = comparison_normalize(block.raw_text)
    pieces = [piece.strip() for piece in CLAUSE_SPLIT_PATTERN.split(normalized) if piece.strip()]
    if not pieces:
        pieces = [normalized]
    return [
        ComparableUnit(
            unit_id=f"{block.block_id}_part_{index}",
            kind="PARAGRAPH",
            order=block.order + index / max(len(pieces), 1) / 10,
            raw_text=piece,
            normalized_text=comparison_normalize(piece)[0],
            match_text=comparison_normalize(piece)[1],
            clause_key=_clause_key(comparison_normalize(piece)[0]),
            locations=(block.location,),
            confidence=_confidence((block.location,)),
            page=block.location.page,
        )
        for index, piece in enumerate(pieces)
        if comparison_normalize(piece)[1]
    ]


def build_comparable_document(
    document: ParsedDocument,
    options: CompareOptionsLike,
    *,
    structured_table_indexes: set[int],
) -> ComparableDocument:
    allowed = (
        {"PARAGRAPH"}
        if options.ignore_headers_footers
        else {"PARAGRAPH", "HEADER", "FOOTER", "SIDEBAR"}
    )
    units: list[ComparableUnit] = []
    for block in sorted(document.blocks, key=lambda item: item.order):
        if block.type in allowed:
            units.extend(_paragraph_units(block))
        elif (
            block.type == "TABLE"
            and block.table is not None
            and block.table.table_index not in structured_table_indexes
        ):
            units.extend(
                _comparable_table_row_unit(block, row)
                for row in _comparable_table_rows(block)
            )
    return ComparableDocument(
        source=document, units=tuple(sorted(units, key=lambda item: item.order))
    )


def _table_header(block: DocumentBlock) -> str:
    rows = _comparable_table_rows(block)
    if not rows:
        return ""
    return "|".join(comparison_normalize(cell.raw_text)[1] for cell in rows[0].cells)


def _table_column_count(block: DocumentBlock) -> int:
    if not block.table:
        return 0
    maximum = 0
    for row in block.table.rows:
        for position, cell in enumerate(row.cells):
            column = cell.location.column if cell.location.column is not None else position
            maximum = max(maximum, column + 1)
    return maximum


def _dense_table_rows(block: DocumentBlock) -> list[ComparableTableRow]:
    if not block.table:
        return []
    column_count = _table_column_count(block)
    dense_rows: list[ComparableTableRow] = []
    for row in sorted(block.table.rows, key=lambda item: item.row):
        cells = [ComparableTableCell(raw_text="", locations=()) for _ in range(column_count)]
        for position, cell in enumerate(row.cells):
            column = cell.location.column if cell.location.column is not None else position
            if column >= column_count:
                continue
            existing = cells[column]
            cells[column] = ComparableTableCell(
                raw_text=f"{existing.raw_text}{cell.raw_text}",
                locations=(*existing.locations, cell.location),
            )
        dense_rows.append(
            ComparableTableRow(row=row.row, cells=tuple(cells), source_rows=(row.row,))
        )
    return dense_rows


def _is_ocr_location(location: DocumentLocation) -> bool:
    return location.source == "OCR"


def _is_adjacent_table_continuation(
    header: ComparableTableRow,
    previous: ComparableTableRow,
    current: ComparableTableRow,
) -> int | None:
    if not previous.cells or len(previous.cells) != len(current.cells):
        return None
    if current.row != previous.source_rows[-1] + 1:
        return None
    nonempty = [
        column
        for column, cell in enumerate(current.cells)
        if comparison_normalize(cell.raw_text)[1]
    ]
    if len(nonempty) != 1 or nonempty[0] == 0:
        return None
    column = nonempty[0]
    header_text = comparison_normalize(header.cells[column].raw_text)[1]
    if not any(keyword in header_text for keyword in TABLE_CONTINUATION_HEADERS):
        return None
    if any(keyword in header_text for keyword in TABLE_PROTECTED_HEADERS):
        return None
    if not comparison_normalize(previous.cells[0].raw_text)[1]:
        return None
    if not comparison_normalize(previous.cells[column].raw_text)[1]:
        return None
    previous_locations = previous.cells[column].locations
    current_locations = current.cells[column].locations
    if not previous_locations or not current_locations:
        return None
    continuation_locations = (*previous_locations, *current_locations)
    if not all(_is_ocr_location(location) for location in continuation_locations):
        return None
    previous_location = previous_locations[-1]
    current_location = current_locations[0]
    if (
        previous_location.table_index != current_location.table_index
        or previous_location.column != current_location.column
        or previous_location.row not in previous.source_rows
        or current_location.row != current.row
    ):
        return None
    if (
        previous_location.page is not None
        and current_location.page is not None
        and previous_location.page != current_location.page
    ):
        return None
    return column


def _comparable_table_rows(block: DocumentBlock) -> list[ComparableTableRow]:
    rows = _dense_table_rows(block)
    if len(rows) < 3:
        return rows
    header, *data = rows
    merged: list[ComparableTableRow] = []
    for row in data:
        if not merged:
            merged.append(row)
            continue
        column = _is_adjacent_table_continuation(header, merged[-1], row)
        if column is None:
            merged.append(row)
            continue
        previous = merged[-1]
        cells = list(previous.cells)
        cells[column] = ComparableTableCell(
            raw_text=f"{cells[column].raw_text}{row.cells[column].raw_text}",
            locations=(*cells[column].locations, *row.cells[column].locations),
        )
        merged[-1] = ComparableTableRow(
            row=previous.row,
            cells=tuple(cells),
            source_rows=(*previous.source_rows, *row.source_rows),
        )
    return [header, *merged]


def _table_shape(block: DocumentBlock) -> tuple[int, int]:
    rows = _comparable_table_rows(block)
    return len(rows), len(rows[0].cells) if rows else 0


def match_compatible_tables(
    baseline: ParsedDocument, target: ParsedDocument
) -> tuple[list[TableMatch], int]:
    base_tables = [block for block in baseline.blocks if block.type == "TABLE" and block.table]
    target_tables = [block for block in target.blocks if block.type == "TABLE" and block.table]
    unused = set(range(len(target_tables)))
    matches: list[TableMatch] = []
    for base in base_tables:
        best = None
        best_score = -1.0
        for position in unused:
            score = ratio(_table_header(base), _table_header(target_tables[position])) / 100
            if score > best_score:
                best = position
                best_score = score
        if best is None:
            continue
        candidate = target_tables[best]
        base_rows, base_columns = _table_shape(base)
        target_rows, target_columns = _table_shape(candidate)
        row_tolerance = max(3, math.ceil(max(base_rows, target_rows) * 0.2))
        if (
            best_score >= 0.8
            and base_columns == target_columns
            and abs(base_rows - target_rows) <= row_tolerance
        ):
            matches.append(TableMatch(baseline=base, target=candidate))
            unused.remove(best)
    incompatible = len(base_tables) + len(target_tables) - 2 * len(matches)
    return matches, incompatible


def _join_match(units: tuple[ComparableUnit, ...]) -> str:
    return "".join(unit.match_text for unit in units)


def _join_raw(units: tuple[ComparableUnit, ...]) -> str:
    return "\n".join(unit.raw_text for unit in units)


def _same_clause(left: tuple[ComparableUnit, ...], right: tuple[ComparableUnit, ...]) -> bool:
    return bool(left[0].clause_key and left[0].clause_key == right[0].clause_key)


def _same_numeric_context(
    left: tuple[ComparableUnit, ...], right: tuple[ComparableUnit, ...]
) -> bool:
    left_text, right_text = _join_match(left), _join_match(right)
    if not NUMBER_PATTERN.search(left_text) or not NUMBER_PATTERN.search(right_text):
        return False
    left_skeleton = NUMBER_PATTERN.sub("#", left_text)
    right_skeleton = NUMBER_PATTERN.sub("#", right_text)
    return ratio(left_skeleton, right_skeleton) >= 80


def _unique_anchors(
    baseline: tuple[ComparableUnit, ...], target: tuple[ComparableUnit, ...]
) -> list[tuple[int, int]]:
    base_exact: dict[str, list[int]] = {}
    target_exact: dict[str, list[int]] = {}
    for index, unit in enumerate(baseline):
        base_exact.setdefault(unit.match_text, []).append(index)
    for index, unit in enumerate(target):
        target_exact.setdefault(unit.match_text, []).append(index)
    candidates = [
        (base_positions[0], target_exact[text][0])
        for text, base_positions in base_exact.items()
        if len(text) >= 4
        and len(base_positions) == 1
        and len(target_exact.get(text, [])) == 1
    ]
    base_clause = {unit.clause_key: index for index, unit in enumerate(baseline) if unit.clause_key}
    target_clause = {unit.clause_key: index for index, unit in enumerate(target) if unit.clause_key}
    for key in set(base_clause) & set(target_clause):
        i, j = base_clause[key], target_clause[key]
        if ratio(baseline[i].match_text, target[j].match_text) >= 85:
            candidates.append((i, j))
    candidates = sorted(set(candidates))
    if not candidates:
        return []
    lengths = [1] * len(candidates)
    previous = [-1] * len(candidates)
    for index, (_, target_index) in enumerate(candidates):
        for earlier in range(index):
            if candidates[earlier][1] < target_index and lengths[earlier] + 1 > lengths[index]:
                lengths[index] = lengths[earlier] + 1
                previous[index] = earlier
    current = max(range(len(candidates)), key=lengths.__getitem__)
    result = []
    while current >= 0:
        result.append(candidates[current])
        current = previous[current]
    return list(reversed(result))


def _align_region(
    baseline: tuple[ComparableUnit, ...], target: tuple[ComparableUnit, ...]
) -> list[AlignmentStep]:
    rows, columns = len(baseline), len(target)
    negative = float("-inf")
    scores = [[negative] * (columns + 1) for _ in range(rows + 1)]
    back: list[list[tuple[int, int, int, int] | None]] = [
        [None] * (columns + 1) for _ in range(rows + 1)
    ]
    scores[0][0] = 0.0
    for i in range(rows + 1):
        for j in range(columns + 1):
            current = scores[i][j]
            if current == negative:
                continue
            if i < rows and current - 0.7 > scores[i + 1][j]:
                scores[i + 1][j] = current - 0.7
                back[i + 1][j] = (i, j, 1, 0)
            if j < columns and current - 0.7 > scores[i][j + 1]:
                scores[i][j + 1] = current - 0.7
                back[i][j + 1] = (i, j, 0, 1)
            for left_size in range(1, min(4, rows - i) + 1):
                left = baseline[i : i + left_size]
                for right_size in range(1, min(4, columns - j) + 1):
                    right = target[j : j + right_size]
                    similarity = ratio(_join_match(left), _join_match(right)) / 100
                    minimum = (
                        0.55
                        if _same_clause(left, right) or _same_numeric_context(left, right)
                        else 0.72
                    )
                    if similarity < minimum:
                        continue
                    reward = 1 + similarity - 0.08 * (left_size + right_size - 2)
                    next_score = current + reward
                    ni, nj = i + left_size, j + right_size
                    if next_score > scores[ni][nj]:
                        scores[ni][nj] = next_score
                        back[ni][nj] = (i, j, left_size, right_size)
    steps: list[AlignmentStep] = []
    i, j = rows, columns
    while i or j:
        pointer = back[i][j]
        if pointer is None:
            if i:
                pointer = (i - 1, j, 1, 0)
            else:
                pointer = (i, j - 1, 0, 1)
        pi, pj, left_size, right_size = pointer
        left = baseline[pi : pi + left_size]
        right = target[pj : pj + right_size]
        similarity = ratio(_join_match(left), _join_match(right)) / 100 if left and right else 0
        steps.append(AlignmentStep(left, right, similarity))
        i, j = pi, pj
    return list(reversed(steps))


def align_documents(
    baseline: ComparableDocument, target: ComparableDocument
) -> AlignmentOutcome:
    anchors = _unique_anchors(baseline.units, target.units)
    steps: list[AlignmentStep] = []
    base_start = target_start = 0
    for base_index, target_index in anchors:
        steps.extend(
            _align_region(
                baseline.units[base_start:base_index], target.units[target_start:target_index]
            )
        )
        left = (baseline.units[base_index],)
        right = (target.units[target_index],)
        steps.append(AlignmentStep(left, right, ratio(_join_match(left), _join_match(right)) / 100))
        base_start, target_start = base_index + 1, target_index + 1
    steps.extend(_align_region(baseline.units[base_start:], target.units[target_start:]))
    unmatched_base = sum(len(step.baseline) for step in steps if not step.target)
    unmatched_target = sum(len(step.target) for step in steps if not step.baseline)
    base_chars = sum(len(unit.match_text) for unit in baseline.units)
    target_chars = sum(len(unit.match_text) for unit in target.units)
    matched_base_chars = sum(
        sum(len(unit.match_text) for unit in step.baseline)
        for step in steps
        if step.baseline and step.target
    )
    matched_target_chars = sum(
        sum(len(unit.match_text) for unit in step.target)
        for step in steps
        if step.baseline and step.target
    )
    return AlignmentOutcome(
        steps=tuple(steps),
        coverage_baseline=matched_base_chars / base_chars if base_chars else 1.0,
        coverage_target=matched_target_chars / target_chars if target_chars else 1.0,
        unmatched_baseline=unmatched_base,
        unmatched_target=unmatched_target,
    )


def _page_flat(document: ComparableDocument) -> ComparableDocument | None:
    if not document.units or any(unit.page is None for unit in document.units):
        return None
    by_page: dict[int, list[ComparableUnit]] = {}
    for unit in document.units:
        assert unit.page is not None
        by_page.setdefault(unit.page, []).append(unit)
    units = []
    for order, (page, page_units) in enumerate(sorted(by_page.items())):
        locations = tuple(location for unit in page_units for location in unit.locations)
        raw = _join_raw(tuple(page_units))
        normalized, match_text = comparison_normalize(raw)
        units.append(
            ComparableUnit(
                unit_id=f"{document.source.file_id}_page_{page}",
                kind="PAGE",
                order=float(order),
                raw_text=raw,
                normalized_text=normalized,
                match_text=match_text,
                clause_key=None,
                locations=locations,
                confidence=_confidence(locations),
                page=page,
            )
        )
    return ComparableDocument(source=document.source, units=tuple(units))


def _segments(before: str, after: str) -> list[DiffSegment]:
    segments = []
    for operation, i1, i2, j1, j2 in SequenceMatcher(None, before, after).get_opcodes():
        if operation in {"equal", "delete", "replace"} and i1 != i2:
            segments.append(
                DiffSegment(
                    operation="EQUAL" if operation == "equal" else "DELETE", text=before[i1:i2]
                )
            )
        if operation in {"insert", "replace"} and j1 != j2:
            segments.append(DiffSegment(operation="INSERT", text=after[j1:j2]))
    return segments


def _side(document: ParsedDocument, units: tuple[ComparableUnit, ...]) -> DiffSide:
    locations = [location for unit in units for location in unit.locations]
    return DiffSide(
        file_id=document.file_id,
        location=locations[0],
        locations=locations,
        text=_join_raw(units),
    )


def _make_diff(
    index: int,
    diff_type: str,
    baseline: ParsedDocument,
    target: ParsedDocument,
    left: tuple[ComparableUnit, ...],
    right: tuple[ComparableUnit, ...],
    similarity: float,
    options: CompareOptionsLike,
) -> DiffItem:
    before, after = _join_raw(left), _join_raw(right)
    labels = {
        "ADDED": "目标文件新增内容",
        "DELETED": "目标文件缺少内容",
        "MODIFIED": "文字内容发生变化",
        "NUMERIC_CHANGED": "数值、金额、比例、日期或期限发生变化",
        "TABLE_ROW_ADDED": "目标表格新增行",
        "TABLE_ROW_DELETED": "目标表格缺少行",
        "TABLE_CELL_CHANGED": "表格单元格发生变化",
    }
    all_units = (*left, *right)
    ocr_confidences = [
        unit.confidence
        for unit in all_units
        if any(location.source == "OCR" for location in unit.locations)
    ]
    low_ocr = any(value < options.ocr_low_confidence_threshold for value in ocr_confidences)
    confidence = min([similarity, *ocr_confidences]) if ocr_confidences else similarity
    both_sides_ocr = bool(left and right) and all(
        any(location.source == "OCR" for location in unit.locations) for unit in all_units
    )
    review_reason = None
    if (
        both_sides_ocr
        and diff_type in {"MODIFIED", "TABLE_CELL_CHANGED"}
        and not _numeric_changed(before, after)
    ):
        matcher = SequenceMatcher(None, before, after)
        changed_characters = sum(
            (i2 - i1) + (j2 - j1)
            for operation, i1, i2, j1, j2 in matcher.get_opcodes()
            if operation != "equal"
        )
        before_match = comparison_normalize(before)[1]
        after_match = comparison_normalize(after)[1]
        parser_order_variance = (
            len(before_match) >= 20
            and len(before_match) == len(after_match)
            and Counter(before_match) == Counter(after_match)
        )
        tiny_variance = matcher.ratio() >= 0.98 and changed_characters <= 2
        placeholder_variance = tiny_variance and sum(
            keyword in f"{before} {after}" for keyword in PLACEHOLDER_CONTEXT
        ) >= 2
        if placeholder_variance:
            review_reason = "OCR_PLACEHOLDER_VARIANCE"
        elif parser_order_variance:
            review_reason = "OCR_READING_ORDER_VARIANCE"
        elif tiny_variance:
            review_reason = "OCR_SINGLE_CHAR_VARIANCE"
        elif low_ocr:
            review_reason = "OCR_LOW_CONFIDENCE_VARIANCE"
    return DiffItem(
        diff_id=f"diff_{index:06d}",
        diff_type=diff_type,
        title=labels[diff_type],
        baseline=_side(baseline, left) if left else None,
        target=_side(target, right) if right else None,
        segments=_segments(before, after) if left and right else [],
        confidence=round(max(0.0, min(confidence, 1.0)), 4),
        review_reason=review_reason,
    )


def _numeric_changed(before: str, after: str) -> bool:
    return NUMBER_PATTERN.findall(before) != NUMBER_PATTERN.findall(after) and bool(
        NUMBER_PATTERN.search(before) and NUMBER_PATTERN.search(after)
    )


def _candidate_diffs(
    outcome: AlignmentOutcome,
    baseline: ParsedDocument,
    target: ParsedDocument,
    options: CompareOptionsLike,
) -> list[DiffItem]:
    differences = []
    for step in outcome.steps:
        if step.baseline and step.target and _join_match(step.baseline) == _join_match(step.target):
            continue
        if step.baseline and step.target:
            diff_type = (
                "NUMERIC_CHANGED"
                if options.numeric_sensitive
                and _numeric_changed(_join_raw(step.baseline), _join_raw(step.target))
                else "MODIFIED"
            )
        elif step.baseline:
            diff_type = "DELETED"
        else:
            diff_type = "ADDED"
        differences.append(
            _make_diff(
                len(differences) + 1,
                diff_type,
                baseline,
                target,
                step.baseline,
                step.target,
                step.similarity if step.baseline and step.target else 1.0,
                options,
            )
        )
    return differences


def _row_key(row: ComparableTableRow) -> str:
    return comparison_normalize(row.cells[0].raw_text)[1] if row.cells else ""


def _comparable_table_row_unit(
    block: DocumentBlock, row: ComparableTableRow
) -> ComparableUnit:
    raw = " | ".join(cell.raw_text for cell in row.cells)
    normalized, match_text = comparison_normalize(raw)
    locations = tuple(location for cell in row.cells for location in cell.locations)
    locations = locations or (block.location,)
    return ComparableUnit(
        unit_id=f"{block.block_id}_rows_{'_'.join(map(str, row.source_rows))}",
        kind="TABLE_TEXT",
        order=block.order + (row.row + 1) / 1000,
        raw_text=raw,
        normalized_text=normalized,
        match_text=match_text,
        clause_key=None,
        locations=locations,
        confidence=_confidence(locations),
        page=next((location.page for location in locations if location.page is not None), None),
    )


def _table_unit(
    block: DocumentBlock, row: ComparableTableRow, column: int | None = None
) -> ComparableUnit:
    if column is None:
        return _comparable_table_row_unit(block, row)
    cell = row.cells[column]
    normalized, match_text = comparison_normalize(cell.raw_text)
    locations = cell.locations or tuple(
        location for candidate in row.cells for location in candidate.locations
    )
    locations = locations or (block.location,)
    return ComparableUnit(
        unit_id=f"{block.block_id}_r{'_'.join(map(str, row.source_rows))}_c{column}",
        kind="TABLE_CELL",
        order=block.order + (row.row + 1) / 1000 + column / 100000,
        raw_text=cell.raw_text,
        normalized_text=normalized,
        match_text=match_text,
        clause_key=None,
        locations=locations,
        confidence=_confidence(locations),
        page=next((location.page for location in locations if location.page is not None), None),
    )


def _compare_table_matches(
    matches: list[TableMatch],
    baseline: ParsedDocument,
    target: ParsedDocument,
    options: CompareOptionsLike,
    start: int,
) -> list[DiffItem]:
    differences = []
    for match in matches:
        assert match.baseline.table and match.target.table
        base_rows = _comparable_table_rows(match.baseline)
        target_rows = _comparable_table_rows(match.target)
        base_data, target_data = base_rows[1:], target_rows[1:]
        base_keys, target_keys = [_row_key(row) for row in base_data], [
            _row_key(row) for row in target_data
        ]
        keyed = (
            all(base_keys)
            and all(target_keys)
            and len(set(base_keys)) == len(base_keys)
            and len(set(target_keys)) == len(target_keys)
        )
        pairs: list[tuple[ComparableTableRow | None, ComparableTableRow | None]] = []
        if keyed:
            target_by_key = dict(zip(target_keys, target_data, strict=True))
            base_key_set = set(base_keys)
            pairs.extend(
                (row, target_by_key.get(key))
                for key, row in zip(base_keys, base_data, strict=True)
            )
            pairs.extend(
                (None, row)
                for key, row in zip(target_keys, target_data, strict=True)
                if key not in base_key_set
            )
        else:
            pairs.extend(
                (
                    base_data[index] if index < len(base_data) else None,
                    target_data[index] if index < len(target_data) else None,
                )
                for index in range(max(len(base_data), len(target_data)))
            )
        for base_row, target_row in pairs:
            if base_row is None and target_row is not None:
                differences.append(
                    _make_diff(
                        start + len(differences),
                        "TABLE_ROW_ADDED",
                        baseline,
                        target,
                        (),
                        (_table_unit(match.target, target_row),),
                        0.9,
                        options,
                    )
                )
                continue
            if target_row is None and base_row is not None:
                differences.append(
                    _make_diff(
                        start + len(differences),
                        "TABLE_ROW_DELETED",
                        baseline,
                        target,
                        (_table_unit(match.baseline, base_row),),
                        (),
                        0.9,
                        options,
                    )
                )
                continue
            assert base_row is not None and target_row is not None
            for column, (base_cell, target_cell) in enumerate(
                zip(base_row.cells, target_row.cells, strict=True)
            ):
                if comparison_normalize(base_cell.raw_text)[1] == comparison_normalize(
                    target_cell.raw_text
                )[1]:
                    continue
                differences.append(
                    _make_diff(
                        start + len(differences),
                        "TABLE_CELL_CHANGED",
                        baseline,
                        target,
                        (_table_unit(match.baseline, base_row, column),),
                        (_table_unit(match.target, target_row, column),),
                        0.95,
                        options,
                    )
                )
    return differences


def _all_document_text(document: ParsedDocument) -> str:
    parts = []
    for block in sorted(document.blocks, key=lambda item: item.order):
        if block.type == "TABLE" and block.table:
            parts.extend("|".join(cell.raw_text for cell in row.cells) for row in block.table.rows)
        else:
            parts.append(block.raw_text)
    return comparison_normalize("".join(parts))[1]


def aggregate_warnings(warnings: list[ProcessingWarning]) -> list[ProcessingWarning]:
    grouped: dict[str, list[ProcessingWarning]] = {}
    for warning in warnings:
        grouped.setdefault(warning.code, []).append(warning)
    result = []
    for _code, items in grouped.items():
        first = items[0].model_copy(deep=True)
        first.requires_manual_review = any(item.requires_manual_review for item in items)
        first.details = {**first.details, "count": len(items)}
        result.append(first)
    return result


def compare_documents_reliably(
    baseline: ParsedDocument, target: ParsedDocument, options: CompareOptionsLike
) -> ComparisonResult:
    table_matches, incompatible_tables = match_compatible_tables(baseline, target)
    base_structured = {
        match.baseline.table.table_index for match in table_matches if match.baseline.table
    }
    target_structured = {
        match.target.table.table_index for match in table_matches if match.target.table
    }
    base = build_comparable_document(
        baseline, options, structured_table_indexes=base_structured
    )
    compared_target = build_comparable_document(
        target, options, structured_table_indexes=target_structured
    )
    fallback_mode = "STRUCTURED"
    smaller = max(1, min(len(base.units), len(compared_target.units)))
    unit_ratio = max(len(base.units), len(compared_target.units)) / smaller
    if unit_ratio > 3:
        flat_base, flat_target = _page_flat(base), _page_flat(compared_target)
        if flat_base is not None and flat_target is not None:
            base, compared_target = flat_base, flat_target
            fallback_mode = "PAGE_FLAT"
    outcome = align_documents(base, compared_target)
    candidates = _candidate_diffs(outcome, baseline, target, options)
    candidates.extend(
        _compare_table_matches(
            table_matches, baseline, target, options, start=len(candidates) + 1
        )
    )
    global_similarity = ratio(_all_document_text(baseline), _all_document_text(target)) / 100
    base_count, target_count = len(base.units), len(compared_target.units)
    unmatched_base_ratio = outcome.unmatched_baseline / base_count if base_count else 0.0
    unmatched_target_ratio = outcome.unmatched_target / target_count if target_count else 0.0
    total_units = base_count + target_count
    reasons: list[str] = []
    shared_clause_keys = {
        unit.clause_key for unit in base.units if unit.clause_key
    } & {unit.clause_key for unit in compared_target.units if unit.clause_key}
    if global_similarity < 0.3 and not shared_clause_keys:
        reasons.append("DOCUMENT_PAIR_UNRELATED")
    if total_units >= 10 and (
        outcome.coverage_baseline < 0.7 or outcome.coverage_target < 0.7
    ):
        reasons.append("ALIGNMENT_UNRELIABLE")
    if unit_ratio > 3 and max(unmatched_base_ratio, unmatched_target_ratio) > 0.3:
        reasons.extend(["PARSER_STRUCTURE_MISMATCH", "ALIGNMENT_UNRELIABLE"])
    if global_similarity > 0.85 and len(candidates) > max(200, total_units * 0.5):
        reasons.append("ALIGNMENT_UNRELIABLE")
    reasons = list(dict.fromkeys(reasons))
    reliable = not {"DOCUMENT_PAIR_UNRELATED", "ALIGNMENT_UNRELIABLE"}.intersection(reasons)
    emitted = candidates if reliable else []
    warnings = [*baseline.warnings, *target.warnings]
    if incompatible_tables:
        warnings.append(
            ProcessingWarning(
                code="TABLE_STRUCTURE_INCOMPATIBLE",
                message="双方表格结构不兼容，已降级为阅读顺序文本比对",
                requires_manual_review=False,
                details={"table_count": incompatible_tables},
            )
        )
    if "PARSER_STRUCTURE_MISMATCH" in reasons:
        warnings.append(
            ProcessingWarning(
                code="PARSER_STRUCTURE_MISMATCH",
                message="双方解析结构粒度差异过大",
                details={"unit_ratio": round(unit_ratio, 4)},
            )
        )
    if "DOCUMENT_PAIR_UNRELATED" in reasons:
        warnings.append(
            ProcessingWarning(
                code="DOCUMENT_PAIR_UNRELATED",
                message="双方文档整体相似度过低，疑似文件配对错误",
                details={"global_text_similarity": round(global_similarity, 4)},
            )
        )
    if "ALIGNMENT_UNRELIABLE" in reasons:
        warnings.append(
            ProcessingWarning(
                code="ALIGNMENT_UNRELIABLE",
                message="文档对齐可靠性不足，候选差异未升级为业务风险",
                details={"candidate_diff_count": len(candidates)},
            )
        )
    if not options.ignore_formatting:
        warnings.append(
            ProcessingWarning(
                code="FORMATTING_COMPARISON_NOT_SUPPORTED",
                message="本阶段仅比较文字和基础表格内容，不比较字体、样式或版式",
            )
        )
    diagnostics = ComparisonDiagnostics(
        reliable=reliable,
        baseline_unit_count=base_count,
        target_unit_count=target_count,
        aligned_unit_count=sum(
            max(len(step.baseline), len(step.target))
            for step in outcome.steps
            if step.baseline and step.target
        ),
        unmatched_baseline_count=outcome.unmatched_baseline,
        unmatched_target_count=outcome.unmatched_target,
        alignment_coverage_baseline=round(outcome.coverage_baseline, 4),
        alignment_coverage_target=round(outcome.coverage_target, 4),
        unmatched_ratio_baseline=round(unmatched_base_ratio, 4),
        unmatched_ratio_target=round(unmatched_target_ratio, 4),
        global_text_similarity=round(global_similarity, 4),
        candidate_diff_count=len(candidates),
        emitted_diff_count=len(emitted),
        compatible_table_count=len(table_matches),
        fallback_mode=fallback_mode,
        reasons=reasons,
    )
    return ComparisonResult(
        diff_items=emitted,
        warnings=aggregate_warnings(warnings),
        diagnostics=diagnostics,
    )
