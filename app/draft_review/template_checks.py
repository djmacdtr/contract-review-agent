from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.comparison.engine import NUMBER_PATTERN, CompareOptions, compare_documents
from app.comparison.models import ComparisonDiagnostics, DiffItem, DiffSide
from app.comparison.reliable import comparison_normalize
from app.documents.models import DocumentLocation, ParsedDocument, ProcessingWarning, TableCell

SYMBOLIC_PLACEHOLDER = re.compile(
    r"##\{[^{}\r\n]+\}|\{\{[^{}\r\n]+\}\}|\$\{[^{}\r\n]+\}|"
    r"[\[【]\s*(?:待填|填写|补充)[^\]】\r\n]*[\]】]"
)
BLANK_MARKER = re.compile(r"[_＿]{3,}|手动补充")
FILL_MARKER = re.compile(f"(?:{SYMBOLIC_PLACEHOLDER.pattern}|{BLANK_MARKER.pattern})")
REQUIRED_TABLE_LABELS = (
    "名称",
    "金额",
    "日期",
    "期限",
    "利率",
    "比例",
    "编号",
    "账号",
    "主体",
    "保证人",
    "地址",
    "付款",
)


class FilteredTemplateDiff(BaseModel):
    filter_reason: Literal["TEMPLATE_FILL_ALLOWED"]
    diff: DiffItem


class TemplateReviewDiagnostics(BaseModel):
    comparison: ComparisonDiagnostics
    raw_diff_count: int
    retained_diff_count: int
    filtered_diff_count: int
    filtered_diff_items: list[FilteredTemplateDiff] = Field(default_factory=list)
    expanded_table_count: int = 0
    coalesced_fill_count: int = 0


class TemplateReviewResult(BaseModel):
    diff_items: list[DiffItem]
    rule_checks: list[dict[str, Any]]
    warnings: list[ProcessingWarning]
    diagnostics: TemplateReviewDiagnostics

    @property
    def failed_rule_checks(self) -> list[dict[str, Any]]:
        return [item for item in self.rule_checks if item["status"] == "FAILED"]


def _location(file_id: str, location: DocumentLocation) -> dict[str, Any]:
    return {"file_id": file_id, **location.model_dump(mode="json", exclude_none=True)}


def _normalized(text: str) -> str:
    return comparison_normalize(text)[0]


def _has_unresolved(text: str) -> bool:
    return bool(FILL_MARKER.search(_normalized(text)))


def _matches_filled_template(template_text: str, target_text: str) -> bool:
    template = _normalized(template_text)
    target = _normalized(target_text)
    matches = list(FILL_MARKER.finditer(template))
    if not matches or not target or _has_unresolved(target):
        return False
    pieces = []
    cursor = 0
    for match in matches:
        pieces.append(re.escape(template[cursor : match.start()]))
        pieces.append(r".+?")
        cursor = match.end()
    pieces.append(re.escape(template[cursor:]))
    return re.fullmatch("".join(pieces), target, flags=re.DOTALL) is not None


def _allowed_fill_diff(diff: DiffItem) -> bool:
    if not diff.baseline or not diff.target:
        return False
    before, after = diff.baseline.text, diff.target.text
    if _matches_filled_template(before, after):
        return True
    return diff.diff_type == "TABLE_CELL_CHANGED" and not _normalized(before) and bool(
        _normalized(after)
    )


def _coalesce_positional_fills(differences: list[DiffItem]) -> tuple[list[DiffItem], int]:
    added_by_paragraph: dict[int, list[int]] = {}
    for index, diff in enumerate(differences):
        if (
            diff.diff_type == "ADDED"
            and diff.target
            and diff.target.location.paragraph_index is not None
        ):
            added_by_paragraph.setdefault(diff.target.location.paragraph_index, []).append(index)
    consumed: set[int] = set()
    replacements: dict[int, DiffItem] = {}
    for index, diff in enumerate(differences):
        if (
            diff.diff_type != "DELETED"
            or not diff.baseline
            or diff.baseline.location.paragraph_index is None
            or not _has_unresolved(diff.baseline.text)
        ):
            continue
        candidates = added_by_paragraph.get(diff.baseline.location.paragraph_index, [])
        candidate = next(
            (
                item_index
                for item_index in candidates
                if item_index not in consumed
                and differences[item_index].target
                and _matches_filled_template(
                    diff.baseline.text, differences[item_index].target.text
                )
            ),
            None,
        )
        if candidate is None:
            continue
        consumed.add(candidate)
        replacements[index] = diff.model_copy(
            update={
                "diff_type": "MODIFIED",
                "title": "模板允许填写区域已填写",
                "target": differences[candidate].target,
                "confidence": min(diff.confidence, differences[candidate].confidence),
                "requires_manual_review": False,
            }
        )
    result = [
        replacements.get(index, diff)
        for index, diff in enumerate(differences)
        if index not in consumed
    ]
    return result, len(replacements)


