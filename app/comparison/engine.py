from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from rapidfuzz.fuzz import ratio

from app.comparison.models import ComparisonResult, DiffItem, DiffSegment, DiffSide
from app.documents.models import DocumentBlock, ParsedDocument, TableRow
from app.documents.normalization import normalize_text

NUMBER_PATTERN = re.compile(
    r"(?:\d{4}[年./-]\d{1,2}(?:[月./-]\d{1,2}日?)?|\d[\d,]*(?:\.\d+)?%?|[一二三四五六七八九十百千万]+(?:年|个月|月|日))"
)
CLAUSE_KEY = re.compile(r"^(第[一二三四五六七八九十百千万0-9]+条|\d+(?:\.\d+)*[、.])")
@dataclass(frozen=True)
class CompareOptions:
    ignore_formatting: bool = True
    ignore_headers_footers: bool = True
    numeric_sensitive: bool = True
    ocr_low_confidence_threshold: float = 0.8
    page_missing_min_equivalent: float = 0.8
    page_missing_min_anchor_similarity: float = 0.85
    page_missing_min_structure_units: int = 2


def _side(document: ParsedDocument, block: DocumentBlock) -> DiffSide:
    return DiffSide(file_id=document.file_id, location=block.location, text=block.raw_text)


def _segments(before: str, after: str) -> list[DiffSegment]:
    segments: list[DiffSegment] = []
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


def _make_diff(
    index: int,
    diff_type: str,
    baseline_document: ParsedDocument,
    target_document: ParsedDocument,
    baseline: DocumentBlock | None,
    target: DocumentBlock | None,
    confidence: float,
    ocr_low_confidence_threshold: float = 0.8,
) -> DiffItem:
    before = baseline.raw_text if baseline else ""
    after = target.raw_text if target else ""
    labels = {
        "ADDED": "目标文件新增内容",
        "DELETED": "目标文件缺少内容",
        "MODIFIED": "文字内容发生变化",
        "NUMERIC_CHANGED": "数值、金额、比例、日期或期限发生变化",
        "TABLE_ROW_ADDED": "目标表格新增行",
        "TABLE_ROW_DELETED": "目标表格缺少行",
        "TABLE_CELL_CHANGED": "表格单元格发生变化",
        "TABLE_STRUCTURE_EXPANDED": "模板表格结构发生变化",
    }
    locations = [block.location for block in (baseline, target) if block is not None]
    ocr_locations = [location for location in locations if location.source == "OCR"]
    ocr_confidences = [
        location.confidence for location in ocr_locations if location.confidence is not None
    ]
    if ocr_confidences:
        confidence = min(confidence, *ocr_confidences)
    return DiffItem(
        diff_id=f"diff_{index:06d}",
        diff_type=diff_type,
        title=labels[diff_type],
        baseline=_side(baseline_document, baseline) if baseline else None,
        target=_side(target_document, target) if target else None,
        segments=_segments(before, after) if baseline and target else [],
        confidence=round(confidence, 4),
    )


def _paragraphs(document: ParsedDocument, options: CompareOptions) -> list[DocumentBlock]:
    allowed = (
        {"PARAGRAPH"}
        if options.ignore_headers_footers
        else {"PARAGRAPH", "HEADER", "FOOTER", "SIDEBAR"}
    )
    return [block for block in document.blocks if block.type in allowed and block.normalized_text]


def _numeric_changed(before: str, after: str) -> bool:
    return NUMBER_PATTERN.findall(before) != NUMBER_PATTERN.findall(after) and bool(
        NUMBER_PATTERN.search(before) and NUMBER_PATTERN.search(after)
    )