def _issue(
    code: str,
    index: int,
    name: str,
    file_id: str,
    location: DocumentLocation,
) -> dict[str, Any]:
    return {
        "rule_id": f"draft.{code}.{index:04d}",
        "rule_name": name,
        "status": "FAILED",
        "location": _location(file_id, location),
        "inputs": {"marker_type": code.upper()},
        "expected": "FILLED",
        "actual": "MISSING_OR_UNRESOLVED",
        "message": f"{name}，需要人工补充或确认。",
    }


def _unresolved_rules(target: ParsedDocument) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for block in sorted(target.blocks, key=lambda item: item.order):
        values: list[tuple[str, DocumentLocation]]
        if block.type == "TABLE" and block.table:
            values = [
                (cell.raw_text, cell.location)
                for row in block.table.rows
                for cell in row.cells
            ]
        else:
            values = [(block.raw_text, block.location)]
        for text, location in values:
            if SYMBOLIC_PLACEHOLDER.search(_normalized(text)):
                rules.append(
                    _issue(
                        "unresolved_placeholder",
                        len(rules) + 1,
                        "发现未替换占位符",
                        target.file_id,
                        location,
                    )
                )
            if BLANK_MARKER.search(_normalized(text)):
                rules.append(
                    _issue(
                        "unresolved_blank",
                        len(rules) + 1,
                        "发现疑似未填写空白",
                        target.file_id,
                        location,
                    )
                )
    return rules


def _cell_at(document: ParsedDocument) -> dict[tuple[int, int, int], TableCell]:
    result: dict[tuple[int, int, int], TableCell] = {}
    for block in document.blocks:
        if block.type != "TABLE" or not block.table:
            continue
        for row in block.table.rows:
            for position, cell in enumerate(row.cells):
                column = cell.location.column if cell.location.column is not None else position
                result[(block.table.table_index, row.row, column)] = cell
    return result


def _table_shapes(document: ParsedDocument) -> dict[int, tuple[int, tuple[int, ...]]]:
    shapes: dict[int, tuple[int, tuple[int, ...]]] = {}
    for block in document.blocks:
        if block.type != "TABLE" or not block.table:
            continue
        shapes[block.table.table_index] = (
            len(block.table.rows),
            tuple(len(row.cells) for row in block.table.rows),
        )
    return shapes


def _table_cell_diff(
    index: int,
    template: ParsedDocument,
    target: ParsedDocument,
    before: TableCell,
    after: TableCell,
) -> DiffItem:
    before_text = before.raw_text
    after_text = after.raw_text
    numeric_changed = (
        bool(NUMBER_PATTERN.search(before_text) and NUMBER_PATTERN.search(after_text))
        and NUMBER_PATTERN.findall(before_text) != NUMBER_PATTERN.findall(after_text)
    )
    return DiffItem(
        diff_id=f"draft_table_{index:06d}",
        diff_type="NUMERIC_CHANGED" if numeric_changed else "TABLE_CELL_CHANGED",
        title="模板表格固定单元格发生变化",
        baseline=DiffSide(
            file_id=template.file_id,
            location=before.location,
            locations=[before.location],
            text=before_text,
        ),
        target=DiffSide(
            file_id=target.file_id,
            location=after.location,
            locations=[after.location],
            text=after_text,
        ),
        confidence=0.95,
    )