def _compare_paragraphs(
    baseline_document: ParsedDocument,
    target_document: ParsedDocument,
    options: CompareOptions,
    start_index: int,
) -> list[DiffItem]:
    baseline = _paragraphs(baseline_document, options)
    target = _paragraphs(target_document, options)
    matcher = SequenceMatcher(
        None,
        [block.normalized_text for block in baseline],
        [block.normalized_text for block in target],
        autojunk=False,
    )
    differences: list[DiffItem] = []
    index = start_index
    for operation, i1, i2, j1, j2 in matcher.get_opcodes():
        if operation == "equal":
            continue
        before_group = baseline[i1:i2]
        after_group = target[j1:j2]
        paired = min(len(before_group), len(after_group))
        consumed_before = 0
        consumed_after = 0
        for offset in range(paired):
            before = before_group[offset]
            after = after_group[offset]
            similarity = ratio(before.normalized_text, after.normalized_text) / 100
            same_clause = bool(
                CLAUSE_KEY.match(before.normalized_text)
                and CLAUSE_KEY.match(before.normalized_text).group()
                == (
                    CLAUSE_KEY.match(after.normalized_text).group()
                    if CLAUSE_KEY.match(after.normalized_text)
                    else None
                )
            )
            minimum_similarity = 0.45 if same_clause else 0.5
            if similarity < minimum_similarity:
                break
            kind = (
                "NUMERIC_CHANGED"
                if options.numeric_sensitive and _numeric_changed(before.raw_text, after.raw_text)
                else "MODIFIED"
            )
            differences.append(
                _make_diff(
                    index,
                    kind,
                    baseline_document,
                    target_document,
                    before,
                    after,
                    similarity,
                    ocr_low_confidence_threshold=options.ocr_low_confidence_threshold,
                )
            )
            index += 1
            consumed_before += 1
            consumed_after += 1
        for before in before_group[consumed_before:]:
            differences.append(
                _make_diff(
                    index,
                    "DELETED",
                    baseline_document,
                    target_document,
                    before,
                    None,
                    1.0,
                    ocr_low_confidence_threshold=options.ocr_low_confidence_threshold,
                )
            )
            index += 1
        for after in after_group[consumed_after:]:
            differences.append(
                _make_diff(
                    index,
                    "ADDED",
                    baseline_document,
                    target_document,
                    None,
                    after,
                    1.0,
                    ocr_low_confidence_threshold=options.ocr_low_confidence_threshold,
                )
            )
            index += 1
    return differences


def _row_text(row: TableRow) -> str:
    return " | ".join(cell.raw_text for cell in row.cells)


def _table_header(block: DocumentBlock) -> str:
    if not block.table or not block.table.rows:
        return ""
    return " | ".join(cell.normalized_text for cell in block.table.rows[0].cells)


def _table_block(
    document: ParsedDocument,
    table_index: int,
    row: TableRow | None = None,
    column: int | None = None,
) -> DocumentBlock:
    text = _row_text(row) if row else ""
    location = (
        row.cells[column].location
        if row and column is not None and column < len(row.cells)
        else (row.cells[0].location if row and row.cells else document.blocks[0].location)
    )
    return DocumentBlock(
        block_id=f"{document.file_id}_table_{table_index}_{row.row if row else 0}_{column or 0}",
        type="TABLE",
        order=table_index,
        raw_text=(row.cells[column].raw_text if row and column is not None else text),
        normalized_text=normalize_text(text),
        location=location,
    )