def _compare_compatible_table_cells(
    template: ParsedDocument, target: ParsedDocument, start_index: int
) -> tuple[list[DiffItem], int]:
    template_shapes = _table_shapes(template)
    target_shapes = _table_shapes(target)
    compatible_indexes = {
        index
        for index, shape in template_shapes.items()
        if target_shapes.get(index) == shape
    }
    template_cells = _cell_at(template)
    target_cells = _cell_at(target)
    differences: list[DiffItem] = []
    for key in sorted(template_cells):
        if key[0] not in compatible_indexes or key not in target_cells:
            continue
        before = template_cells[key]
        after = target_cells[key]
        if _normalized(before.raw_text) == _normalized(after.raw_text):
            continue
        differences.append(
            _table_cell_diff(
                start_index + len(differences), template, target, before, after
            )
        )
    expanded = sum(
        1
        for index, shape in template_shapes.items()
        if index in target_shapes and target_shapes[index] != shape
    )
    expanded += len(set(template_shapes) ^ set(target_shapes))
    return differences, expanded


def _without_tables(document: ParsedDocument) -> ParsedDocument:
    return document.model_copy(
        update={"blocks": [block for block in document.blocks if block.type != "TABLE"]}
    )


def _required_empty_table_rules(
    template: ParsedDocument, target: ParsedDocument, start_index: int
) -> list[dict[str, Any]]:
    template_cells = _cell_at(template)
    target_cells = _cell_at(target)
    rules: list[dict[str, Any]] = []
    for (table_index, row, column), template_cell in template_cells.items():
        if column == 0:
            continue
        target_cell = target_cells.get((table_index, row, column))
        if target_cell is None or _normalized(target_cell.raw_text):
            continue
        previous = template_cells.get((table_index, row, column - 1))
        template_marker = _has_unresolved(template_cell.raw_text)
        labelled_empty = bool(
            previous
            and not _normalized(template_cell.raw_text)
            and any(keyword in previous.raw_text for keyword in REQUIRED_TABLE_LABELS)
        )
        if not template_marker and not labelled_empty:
            continue
        rules.append(
            _issue(
                "required_table_cell_empty",
                start_index + len(rules),
                "必填表格单元格为空",
                target.file_id,
                target_cell.location,
            )
        )
    return rules


def analyze_template(
    template: ParsedDocument,
    target: ParsedDocument,
    *,
    ignore_formatting: bool = True,
    ignore_headers_footers: bool = True,
    check_blank_fields: bool = True,
    ocr_low_confidence_threshold: float = 0.8,
) -> TemplateReviewResult:
    comparison = compare_documents(
        _without_tables(template),
        _without_tables(target),
        CompareOptions(
            ignore_formatting=ignore_formatting,
            ignore_headers_footers=ignore_headers_footers,
            numeric_sensitive=True,
            ocr_low_confidence_threshold=ocr_low_confidence_threshold,
        ),
    )
    table_differences, expanded_table_count = _compare_compatible_table_cells(
        template, target, start_index=len(comparison.diff_items) + 1
    )
    raw_differences, coalesced_fill_count = _coalesce_positional_fills(
        [*comparison.diff_items, *table_differences]
    )
    retained: list[DiffItem] = []
    filtered: list[FilteredTemplateDiff] = []
    for diff in raw_differences:
        if _allowed_fill_diff(diff):
            filtered.append(
                FilteredTemplateDiff(filter_reason="TEMPLATE_FILL_ALLOWED", diff=diff)
            )
        else:
            retained.append(diff)
    rule_checks: list[dict[str, Any]] = []
    if check_blank_fields:
        rule_checks = _unresolved_rules(target)
        rule_checks.extend(
            _required_empty_table_rules(template, target, start_index=len(rule_checks) + 1)
        )
    warnings = list(comparison.warnings)
    if expanded_table_count:
        warnings.append(
            ProcessingWarning(
                code="TEMPLATE_TABLE_STRUCTURE_EXPANDED",
                message="模板表格在目标合同中发生扩展，已跳过不可靠的逐单元格业务判断。",
                requires_manual_review=True,
                details={"count": expanded_table_count},
            )
        )
    return TemplateReviewResult(
        diff_items=retained,
        rule_checks=rule_checks,
        warnings=warnings,
        diagnostics=TemplateReviewDiagnostics(
            comparison=comparison.diagnostics,
            raw_diff_count=len(raw_differences),
            retained_diff_count=len(retained),
            filtered_diff_count=len(filtered),
            filtered_diff_items=filtered,
            expanded_table_count=expanded_table_count,
            coalesced_fill_count=coalesced_fill_count,
        ),
    )