def _compare_tables(
    baseline_document: ParsedDocument,
    target_document: ParsedDocument,
    start_index: int,
    options: CompareOptions,
) -> list[DiffItem]:
    base_tables = [
        block for block in baseline_document.blocks if block.type == "TABLE" and block.table
    ]
    target_tables = [
        block for block in target_document.blocks if block.type == "TABLE" and block.table
    ]
    differences: list[DiffItem] = []
    index = start_index
    unused_targets = set(range(len(target_tables)))

    def emit_rows(base_block: DocumentBlock | None, target_block: DocumentBlock | None) -> None:
        nonlocal index
        base = base_block.table if base_block else None
        target = target_block.table if target_block else None
        base_rows = base.rows if base else []
        target_rows = target.rows if target else []
        table_index = base.table_index if base else (target.table_index if target else 0)

        def emit_row_pair(base_row: TableRow | None, target_row: TableRow | None) -> None:
            nonlocal index
            if base_row is None:
                differences.append(
                    _make_diff(
                        index,
                        "TABLE_ROW_ADDED",
                        baseline_document,
                        target_document,
                        None,
                        _table_block(target_document, table_index, target_row),
                        0.9,
                        ocr_low_confidence_threshold=options.ocr_low_confidence_threshold,
                    )
                )
                index += 1
                return
            if target_row is None:
                differences.append(
                    _make_diff(
                        index,
                        "TABLE_ROW_DELETED",
                        baseline_document,
                        target_document,
                        _table_block(baseline_document, table_index, base_row),
                        None,
                        0.9,
                        ocr_low_confidence_threshold=options.ocr_low_confidence_threshold,
                    )
                )
                index += 1
                return
            for column in range(max(len(base_row.cells), len(target_row.cells))):
                before = (
                    base_row.cells[column].normalized_text if column < len(base_row.cells) else ""
                )
                after = (
                    target_row.cells[column].normalized_text
                    if column < len(target_row.cells)
                    else ""
                )
                if before == after:
                    continue
                base_cell_block = (
                    _table_block(baseline_document, table_index, base_row, column)
                    if column < len(base_row.cells)
                    else None
                )
                target_cell_block = (
                    _table_block(target_document, table_index, target_row, column)
                    if column < len(target_row.cells)
                    else None
                )
                differences.append(
                    _make_diff(
                        index,
                        "TABLE_CELL_CHANGED",
                        baseline_document,
                        target_document,
                        base_cell_block,
                        target_cell_block,
                        0.95,
                        ocr_low_confidence_threshold=options.ocr_low_confidence_threshold,
                    )
                )
                index += 1

        if not base_rows or not target_rows:
            for row in base_rows:
                emit_row_pair(row, None)
            for row in target_rows:
                emit_row_pair(None, row)
            return

        emit_row_pair(base_rows[0], target_rows[0])
        base_data = base_rows[1:]
        target_data = target_rows[1:]
        base_keys = [row.cells[0].normalized_text if row.cells else "" for row in base_data]
        target_keys = [row.cells[0].normalized_text if row.cells else "" for row in target_data]
        keyed = (
            all(base_keys)
            and all(target_keys)
            and len(set(base_keys)) == len(base_keys)
            and len(set(target_keys)) == len(target_keys)
        )
        if keyed:
            target_by_key = dict(zip(target_keys, target_data, strict=True))
            base_key_set = set(base_keys)
            for key, row in zip(base_keys, base_data, strict=True):
                emit_row_pair(row, target_by_key.get(key))
            for key, row in zip(target_keys, target_data, strict=True):
                if key not in base_key_set:
                    emit_row_pair(None, row)
        else:
            for row_index in range(max(len(base_data), len(target_data))):
                emit_row_pair(
                    base_data[row_index] if row_index < len(base_data) else None,
                    target_data[row_index] if row_index < len(target_data) else None,
                )

    for base_position, base_block in enumerate(base_tables):
        best_target = None
        best_score = -1.0
        for target_position in unused_targets:
            score = ratio(_table_header(base_block), _table_header(target_tables[target_position]))
            if score > best_score:
                best_score = score
                best_target = target_position
        if best_target is not None and (
            best_score >= 70 or (not _table_header(base_block) and best_target == base_position)
        ):
            unused_targets.remove(best_target)
            emit_rows(base_block, target_tables[best_target])
        else:
            emit_rows(base_block, None)
    for target_position in sorted(unused_targets):
        emit_rows(None, target_tables[target_position])
    return differences


def compare_documents(
    baseline: ParsedDocument, target: ParsedDocument, options: CompareOptions
) -> ComparisonResult:
    from app.comparison.reliable import compare_documents_reliably

    return compare_documents_reliably(baseline, target, options)


def is_ocr_review_only_diff(item: DiffItem) -> bool:
    return (
        item.diff_type != "NUMERIC_CHANGED"
        and item.review_reason is not None
    )
