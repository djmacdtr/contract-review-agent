"""Controlled LLM validation for suspected FINAL_COMPARE V2 duplicates.

The deterministic comparator remains authoritative.  This module only finds
conservative duplicate clusters and lets the model choose whether a cluster
represents one logical difference.  Model failures never remove evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from functools import cache
from itertools import combinations
from typing import Any

from rapidfuzz.fuzz import partial_ratio, ratio

from app.adapters.llm.schemas import FinalCompareDuplicateClusterDecision
from app.comparison.logical_v2 import (
    _DIFF_PRIORITY,
    _candidate_id,
    _field_key,
    _table_pair_map,
)
from app.comparison.models import ComparisonResult, DiffItem, DiffSide
from app.comparison.reliable import _numeric_changed, comparison_normalize
from app.documents.models import ParsedDocument

_VALUE_TOKEN = re.compile(
    r"(?:\d{4}[年./-]\d{1,2}(?:[月./-]\d{1,2}日?)?"
    r"|\d[\d,]*(?:\.\d+)?%?"
    r"|[一二三四五六七八九十百千万]+(?:年|个月|月|日))"
)
_LOGICAL_CLUSTER_MAX_SIZE = 4
_EQUIVALENCE_CLUSTER_MAX_SIZE = 3
_CLUSTER_BATCH_SIZE = 4
_CLUSTER_RECOVERY_SIZE = 2
_CLUSTER_MAX_LOGICAL_CALLS = 8
_CLUSTER_CONFIDENCE = 0.95
_EQUIVALENT_CONFIDENCE = 0.98

# These patterns are used only for safe aggregate diagnostics.  The audit
# never returns the matched value or any source text.
_AMOUNT_CONTEXT = re.compile(r"(?:金额|价款|租金|租赁费|人民币|元|万元|亿元|￥|¥)")
_DATE_CONTEXT = re.compile(
    r"(?:日期|时间|季度|年度|\d{4}[年./-]\d{1,2}(?:[月./-]\d{1,2}日?)?|\d{1,2}月\d{1,2}日)"
)
_TERM_CONTEXT = re.compile(r"(?:期限|租期|租赁期|期数|个月|天|年)")
_IDENTIFIER_CONTEXT = re.compile(
    r"(?:编号|编码|合同号|协议号|统一社会信用代码|证件号|账号|[A-Za-z]{2,}[\d_-]{2,})"
)
_BUSINESS_FIELD_ALIASES = (
    "租赁利率",
    "每期租金支付日",
    "租金支付方式",
    "租金期数",
    "租金总额",
    "租金金额",
    "租金支付日",
    "支付日期",
    "支付方式",
    "付款方式",
    "收款方式",
    "每期租金",
    "租赁期限",
    "租赁期间",
    "收款账户",
    "保证金",
    "手续费",
    "保险",
    "特别约定",
    "租金",
    "利率",
    "租赁物",
    "租赁物使用地点",
    "项目名称",
    "设备名称",
    "名称",
    "金额",
    "总额",
    "日期",
    "数量",
    "期数",
    "编号",
    "序号",
)
_BUSINESS_FIELD_CANONICAL = {
    "租赁利率": "利率",
    "每期租金支付日": "租金支付日",
    "租金支付方式": "支付方式",
    "租金期数": "期数",
    "租金总额": "总额",
    "租金金额": "每期租金",
    "支付日期": "租金支付日",
    "付款方式": "支付方式",
    "收款方式": "支付方式",
}
_SUBNUMBER_PREFIX = re.compile(
    r"^(?:[\(（\[]?\s*[0-9一二三四五六七八九十百千万]+"
    r"\s*[\)）\].、．.]|第[0-9一二三四五六七八九十百千万]+条)"
)

_EQUIVALENCE_REJECTION_CODES = (
    "COORDINATE_MISMATCH",
    "FIELD_MISMATCH",
    "VALUE_MISMATCH",
    "COMPOSITE_EQUIVALENCE_REQUIRED",
    "BOUNDARY_EVIDENCE_MISSING",
    "EQUIVALENT_COMPONENT_OVERMERGED",
)
_FORMULA_MARKERS = re.compile(r"(?:公式|计算|比例|利率|费率|[=＋+\-*/×÷%％])")
_FORMULA_VALUE = re.compile(
    r"(?:\d+(?:\.\d+)?\s*[％%]|\d+(?:\.\d+)?\s*(?:个月|年|期|元|万元|亿元))"
)
_COMMON_BUSINESS_WORDS = frozenset(
    {
        "合同",
        "内容",
        "变更",
        "发生",
        "项目",
        "金额",
        "数值",
        "公式",
        "计算",
        "其中",
        "按照",
        "应当",
        "不得",
    }
)
_UNIQUE_EQUIVALENCE_FIELDS = frozenset(
    {
        "期数",
        "租赁期限",
        "租金支付日",
        "支付方式",
        "总额",
        "合计",
        "每期租金",
        "收款账户",
        "利率",
        "手续费",
        "保险",
        "保证金",
    }
)

# A logical change may be emitted as an add/delete pair or as a value change
# plus a table-cell change. These are the only cross-type combinations that
# are meaningful for a candidate group. Missing-content records stay within
# their own family and cannot bridge an ordinary paragraph or table row.
_LOGICAL_VALUE_TYPES = frozenset(
    {"MODIFIED", "NUMERIC_CHANGED", "TABLE_CELL_CHANGED"}
)
_LOGICAL_ADD_TYPES = frozenset({"ADDED", "TABLE_ROW_ADDED"})
_LOGICAL_DELETE_TYPES = frozenset({"DELETED", "TABLE_ROW_DELETED"})
_LOGICAL_MISSING_TYPES = frozenset({"PAGE_MISSING", "CONTENT_BLOCK_MISSING"})


def _logical_diff_types_compatible(left: str, right: str) -> bool:
    """Return whether two emitted facts can describe one logical change."""

    if left == right:
        return True
    if {left, right} <= _LOGICAL_VALUE_TYPES:
        return True
    if (left in _LOGICAL_VALUE_TYPES and right in (_LOGICAL_ADD_TYPES | _LOGICAL_DELETE_TYPES)) or (
        right in _LOGICAL_VALUE_TYPES
        and left in (_LOGICAL_ADD_TYPES | _LOGICAL_DELETE_TYPES)
    ):
        return True
    if left in _LOGICAL_ADD_TYPES and right in _LOGICAL_DELETE_TYPES:
        return True
    if left in _LOGICAL_DELETE_TYPES and right in _LOGICAL_ADD_TYPES:
        return True
    return left in _LOGICAL_MISSING_TYPES and right in _LOGICAL_MISSING_TYPES


@dataclass(frozen=True)
class DuplicateCluster:
    cluster_id: str
    candidate_ids: tuple[str, ...]
    payload: dict[str, Any]
    relation_reason: str = "LOGICAL_COORDINATE_MATCH"
    discovery_action: str = "SAME_LOGICAL_CHANGE"

    @property
    def group_id(self) -> str:
        """The V2 name for this internal logical candidate group."""

        digest = self.cluster_id.removeprefix("cluster_")
        return f"group_{digest}"

    @property
    def canary_category(self) -> str:
        """Return a safe, deterministic category used by the local Canary gate."""

        explicit = self.payload.get("canary_category")
        if isinstance(explicit, str) and explicit:
            return explicit
        if self.discovery_action == "EQUIVALENT_NO_CHANGE":
            return (
                "TABLE_FIELD_EQUIVALENCE"
                if self.payload.get("candidate_coordinate_kind") == "TABLE"
                else "FORMULA_EQUIVALENCE"
            )
        return (
            "TABLE_MERGE"
            if self.payload.get("candidate_coordinate_kind") == "TABLE"
            else "PARAGRAPH_MERGE"
        )


def _locations(side: DiffSide | None) -> list[Any]:
    if side is None:
        return []
    return list(side.locations or [side.location])


def _normalized(side: DiffSide | None) -> str | None:
    if side is None:
        return None
    return comparison_normalize(side.text)[1]


def _table_indexes(side: DiffSide | None) -> set[int]:
    return {
        location.table_index
        for location in _locations(side)
        if location.table_index is not None
    }


def _pages(side: DiffSide | None) -> set[int]:
    return {
        location.page
        for location in _locations(side)
        if location.page is not None
    }


def _value_tokens(text: str | None) -> tuple[str, ...]:
    return tuple(_VALUE_TOKEN.findall(text or ""))


def _business_field_key(text: str | None) -> str:
    normalized = comparison_normalize(text or "")[1]
    matches = [
        (normalized.find(alias), -len(alias), alias)
        for alias in _BUSINESS_FIELD_ALIASES
        if alias in normalized
    ]
    if matches:
        _, _, alias = min(matches)
        return _field_key(_BUSINESS_FIELD_CANONICAL.get(alias, alias))
    return ""


def _business_field_keys(text: str | None) -> tuple[str, ...]:
    """Return all field aliases in a cell, preferring the leading label.

    A complete OCR row can contain both a group label (``租金``) and a value
    label (``租赁利率``).  Coordinate construction uses the cell-local first
    label, while this helper is useful for tests and diagnostics that need the
    complete alias set without treating a group label as the value field.
    """

    normalized = comparison_normalize(text or "")[1]
    matches = {
        _field_key(_BUSINESS_FIELD_CANONICAL.get(alias, alias))
        for alias in _BUSINESS_FIELD_ALIASES
        if alias in normalized
    }
    return tuple(sorted(matches))


def _audit_location_key(location: Any) -> tuple[Any, ...]:
    return (
        getattr(location, "page", None),
        getattr(location, "paragraph_index", None),
        getattr(location, "table_index", None),
        getattr(location, "row", None),
        getattr(location, "column", None),
        getattr(location, "section", None),
    )


def _audit_side_locations(side: DiffSide | None) -> tuple[tuple[Any, ...], ...]:
    if side is None:
        return ()
    return tuple(sorted({_audit_location_key(item) for item in _locations(side)}))


def _audit_diff_signature(diff: DiffItem) -> tuple[Any, ...]:
    return (
        diff.diff_type,
        diff.baseline.file_id if diff.baseline else None,
        _normalized(diff.baseline),
        _audit_side_locations(diff.baseline),
        diff.target.file_id if diff.target else None,
        _normalized(diff.target),
        _audit_side_locations(diff.target),
    )


def _audit_position_signature(diff: DiffItem) -> tuple[Any, ...]:
    return (
        diff.baseline.file_id if diff.baseline else None,
        _audit_side_locations(diff.baseline),
        diff.target.file_id if diff.target else None,
        _audit_side_locations(diff.target),
    )


def _audit_has_context(diff: DiffItem, pattern: re.Pattern[str]) -> bool:
    text = " ".join(
        side.text for side in (diff.baseline, diff.target) if side is not None
    )
    return bool(pattern.search(text))


def _audit_texts_differ(diff: DiffItem) -> bool:
    return bool(
        diff.baseline
        and diff.target
        and _normalized(diff.baseline) != _normalized(diff.target)
    )


def build_v2_quality_audit(
    comparison: ComparisonResult,
    *,
    page_coverage: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Return safe, zero-body diagnostics for a FINAL_LOGICAL_V2 replay.

    The audit is deliberately independent from the duplicate-cluster LLM.  It
    describes what the deterministic comparator produced and can therefore be
    used as a dry-run gate without external calls or database writes.
    """

    diffs = list(comparison.diff_items)
    signatures = Counter(_audit_diff_signature(diff) for diff in diffs)
    position_types: dict[tuple[Any, ...], set[str]] = {}
    for diff in diffs:
        position_types.setdefault(_audit_position_signature(diff), set()).add(diff.diff_type)
    cross_type_groups = [types for types in position_types.values() if len(types) > 1]
    bilateral_text_different_count = sum(_audit_texts_differ(diff) for diff in diffs)
    structure_uncertain_count = sum(
        diff.validation_reason_code
        in {"TABLE_STRUCTURE_AMBIGUOUS", "TABLE_CANDIDATE_AMBIGUOUS"}
        for diff in diffs
    )
    structure_uncertain_count += sum(
        int(
            warning.code in {
                "FINAL_COMPARE_CANDIDATES_REVIEW_REQUIRED",
                "TABLE_CANDIDATES_REVIEW_REQUIRED",
            }
            and isinstance(warning.details.get("candidate_count"), int)
        )
        * int(warning.details.get("candidate_count", 0))
        for warning in comparison.warnings
    )

    audit: dict[str, Any] = {
        "candidate_count": len(diffs),
        "exact_duplicate_signature_count": sum(count > 1 for count in signatures.values()),
        "exact_duplicate_excess_count": sum(max(0, count - 1) for count in signatures.values()),
        "same_position_cross_type_group_count": len(cross_type_groups),
        "same_position_cross_type_excess_count": sum(
            len(types) - 1 for types in cross_type_groups
        ),
        "table_structure_uncertain_count": structure_uncertain_count,
        "review_required_count": sum(
            diff.validation_status == "REVIEW_REQUIRED" for diff in diffs
        ),
        "bilateral_normalized_text_different_count": bilateral_text_different_count,
        "amount_change_count": sum(
            _audit_texts_differ(diff)
            and _audit_has_context(diff, _AMOUNT_CONTEXT)
            for diff in diffs
        ),
        "date_change_count": sum(
            _audit_texts_differ(diff)
            and _audit_has_context(diff, _DATE_CONTEXT)
            for diff in diffs
        ),
        "term_change_count": sum(
            _audit_texts_differ(diff)
            and _audit_has_context(diff, _TERM_CONTEXT)
            for diff in diffs
        ),
        "identifier_change_count": sum(
            _audit_texts_differ(diff)
            and _audit_has_context(diff, _IDENTIFIER_CONTEXT)
            for diff in diffs
        ),
        "numeric_change_count": sum(diff.diff_type == "NUMERIC_CHANGED" for diff in diffs),
        "aligned_unit_count": comparison.diagnostics.aligned_unit_count,
        "compatible_table_count": comparison.diagnostics.compatible_table_count,
        "unmatched_baseline_count": comparison.diagnostics.unmatched_baseline_count,
        "unmatched_target_count": comparison.diagnostics.unmatched_target_count,
        "alignment_coverage_baseline": comparison.diagnostics.alignment_coverage_baseline,
        "alignment_coverage_target": comparison.diagnostics.alignment_coverage_target,
        "reliable": comparison.diagnostics.reliable,
    }
    for key in (
        "sparse_column_alignment_count",
        "vertical_merge_continuation_count",
        "key_value_row_alignment_count",
        "table_mismatch_excluded_count",
    ):
        audit[key] = int(comparison.validation_stats.get(key, 0))
    discovery = comparison.validation_metadata.get("candidate_discovery", {})
    audit["equivalence_rejection_counts"] = discovery.get(
        "equivalence_rejection_counts", {}
    )
    audit["boundary_noise_rejection_count"] = int(
        discovery.get("boundary_noise_rejection_count", 0)
    )
    if page_coverage is not None:
        audit["page_evidence"] = {
            key: int(page_coverage.get(key, 0))
            for key in (
                "required_evidence_count",
                "covered_evidence_count",
                "missing_evidence_count",
            )
        }
    return audit


def _safe_text(text: str | None) -> dict[str, Any]:
    value = text or ""
    return {
        "chars": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16],
    }


def _gold_manifest_digest(manifest: dict[str, Any]) -> str:
    value = {
        key: item for key, item in manifest.items() if key != "manifest_sha256"
    }
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _location_payload(side: DiffSide | None) -> list[dict[str, Any]]:
    return [
        {
            "page": location.page,
            "table_index": location.table_index,
            "row": location.row,
            "column": location.column,
            "bbox": location.bbox,
        }
        for location in _locations(side)
    ]


def _side_payload(side: DiffSide | None, document: ParsedDocument | None) -> dict[str, Any] | None:
    if side is None:
        return None
    payload: dict[str, Any] = {
        "file_id": side.file_id,
        "text": side.text,
        "locations": _location_payload(side),
    }
    if document is not None:
        contexts: list[dict[str, Any]] = []
        for location in _locations(side):
            if location.table_index is None or location.row is None:
                continue
            table = next(
                (
                    block.table
                    for block in document.blocks
                    if block.table is not None
                    and block.table.table_index == location.table_index
                ),
                None,
            )
            if table is None or location.row >= len(table.rows):
                continue
            row = table.rows[location.row]
            contexts.append(
                {
                    "header": (
                        table.rows[0].cells[location.column].raw_text
                        if table.rows
                        and location.column is not None
                        and location.column < len(table.rows[0].cells)
                        else None
                    ),
                    "row_context": [cell.raw_text for cell in row.cells],
                }
            )
        if contexts:
            payload["contexts"] = contexts
    return payload


def _axis_values(side: DiffSide | None) -> list[tuple[int, int, int]]:
    values: list[tuple[int, int, int]] = []
    for location in _locations(side):
        if (
            location.page is None
            or location.table_index is None
            or location.row is None
            or location.column is None
        ):
            continue
        values.append((location.page, location.row, location.column))
    return values


def _near_or_overlapping(left: DiffSide | None, right: DiffSide | None) -> bool:
    left_values, right_values = _axis_values(left), _axis_values(right)
    if not left_values or not right_values:
        return False
    if _table_indexes(left) != _table_indexes(right):
        return False
    for left_page, left_row, left_column in left_values:
        for right_page, right_row, right_column in right_values:
            if abs(left_page - right_page) <= 1 and (
                (left_column == right_column and abs(left_row - right_row) <= 1)
                or (left_row == right_row and abs(left_column - right_column) <= 1)
            ):
                return True
    left_boxes = [location.bbox for location in _locations(left) if location.bbox]
    right_boxes = [location.bbox for location in _locations(right) if location.bbox]
    for left_box in left_boxes:
        for right_box in right_boxes:
            if len(left_box) < 4 or len(right_box) < 4:
                continue
            if abs(left_box[0] - right_box[0]) <= 30 and abs(left_box[1] - right_box[1]) <= 30:
                return True
    return False


def _same_candidate_region(
    left: DiffItem,
    right: DiffItem,
    *,
    baseline: ParsedDocument | None = None,
    target: ParsedDocument | None = None,
) -> bool:
    if (
        left.baseline is None
        or right.baseline is None
        or left.target is None
        or right.target is None
    ):
        return False
    if (
        left.baseline.file_id != right.baseline.file_id
        or left.target.file_id != right.target.file_id
    ):
        return False
    if not _same_matched_table_pair(
        left, right, baseline=baseline, target=target
    ):
        return False
    left_pages = _pages(left.baseline) | _pages(left.target)
    right_pages = _pages(right.baseline) | _pages(right.target)
    if (
        left_pages
        and right_pages
        and min(abs(a - b) for a in left_pages for b in right_pages) > 1
    ):
        return False
    if not (
        _near_or_overlapping(left.baseline, right.baseline)
        or _near_or_overlapping(left.target, right.target)
    ):
        return False
    return True


def _same_matched_table_pair(
    left: DiffItem,
    right: DiffItem,
    *,
    baseline: ParsedDocument | None,
    target: ParsedDocument | None,
) -> bool:
    """Check table identity without assuming both parsers use the same index."""

    left_tables = _table_indexes(left.baseline)
    right_tables = _table_indexes(left.target)
    other_left_tables = _table_indexes(right.baseline)
    other_right_tables = _table_indexes(right.target)
    if left_tables != other_left_tables or right_tables != other_right_tables:
        return False
    if baseline is None or target is None:
        return left_tables == other_left_tables and right_tables == other_right_tables
    table_pairs = _table_pair_map(baseline, target)
    return all(table_pairs.get(table_index) in right_tables for table_index in left_tables)


def _same_text_and_type(left: DiffItem, right: DiffItem) -> bool:
    if _normalized(left.baseline) != _normalized(right.baseline):
        return False
    if _normalized(left.target) != _normalized(right.target):
        return False
    if (
        left.baseline
        and left.target
        and _numeric_changed(left.baseline.text, left.target.text)
    ):
        return False
    if (
        right.baseline
        and right.target
        and _numeric_changed(right.baseline.text, right.target.text)
    ):
        return False
    left_priority = _DIFF_PRIORITY.get(left.diff_type, 99)
    right_priority = _DIFF_PRIORITY.get(right.diff_type, 99)
    return left.diff_type == right.diff_type or abs(left_priority - right_priority) <= 1


def _side_axis(side: DiffSide | None) -> list[tuple[int | None, int | None, int | None]]:
    if side is None:
        return []
    return [
        (
            getattr(location, "paragraph_index", None),
            getattr(location, "row", None),
            getattr(location, "column", None),
        )
        for location in _locations(side)
    ]


def _strip_clause_number(text: str | None) -> str:
    value = comparison_normalize(text or "")[1]
    return re.sub(r"^(?:第[一二三四五六七八九十百千万0-9]+条|[0-9]+(?:\.[0-9]+)*[、.])", "", value)


def _candidate_value_tokens(diff: DiffItem) -> tuple[str, ...]:
    return tuple(
        sorted(
            set(_value_tokens(diff.baseline.text if diff.baseline else None))
            | set(_value_tokens(diff.target.text if diff.target else None))
        )
    )


def _raw_table_row(document: ParsedDocument, table_index: int, row_number: int) -> Any:
    for block in document.blocks:
        if block.table is not None and block.table.table_index == table_index:
            return next((row for row in block.table.rows if row.row == row_number), None)
    return None


def _raw_table_cell(
    document: ParsedDocument, table_index: int, row_number: int, column: int
) -> Any:
    row = _raw_table_row(document, table_index, row_number)
    if row is None:
        return None
    return next(
        (
            cell
            for ordinal, cell in enumerate(row.cells)
            if (cell.location.column if cell.location.column is not None else ordinal)
            == column
        ),
        None,
    )


def _key_value_group_label(
    document: ParsedDocument, table_index: int, row_number: int
) -> str:
    """Resolve a key/value table's repeated or blank group label safely."""

    table = next(
        (
            block.table
            for block in document.blocks
            if block.table is not None and block.table.table_index == table_index
        ),
        None,
    )
    if table is None:
        return ""
    group_column = min(
        (
            cell.location.column
            for row in table.rows
            for cell in row.cells
            if cell.location.column is not None
        ),
        default=0,
    )
    for candidate_row in range(row_number, -1, -1):
        row = _raw_table_row(document, table_index, candidate_row)
        if row is None:
            continue
        first_cell = next(
            (
                cell
                for cell in row.cells
                if (cell.location.column if cell.location.column is not None else 0)
                == group_column
            ),
            None,
        )
        field = _business_field_key(first_cell.raw_text) if first_cell else ""
        if field:
            return field
    return ""


def _key_value_row_key(
    document: ParsedDocument, table_index: int, row_number: int
) -> str:
    row = _raw_table_row(document, table_index, row_number)
    if row is None:
        return ""
    field = _key_value_group_label(document, table_index, row_number)
    if not field:
        return ""
    table = next(
        (
            block.table
            for block in document.blocks
            if block.table is not None and block.table.table_index == table_index
        ),
        None,
    )
    first_column = min(
        (
            cell.location.column
            for table_row in (table.rows if table is not None else [])
            for cell in table_row.cells
            if cell.location.column is not None
        ),
        default=0,
    )
    value_cells = sorted(
        (
            cell
            for cell in row.cells
            if (cell.location.column if cell.location.column is not None else 0)
            > first_column
            and comparison_normalize(cell.raw_text)[1]
        ),
        key=lambda item: item.location.column
        if item.location.column is not None
        else 0,
    )
    for cell in value_cells:
        value = comparison_normalize(cell.raw_text)[1]
        match = _SUBNUMBER_PREFIX.match(value)
        if match:
            item_field = _business_field_key(value)
            if item_field and item_field != field:
                return f"KV:{field}|FIELD:{item_field}|SUB:{match.group(0)}"
            return f"KV:{field}|SUB:{match.group(0)}"
        item_field = _business_field_key(value)
        if item_field and item_field != field:
            return f"KV:{field}|FIELD:{item_field}"
    return f"KV:{field}"


def _key_value_group_from_row_key(row_key: str) -> str:
    if not row_key.startswith("KV:"):
        return ""
    return row_key[3:].split("|", 1)[0]


def _key_value_rows_are_compatible(
    left_rows: set[str], right_rows: set[str], fields: set[str]
) -> bool:
    """Keep key/value candidates in the same business section.

    A shifted rent row may lose its group-label cell, so ``租金`` and the
    field name are compatible fallbacks.  Distinct groups such as ``保证金``
    and ``租金`` are not: the shared field ``收款账户`` alone is insufficient
    evidence to join them.
    """

    if not left_rows or not right_rows or left_rows & right_rows:
        return True
    left_groups = {
        _key_value_group_from_row_key(value)
        for value in left_rows
        if _key_value_group_from_row_key(value)
    }
    right_groups = {
        _key_value_group_from_row_key(value)
        for value in right_rows
        if _key_value_group_from_row_key(value)
    }
    if not left_groups or not right_groups:
        return True
    allowed_fallbacks = {"租金", *fields}
    return bool(left_groups & right_groups) or bool(
        left_groups <= allowed_fallbacks and right_groups <= allowed_fallbacks
    )


def _table_pair_key(
    side: DiffSide | None,
    *,
    is_baseline: bool,
    table_pairs: dict[int, int],
) -> tuple[tuple[int | None, int | None], ...]:
    table_indexes = sorted(_table_indexes(side))
    if is_baseline:
        return tuple((index, table_pairs.get(index)) for index in table_indexes)
    reverse = {target: baseline for baseline, target in table_pairs.items()}
    return tuple((reverse.get(index), index) for index in table_indexes)


def _table_side_coordinate(
    side: DiffSide | None,
    document: ParsedDocument | None,
    *,
    is_baseline: bool,
    table_pairs: dict[int, int],
) -> dict[str, Any] | None:
    if side is None or not _table_indexes(side):
        return None
    table_pair = _table_pair_key(
        side, is_baseline=is_baseline, table_pairs=table_pairs
    )
    fields: set[str] = set()
    row_keys: set[str] = set()
    logical_ids: set[str] = set()
    for location in _locations(side):
        if (
            document is None
            or location.table_index is None
            or location.row is None
            or location.column is None
        ):
            continue
        cell = _raw_table_cell(
            document, location.table_index, location.row, location.column
        )
        table = next(
            (
                block.table
                for block in document.blocks
                if block.table is not None
                and block.table.table_index == location.table_index
            ),
            None,
        )
        if cell is not None and cell.logical_cell_id:
            logical_ids.add(cell.logical_cell_id)
        if table is None or not table.rows:
            continue
        header_row = next(
            (
                row
                for row in table.rows
                if sum(
                    _field_key(cell.raw_text)
                    in {
                        "序号",
                        "编号",
                        "代码",
                        "名称",
                        "项目",
                        "设备",
                        "型号",
                        "位置",
                        "单位",
                        "数量",
                        "金额",
                        "期数",
                        "租金支付日",
                        "每期租金",
                        "合计",
                    }
                    for cell in row.cells
                )
                >= 2
            ),
            None,
        )
        if header_row is None:
            # A table without a reliable header must not turn an arbitrary
            # metadata/value cell into a column coordinate.  Key/value
            # attachments are different: their first non-empty cell is the
            # business field label and is safe to use as a row coordinate.
            row = _raw_table_row(document, location.table_index, location.row)
            first_cell = min(
                (
                    cell
                    for cell in (row.cells if row is not None else [])
                    if comparison_normalize(cell.raw_text)[1]
                ),
                key=lambda item: item.location.column
                if item.location.column is not None
                else 0,
                default=None,
            )
            if first_cell is not None:
                field = _key_value_group_label(
                    document, location.table_index, location.row
                )
                if field:
                    row = _raw_table_row(document, location.table_index, location.row)
                    value_fields = {
                        _business_field_key(cell.raw_text)
                        for cell in (row.cells if row is not None else [])
                        if _business_field_key(cell.raw_text)
                        and _business_field_key(cell.raw_text) != field
                    }
                    fields.update(value_fields or {field})
                    row_keys.add(
                        _key_value_row_key(
                            document, location.table_index, location.row
                        )
                    )
            continue
        header_cell = next(
            (
                header
                for ordinal, header in enumerate(header_row.cells)
                if (header.location.column if header.location.column is not None else ordinal)
                == location.column
            ),
            None,
        )
        if header_cell is not None:
            field = _business_field_key(header_cell.raw_text) or _field_key(
                header_cell.raw_text
            )
            if field:
                fields.add(field)
        row = _raw_table_row(document, location.table_index, location.row)
        if row is not None:
            identity_parts: list[str] = []
            header_by_column = {
                (
                    header.location.column
                    if header.location.column is not None
                    else ordinal
                ): _field_key(header.raw_text)
                for ordinal, header in enumerate(header_row.cells)
            }
            for ordinal, row_cell in enumerate(row.cells):
                column = (
                    row_cell.location.column
                    if row_cell.location.column is not None
                    else ordinal
                )
                header = header_by_column.get(column, "")
                if header in {"序号", "编号", "代码", "名称", "项目", "设备", "型号"}:
                    normalized = comparison_normalize(row_cell.raw_text)[1]
                    if normalized:
                        identity_parts.append(f"{header}:{normalized}")
            if identity_parts:
                row_keys.add("|".join(identity_parts))
    return {
        "table_pair": table_pair,
        "fields": tuple(sorted(fields)),
        "row_keys": tuple(sorted(row_keys)),
        "logical_ids": tuple(sorted(logical_ids)),
    }


_CHAPTER_HEADING = re.compile(
    r"^(?:第[一二三四五六七八九十百千万0-9]+[章节条]\s*[^。；;]{0,70}"
    r"|附件[一二三四五六七八九十百千万0-9]+\s*[^。；;]{0,50}"
    r"|通用条款|租赁附表)$"
)


def _chapter_heading(text: str | None) -> str | None:
    normalized = comparison_normalize(text or "")[1]
    if not normalized or len(normalized) > 100:
        return None
    if not _CHAPTER_HEADING.match(normalized):
        # Small parser-provided section labels are useful in fixtures and in
        # documents whose headings are not prefixed with ``第...条``.  Never
        # accept a sentence-like section value: long OCR content in the
        # ``section`` field is precisely the historical source of false
        # chapter mismatches.
        if (
            len(normalized) > 40
            or re.search(r"[，。；;：:]", normalized)
            or re.match(r"^[0-9一二三四五六七八九十百千万]+[、.．]", normalized)
        ):
            return None
    # The clause number is not a stable cross-version identity.  The semantic
    # heading is, so ``第十条租赁物的保险`` and ``第九条租赁物的保险`` share a
    # chapter coordinate while unrelated sections remain separate.
    return _strip_clause_number(normalized)


def _strict_chapter_heading(text: str | None) -> str | None:
    """Accept only a document-level heading, never an arbitrary section tag."""

    normalized = comparison_normalize(text or "")[1]
    if not normalized or len(normalized) > 100 or not _CHAPTER_HEADING.match(normalized):
        return None
    return _strip_clause_number(normalized)


def _paragraph_block_for_side(
    side: DiffSide, document: ParsedDocument
) -> Any | None:
    """Locate a paragraph by content, with page as a disambiguating hint."""

    normalized = comparison_normalize(side.text)[1]
    paragraphs = [
        block
        for block in sorted(document.blocks, key=lambda item: item.order)
        if block.type == "PARAGRAPH" and block.normalized_text
    ]
    exact = [
        block
        for block in paragraphs
        if comparison_normalize(block.raw_text)[1] == normalized
    ]
    pages = {location.page for location in _locations(side) if location.page is not None}
    if pages:
        exact_on_page = [block for block in exact if block.location.page in pages]
        if exact_on_page:
            return exact_on_page[0]
    if exact:
        return exact[0]
    if not normalized:
        return None
    best = max(
        paragraphs,
        key=lambda block: ratio(
            comparison_normalize(block.raw_text)[1], normalized
        ),
        default=None,
    )
    if best is None or ratio(comparison_normalize(best.raw_text)[1], normalized) < 70:
        return None
    return best


def _paragraph_chapter(
    side: DiffSide | None, document: ParsedDocument | None
) -> str | None:
    if side is None:
        return None
    for location in _locations(side):
        heading = (
            _chapter_heading(location.section)
            if document is None
            else _strict_chapter_heading(location.section)
        )
        if heading:
            return heading
    if document is None:
        return None
    anchor = _paragraph_block_for_side(side, document)
    if anchor is None:
        return None
    for block in reversed(
        [
            item
            for item in document.blocks
            if item.type == "PARAGRAPH" and item.order <= anchor.order
        ]
    ):
        heading = _strict_chapter_heading(block.raw_text)
        if heading:
            return heading
    return None


def _paragraph_coordinate(
    side: DiffSide | None, document: ParsedDocument | None
) -> dict[str, Any] | None:
    if side is None or _table_indexes(side):
        return None
    anchors: list[str] = []
    if document is not None:
        anchor = _paragraph_block_for_side(side, document)
        if anchor is not None:
            paragraphs = [
                block
                for block in sorted(document.blocks, key=lambda item: item.order)
                if block.type == "PARAGRAPH" and block.normalized_text
            ]
            try:
                anchor_index = paragraphs.index(anchor)
            except ValueError:
                anchor_index = None
            if anchor_index is not None:
                for neighbor_index in (anchor_index - 1, anchor_index + 1):
                    if 0 <= neighbor_index < len(paragraphs):
                        anchors.append(
                            _strip_clause_number(paragraphs[neighbor_index].raw_text)
                        )
    return {
        "chapter": _paragraph_chapter(side, document),
        "semantic_text": _strip_clause_number(side.text),
        "anchors": tuple(anchors),
        "text_digest": hashlib.sha256(side.text.encode("utf-8")).hexdigest()[:16],
    }


def _candidate_coordinate(
    diff: DiffItem,
    *,
    baseline: ParsedDocument | None,
    target: ParsedDocument | None,
) -> dict[str, Any]:
    if baseline and target:
        table_pairs = _table_pair_map(baseline, target)
    else:
        indexes = _table_indexes(diff.baseline) | _table_indexes(diff.target)
        table_pairs = {index: index for index in indexes}
    baseline_table = _table_side_coordinate(
        diff.baseline,
        baseline,
        is_baseline=True,
        table_pairs=table_pairs,
    )
    target_table = _table_side_coordinate(
        diff.target,
        target,
        is_baseline=False,
        table_pairs=table_pairs,
    )
    if baseline_table or target_table:
        table = baseline_table or target_table or {}
        other = target_table or baseline_table or {}
        return {
            "kind": "TABLE",
            "table_pair": tuple(table.get("table_pair", other.get("table_pair", ()))),
            "fields": tuple(sorted(set(table.get("fields", ())) | set(other.get("fields", ())))),
            "row_keys": tuple(
                sorted(set(table.get("row_keys", ())) | set(other.get("row_keys", ())))
            ),
            "logical_ids": tuple(
                sorted(
                    set(table.get("logical_ids", ()))
                    | set(other.get("logical_ids", ()))
                )
            ),
        }
    baseline_paragraph = _paragraph_coordinate(diff.baseline, baseline)
    target_paragraph = _paragraph_coordinate(diff.target, target)
    paragraphs = [item for item in (baseline_paragraph, target_paragraph) if item]
    return {
        "kind": "PARAGRAPH",
        "chapters": tuple(sorted({item["chapter"] for item in paragraphs if item.get("chapter")})),
        "semantic_texts": tuple(item["semantic_text"] for item in paragraphs),
        "anchors": tuple(
            anchor
            for item in paragraphs
            for anchor in item.get("anchors", ())
            if anchor
        ),
    }


def _coordinate_key(coordinate: dict[str, Any]) -> tuple[Any, ...]:
    return (
        coordinate.get("kind"),
        coordinate.get("table_pair"),
        coordinate.get("fields"),
        coordinate.get("row_keys"),
        coordinate.get("logical_ids"),
        coordinate.get("chapters"),
    )


def _populated_texts(diff: DiffItem) -> tuple[str, ...]:
    return tuple(
        value
        for value in (_normalized(diff.baseline), _normalized(diff.target))
        if value
    )


def _field_sets_are_compatible(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if not left or not right:
        return False
    return any(
        a == b or ratio(a, b) >= 80
        for a in left
        for b in right
    )


def _same_side_paragraph_fragments_are_near(
    left: DiffSide | None,
    right: DiffSide | None,
) -> bool:
    """Return whether two one-sided paragraph fragments are locally adjacent.

    A parser can split one replacement block into several target paragraphs
    while the baseline emits the whole block as one deletion.  Paragraph
    indexes are the strongest local signal; page adjacency is the fallback
    for OCR locations that do not carry a paragraph index.  This helper never
    compares locations across files or across tables.
    """

    if left is None or right is None or left.file_id != right.file_id:
        return False
    left_locations = _locations(left)
    right_locations = _locations(right)
    for left_location in left_locations:
        for right_location in right_locations:
            if (
                left_location.paragraph_index is not None
                and right_location.paragraph_index is not None
                and abs(left_location.paragraph_index - right_location.paragraph_index)
                <= 4
            ):
                return True
            if (
                left_location.page is not None
                and right_location.page is not None
                and abs(left_location.page - right_location.page) <= 1
            ):
                return True
    return False


def _same_side_paragraph_fragment_relation(
    left: DiffItem,
    right: DiffItem,
    left_coordinate: dict[str, Any],
    right_coordinate: dict[str, Any],
) -> bool:
    """Recognize split fragments without opening a cross-section bridge."""

    if left_coordinate.get("kind") != "PARAGRAPH" or right_coordinate.get(
        "kind"
    ) != "PARAGRAPH":
        return False
    # The current parser split pattern is target-side additions next to one
    # baseline deletion.  Do not generalize this to multiple baseline
    # deletions: same-section deletions can be independent legal changes.
    if left.diff_type != right.diff_type or left.diff_type != "ADDED":
        return False
    chapters = set(left_coordinate.get("chapters", ())) & set(
        right_coordinate.get("chapters", ())
    )
    if not chapters:
        return False
    left_side, right_side = (
        (left.target, right.target)
        if left.diff_type == "ADDED"
        else (left.baseline, right.baseline)
    )
    opposite_left = left.baseline if left.diff_type == "ADDED" else left.target
    opposite_right = right.baseline if right.diff_type == "ADDED" else right.target
    return (
        left_side is not None
        and right_side is not None
        and opposite_left is None
        and opposite_right is None
        and _same_side_paragraph_fragments_are_near(left_side, right_side)
    )


def _contiguous_deletion_block_runs(
    candidates: list[DiffItem],
    coordinates: dict[int, dict[str, Any]],
) -> list[list[int]]:
    """Find same-side deletion runs that form one physical content block.

    Text similarity is intentionally not used here.  A deleted heading, its
    lead paragraph, and numbered list fragments can all have different
    wording while still being one missing block.  The run is restricted to a
    single chapter, file, and directly contiguous paragraph span; isolated
    deletions therefore remain independent changes.
    """

    eligible: list[tuple[int, str, str, int, int]] = []
    for index, diff in enumerate(candidates):
        if (
            diff.diff_type not in (_LOGICAL_DELETE_TYPES | {"CONTENT_BLOCK_MISSING"})
            or diff.baseline is None
            or diff.target is not None
        ):
            continue
        chapter_values = coordinates[index].get("chapters", ())
        if len(chapter_values) != 1:
            continue
        paragraph_indexes = [
            location.paragraph_index
            for location in _locations(diff.baseline)
            if location.paragraph_index is not None
        ]
        if not paragraph_indexes:
            continue
        file_id = diff.baseline.file_id
        eligible.append(
            (
                index,
                file_id,
                str(chapter_values[0]),
                min(paragraph_indexes),
                max(paragraph_indexes),
            )
        )

    runs: list[list[int]] = []
    by_scope: dict[tuple[str, str], list[tuple[int, str, str, int, int]]] = {}
    for item in eligible:
        by_scope.setdefault((item[1], item[2]), []).append(item)
    for items in by_scope.values():
        items.sort(key=lambda item: (item[3], item[4], _sort_key(candidates[item[0]])))
        run: list[int] = []
        run_end: int | None = None

        def append_run(current_run: list[int]) -> None:
            if len(current_run) < 2:
                return
            # A contiguous paragraph run is not, by itself, proof that
            # several legal changes are one missing block.  Require the
            # first deleted candidate in this particular run to carry
            # section context on at least two physical paragraphs.  This is
            # the parser's explicit heading/block-boundary signal and keeps
            # ordinary list-item deletions independent even when they happen
            # to be adjacent in the same chapter.  Synthetic parser fixtures
            # may repeat one section value on both locations, so count
            # populated section locations rather than distinct section
            # strings.
            first_candidate = candidates[current_run[0]]
            explicit_section_locations = sum(
                bool(location.section)
                for location in _locations(first_candidate.baseline)
            )
            if explicit_section_locations >= 2:
                runs.append(list(current_run))

        for index, _file_id, _chapter, start, end in items:
            if run_end is None or start <= run_end + 1:
                run.append(index)
                run_end = max(run_end or end, end)
                continue
            append_run(run)
            run = [index]
            run_end = end
        append_run(run)
    return runs


def _candidate_relation(
    left: DiffItem,
    right: DiffItem,
    *,
    baseline: ParsedDocument | None,
    target: ParsedDocument | None,
    left_coordinate: dict[str, Any] | None = None,
    right_coordinate: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    left_coordinate = left_coordinate or _candidate_coordinate(
        left, baseline=baseline, target=target
    )
    right_coordinate = right_coordinate or _candidate_coordinate(
        right, baseline=baseline, target=target
    )
    if left_coordinate["kind"] != right_coordinate["kind"]:
        return False, "KIND_MISMATCH"
    if not _logical_diff_types_compatible(left.diff_type, right.diff_type):
        return False, "TYPE_MISMATCH"
    if left_coordinate["kind"] == "TABLE":
        if left_coordinate["table_pair"] != right_coordinate["table_pair"]:
            return False, "TABLE_PAIR_MISMATCH"
        left_fields = set(left_coordinate["fields"])
        right_fields = set(right_coordinate["fields"])
        left_rows = set(left_coordinate["row_keys"])
        right_rows = set(right_coordinate["row_keys"])
        left_physical_rows = {
            location.row
            for location in _locations(left.baseline)
            if location.row is not None
        } | {
            location.row
            for location in _locations(left.target)
            if location.row is not None
        }
        right_physical_rows = {
            location.row
            for location in _locations(right.baseline)
            if location.row is not None
        } | {
            location.row
            for location in _locations(right.target)
            if location.row is not None
        }
        one_sided_add_delete = (
            {left.diff_type, right.diff_type}
            in (
                {"ADDED", "DELETED"},
                {"TABLE_ROW_ADDED", "TABLE_ROW_DELETED"},
            )
            and (left.baseline is None or left.target is None)
            and (right.baseline is None or right.target is None)
        )
        if (
            one_sided_add_delete
            and left_physical_rows
            and right_physical_rows
            and left_physical_rows & right_physical_rows
            and (not left_fields or not right_fields)
            and max(left_physical_rows | right_physical_rows) <= 1
        ):
            # A title/identifier row can change shape while retaining its
            # physical row.  It is a logical replacement candidate, not a
            # field equivalence, and the missing side must carry no competing
            # business field before this relation is allowed.
            return True, "TABLE_HEADER_ROW_REPLACEMENT"
        # A row-level OCR candidate can mention several fields, but it must not
        # be used to bridge a candidate for one field into a candidate for a
        # different field.  Canonical aliases have already been normalized by
        # _business_field_key, so exact set equality is the safe relation here.
        if left_fields == {"租金"} or right_fields == {"租金"}:
            return False, "FIELD_MISMATCH"
        if left_fields and right_fields and left_fields != right_fields:
            return False, "FIELD_MISMATCH"
        fields = left_fields & right_fields
        if not fields:
            if baseline is not None or target is not None:
                if not _field_sets_are_compatible(
                    tuple(left_fields), tuple(right_fields)
                ):
                    if not (
                        set(left_coordinate["logical_ids"])
                        & set(right_coordinate["logical_ids"])
                    ):
                        return False, "FIELD_MISMATCH"
                    fields = {"LOGICAL_CELL_ID"}
                else:
                    fields = {"ALIAS_MATCH"}
            else:
                fields = {"TEXT_FALLBACK"}
        left_logical_ids = set(left_coordinate["logical_ids"])
        right_logical_ids = set(right_coordinate["logical_ids"])
        shared_logical_ids = left_logical_ids & right_logical_ids
        logical_overlap = bool(shared_logical_ids) and (
            left_logical_ids == right_logical_ids
            or (len(left_logical_ids) == 1 and len(right_logical_ids) == 1)
        )
        shared_rows = left_rows & right_rows
        row_overlap = bool(shared_rows) and (
            left_rows == right_rows
            or (len(left_rows) == 1 and len(right_rows) == 1)
        )
        if left_rows and right_rows and not _key_value_rows_are_compatible(
            left_rows, right_rows, fields
        ):
            return False, "ROW_KEY_MISMATCH"
        populated_left, populated_right = _populated_texts(left), _populated_texts(right)
        same_populated_text = bool(set(populated_left) & set(populated_right))
        if not (row_overlap or logical_overlap or same_populated_text):
            one_sided_cross = (
                (left.baseline is None) != (right.baseline is None)
                and (left.baseline is None or left.target is None)
                and (right.baseline is None or right.target is None)
            )
            if (
                one_sided_cross
                and len(fields) == 1
                and fields <= _UNIQUE_EQUIVALENCE_FIELDS
            ):
                # A parser may shift a key/value row after an insertion.  A
                # single canonical field on opposite sides is a safer
                # business coordinate than its physical sub-row number.
                return True, "TABLE_FIELD_CROSS_SIDE"
            if not left_rows or not right_rows:
                return False, "ROW_CONTEXT_MISSING"
            return False, "ROW_KEY_MISMATCH"
        if (
            left.baseline is None
            or left.target is None
            or right.baseline is None
            or right.target is None
        ):
            if same_populated_text or row_overlap:
                return True, "TABLE_FIELD_CROSS_SIDE"
            return False, "TABLE_TEXT_MISMATCH"
        if row_overlap or logical_overlap:
            return True, "TABLE_BUSINESS_COORDINATE"
        return (
            same_populated_text,
            "TABLE_REPEATED_TEXT" if same_populated_text else "TABLE_TEXT_MISMATCH",
        )

    left_text = _strip_clause_number(
        left.baseline.text if left.baseline else left.target.text
    )
    right_text = _strip_clause_number(
        right.baseline.text if right.baseline else right.target.text
    )
    left_chapters = set(left_coordinate["chapters"])
    right_chapters = set(right_coordinate["chapters"])
    chapters = left_chapters & right_chapters
    add_delete = {left.diff_type, right.diff_type} == {"ADDED", "DELETED"}
    if left_chapters and right_chapters and not chapters:
        return False, "CHAPTER_MISMATCH"
    chapter_context_incomplete = bool(left_chapters) != bool(right_chapters)
    if not left_text or not right_text:
        return False, "PARAGRAPH_TEXT_MISSING"
    semantic_texts_left = left_coordinate.get("semantic_texts", ()) or (left_text,)
    semantic_texts_right = right_coordinate.get("semantic_texts", ()) or (right_text,)
    similarity = max(
        ratio(left_value, right_value)
        for left_value in semantic_texts_left
        for right_value in semantic_texts_right
    )
    left_grams = {left_text[index : index + 2] for index in range(max(0, len(left_text) - 1))}
    right_grams = {right_text[index : index + 2] for index in range(max(0, len(right_text) - 1))}
    shared_grams = {
        gram
        for gram in left_grams & right_grams
        if any("\u4e00" <= char <= "\u9fff" for char in gram)
    }
    anchor_match = any(
        ratio(left_anchor, right_anchor) >= 85
        for left_anchor in left_coordinate.get("anchors", ())
        for right_anchor in right_coordinate.get("anchors", ())
    )
    has_chapter = bool(chapters)
    left_pages = _pages(left.baseline) | _pages(left.target)
    right_pages = _pages(right.baseline) | _pages(right.target)
    pages_are_near = bool(
        not left_pages
        or not right_pages
        or min(
            abs(left_page - right_page)
            for left_page in left_pages
            for right_page in right_pages
        )
        <= 1
    )
    contained_fragment = False
    partial_fragment = False
    if add_delete and has_chapter:
        # A paragraph can be split into several target fragments after a
        # deletion.  Pair only when a populated side is demonstrably a
        # fragment of the other side, or when the shared wording is strong
        # enough to identify the same clause.  This is deliberately pairwise;
        # the caller still requires compatibility with every group member.
        contained_fragment = any(
            len(shorter_value) >= 8 and shorter_value in longer_value
            for left_value in _populated_texts(left)
            for right_value in _populated_texts(right)
            for shorter_value, longer_value in (
                (left_value, right_value)
                if len(left_value) <= len(right_value)
                else (right_value, left_value),
            )
        )
        partial_fragment = any(
            len(shorter_value) >= 12
            and partial_ratio(shorter_value, longer_value) >= 78
            for left_value in _populated_texts(left)
            for right_value in _populated_texts(right)
            for shorter_value, longer_value in (
                (left_value, right_value)
                if len(left_value) <= len(right_value)
                else (right_value, left_value),
            )
        )
        if contained_fragment or partial_fragment or (
            similarity >= 35 and len(shared_grams) >= 5
        ):
            return True, "PARAGRAPH_BLOCK_FRAGMENT"
    if chapter_context_incomplete and not (
        contained_fragment
        or partial_fragment
        or anchor_match
        or (similarity >= 82 and len(shared_grams) >= 4)
    ):
        # A missing chapter marker is common after OCR splitting, but it is
        # not permission to match an arbitrary paragraph with an attachment
        # or another clause.
        return False, "CHAPTER_CONTEXT_MISSING"
    if anchor_match and (
        (has_chapter and pages_are_near)
        or (
            add_delete
            and similarity >= 72
            and len(shared_grams) >= 3
        )
    ):
        return True, "PARAGRAPH_CONTEXT_ANCHOR"
    if add_delete and not has_chapter:
        semantic_match = similarity >= 72 or (
            similarity >= 55 and len(shared_grams) >= 3
        )
    else:
        semantic_match = similarity >= (55 if has_chapter else 72) or (
            similarity >= 45 and len(shared_grams) >= 2
        )
    if semantic_match:
        return True, "PARAGRAPH_SEMANTIC_ANCHOR"
    return False, "PARAGRAPH_SEMANTIC_MISMATCH"


def _can_group_logical_candidates(
    left: DiffItem,
    right: DiffItem,
    *,
    baseline: ParsedDocument | None,
    target: ParsedDocument | None,
) -> bool:
    """Return whether two candidates share a program-owned business coordinate."""

    return _candidate_relation(
        left, right, baseline=baseline, target=target
    )[0]


def _equivalence_coordinate_compatible(
    left: DiffItem,
    right: DiffItem,
    *,
    baseline: ParsedDocument | None,
    target: ParsedDocument | None,
    left_coordinate: dict[str, Any] | None = None,
    right_coordinate: dict[str, Any] | None = None,
) -> bool:
    """Apply the non-LLM topology gate for an equivalent-layout candidate pair."""

    left_coordinate = left_coordinate or _candidate_coordinate(
        left, baseline=baseline, target=target
    )
    right_coordinate = right_coordinate or _candidate_coordinate(
        right, baseline=baseline, target=target
    )
    if left_coordinate["kind"] != right_coordinate["kind"]:
        return False
    if left_coordinate["kind"] == "TABLE":
        if left_coordinate.get("table_pair") != right_coordinate.get("table_pair"):
            return False
        left_fields = set(left_coordinate.get("fields", ()))
        right_fields = set(right_coordinate.get("fields", ()))
        if left_fields and right_fields and left_fields != right_fields:
            return False
        left_rows = set(left_coordinate.get("row_keys", ()))
        right_rows = set(right_coordinate.get("row_keys", ()))
        left_ids = set(left_coordinate.get("logical_ids", ()))
        right_ids = set(right_coordinate.get("logical_ids", ()))
        if left_rows & right_rows or left_ids & right_ids:
            return True
        # A same table/field without a row or logical-cell identity is not
        # enough: repeated labels in a long table are distinct business facts.
        # A physical adjacency check is the conservative fallback for older
        # OCR entries and add/delete pairs with no row key.
        if not _same_matched_table_pair(left, right, baseline=baseline, target=target):
            return False
        return any(
            _near_or_overlapping(left_side, right_side)
            for left_side in (left.baseline, left.target)
            for right_side in (right.baseline, right.target)
        )
    left_chapters = set(left_coordinate.get("chapters", ()))
    right_chapters = set(right_coordinate.get("chapters", ()))
    if left_chapters and right_chapters and left_chapters & right_chapters:
        return True
    left_semantic = set(left_coordinate.get("semantic_texts", ()))
    right_semantic = set(right_coordinate.get("semantic_texts", ()))
    if left_semantic & right_semantic:
        return True
    return bool(
        set(left_coordinate.get("anchors", ()))
        & set(right_coordinate.get("anchors", ()))
    )


def _compact_equivalence_text(text: str | None) -> str:
    """Normalize labels and punctuation for a non-publishing equivalence key."""

    value = comparison_normalize(text or "")[1]
    return re.sub(r"[\s，。；：、,.!?！？()（）【】\[\]{}（）]", "", value)


def _chinese_number(value: str) -> str:
    """Return a small stable decimal representation for common Chinese numerals."""

    if value.isdigit():
        return value.lstrip("0") or "0"
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    if not value or any(char not in digits and char not in units for char in value):
        return value
    total = 0
    section = 0
    number = 0
    for char in value:
        if char in digits:
            number = digits[char]
            continue
        unit = units[char]
        if unit == 10000:
            section += number
            total += section * unit
            section = 0
            number = 0
        else:
            section += (number or 1) * unit
            number = 0
    return str(total + section + number)


def _standard_field_value(field: str, text: str | None) -> str:
    """Extract a conservative field value used only by equivalent-layout gates."""

    compact = _compact_equivalence_text(text)
    # Key/value attachments commonly put a subnumber before the field label.
    # It is a row coordinate, never the value of the field itself.  Removing
    # it prevents ``3.总额`` from becoming ``AMOUNT:3`` and ``5.支付日期`` from
    # becoming a different value than the same field numbered ``6``.
    prefix = _SUBNUMBER_PREFIX.match(compact)
    if prefix:
        compact = compact[prefix.end() :]
    else:
        # Some OCR rows omit the separator (``3总额...``).  Only strip the
        # number when it is immediately followed by a known field label; a
        # leading year, account number, or amount remains untouched.
        aliases = "|".join(
            re.escape(_compact_equivalence_text(alias))
            for alias in sorted(_BUSINESS_FIELD_ALIASES, key=len, reverse=True)
        )
        compact = re.sub(
            rf"^[0-9一二三四五六七八九十百千万两〇零]+(?=(?:{aliases}))",
            "",
            compact,
            count=1,
        )
    canonical_field = _field_key(field)
    period = re.search(r"(?:共)?([0-9一二三四五六七八九十百千万两〇零]+)期", compact)
    if canonical_field in {"期数", "租赁期限"} and period:
        return f"PERIOD:{_chinese_number(period.group(1))}"
    amount = re.search(r"([0-9][0-9,]*(?:\.\d+)?)(万|亿|元)?", compact)
    if (
        canonical_field
        in {"金额", "总额", "租金", "每期租金", "保证金", "手续费", "保险"}
        and amount
    ):
        number = amount.group(1).replace(",", "")
        unit = amount.group(2) or ""
        try:
            from decimal import Decimal

            multiplier = {"万": Decimal("10000"), "亿": Decimal("100000000")}.get(
                unit, Decimal("1")
            )
            normalized = (Decimal(number) * multiplier).normalize()
            return f"AMOUNT:{normalized}元"
        except Exception:  # noqa: BLE001 - a conservative non-match is safe
            return f"AMOUNT:{number}{unit}"
    date = re.search(
        r"((?:\d{4}年)?\d{1,2}月\d{1,2}日|每月\d{1,2}日|\d{4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?)",
        compact,
    )
    if canonical_field in {"租金支付日", "日期", "支付日期"} and date:
        return f"DATE:{date.group(1)}"
    value = compact
    for alias in sorted(_BUSINESS_FIELD_ALIASES, key=len, reverse=True):
        value = value.replace(_compact_equivalence_text(alias), "")
    value = re.sub(r"^(?:共)?[0-9一二三四五六七八九十百千万两]+[、.．]", "", value)
    return f"VALUE:{value}"


def _candidate_field_keys(
    diff: DiffItem,
    coordinate: dict[str, Any],
) -> tuple[str, ...]:
    fields = {
        _field_key(value)
        for value in coordinate.get("fields", ())
        if isinstance(value, str) and value
    }
    coordinate_fields = set(fields)
    for side in (diff.baseline, diff.target):
        field = _business_field_key(side.text if side else None)
        if field and (not coordinate_fields or field in coordinate_fields):
            fields.add(field)
    return tuple(sorted(fields))


def _candidate_table_values(
    diff: DiffItem,
    coordinate: dict[str, Any],
) -> dict[str, set[str]]:
    fields = _candidate_field_keys(diff, coordinate)
    values: dict[str, set[str]] = {field: set() for field in fields}
    for side in (diff.baseline, diff.target):
        if side is None:
            continue
        text = side.text
        explicit = _business_field_key(text)
        candidate_fields = (
            (explicit,)
            if explicit and (not fields or explicit in fields)
            else fields
        )
        for field in candidate_fields:
            if field:
                values.setdefault(field, set()).add(_standard_field_value(field, text))
    return {field: items for field, items in values.items() if items}


def _formula_signature(text: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    compact = _compact_equivalence_text(text)
    symbols = tuple(sorted(set(re.findall(r"[=+\-*/×÷%％]", compact))))
    values = tuple(sorted(_FORMULA_VALUE.findall(compact)))
    words = tuple(
        sorted(
            {
                _field_key(alias)
                for alias in _BUSINESS_FIELD_ALIASES
                if _compact_equivalence_text(alias) in compact
            }
            - _COMMON_BUSINESS_WORDS
        )
    )
    return symbols, values, words


def _formula_body(text: str) -> str:
    """Remove formula syntax and values before comparing business wording."""

    value = _compact_equivalence_text(text)
    value = _FORMULA_VALUE.sub("", value)
    value = re.sub(r"[=+\-*/×÷%％]", "", value)
    value = re.sub(r"^[0-9一二三四五六七八九十百千万两〇零]+[、.．]", "", value)
    return value


def _formula_group_is_safe(diffs: list[DiffItem]) -> bool:
    """Compare a formula equivalence group after concatenating its fragments.

    Formula fragments are often split at different boundaries by the two
    parsers.  Equality of each physical fragment is therefore too strict.  A
    group is safe only when its extracted values are identical, its formula
    syntax overlaps, and the remaining business wording has strong overlap.
    """

    baseline_text = "".join(
        _normalized(diff.baseline) or ""
        for diff in sorted(diffs, key=_sort_key)
        if diff.baseline is not None
    )
    target_text = "".join(
        _normalized(diff.target) or ""
        for diff in sorted(diffs, key=_sort_key)
        if diff.target is not None
    )
    if not baseline_text or not target_text:
        return False
    if not any(
        _FORMULA_MARKERS.search(value) for value in (baseline_text, target_text)
    ):
        return False
    if any(
        re.search(r"不|无|禁止|不得|除外|但书", value)
        for value in (baseline_text, target_text)
    ):
        return False
    baseline_signature = _formula_signature(baseline_text)
    target_signature = _formula_signature(target_text)
    if not baseline_signature[1] or set(baseline_signature[1]) != set(
        target_signature[1]
    ):
        return False
    if not set(baseline_signature[0]) & set(target_signature[0]):
        return False
    left_body, right_body = _formula_body(baseline_text), _formula_body(target_text)
    if not left_body or not right_body:
        return False
    if left_body in right_body or right_body in left_body:
        return True
    similarity = ratio(left_body, right_body)
    left_grams = {left_body[index : index + 2] for index in range(len(left_body) - 1)}
    right_grams = {right_body[index : index + 2] for index in range(len(right_body) - 1)}
    shared_grams = {
        gram
        for gram in left_grams & right_grams
        if any("\u4e00" <= char <= "\u9fff" for char in gram)
    }
    return similarity >= 55 or len(shared_grams) >= 3


def _formula_context_related(
    left: dict[str, Any], right: dict[str, Any]
) -> bool:
    """Require a shared chapter or local semantic anchor for formula pairing."""

    left_chapters = [_strip_clause_number(value) for value in left.get("chapters", ())]
    right_chapters = [_strip_clause_number(value) for value in right.get("chapters", ())]
    if any(
        a == b or ratio(a, b) >= 85
        for a in left_chapters
        for b in right_chapters
        if a and b
    ):
        return True
    return any(
        a == b or ratio(a, b) >= 85
        for a in left.get("anchors", ())
        for b in right.get("anchors", ())
        if a and b
    )


def _formula_pair_is_eligible(
    left: DiffItem,
    right: DiffItem,
    *,
    baseline: ParsedDocument | None = None,
    target: ParsedDocument | None = None,
    left_coordinate: dict[str, Any] | None = None,
    right_coordinate: dict[str, Any] | None = None,
) -> bool:
    """Return whether two fragments can seed a group-level formula check."""

    if {left.diff_type, right.diff_type} != {"ADDED", "DELETED"}:
        return False
    left_signature = _formula_signature(" ".join(_populated_texts(left)))
    right_signature = _formula_signature(" ".join(_populated_texts(right)))
    if not set(left_signature[1]) & set(right_signature[1]):
        return False
    if baseline is None and target is None:
        return True
    left_coordinate = left_coordinate or _candidate_coordinate(
        left, baseline=baseline, target=target
    )
    right_coordinate = right_coordinate or _candidate_coordinate(
        right, baseline=baseline, target=target
    )
    return _formula_context_related(left_coordinate, right_coordinate)


def _discover_formula_equivalence_groups(
    candidates: list[DiffItem],
    *,
    baseline: ParsedDocument | None,
    target: ParsedDocument | None,
    coordinates: dict[int, dict[str, Any]] | None = None,
) -> list[list[int]]:
    """Find small formula groups using a group-level, not transitive, test."""

    coordinates = coordinates or {
        index: _candidate_coordinate(candidate, baseline=baseline, target=target)
        for index, candidate in enumerate(candidates)
    }
    ordered = sorted(range(len(candidates)), key=lambda index: _sort_key(candidates[index]))
    groups: list[list[int]] = []
    used: set[int] = set()
    for left_offset, left_index in enumerate(ordered):
        if left_index in used or coordinates[left_index].get("kind") != "PARAGRAPH":
            continue
        for right_index in ordered[left_offset + 1 :]:
            if right_index in used or coordinates[right_index].get("kind") != "PARAGRAPH":
                continue
            left = candidates[left_index]
            right = candidates[right_index]
            if not _formula_pair_is_eligible(
                left,
                right,
                baseline=baseline,
                target=target,
                left_coordinate=coordinates[left_index],
                right_coordinate=coordinates[right_index],
            ):
                continue
            pair = [left_index, right_index]
            bridge_candidates = [
                index
                for index in ordered
                if index not in {left_index, right_index}
                and index not in used
                and coordinates[index].get("kind") == "PARAGRAPH"
                and {candidates[index].diff_type, left.diff_type, right.diff_type}
                <= {"ADDED", "DELETED"}
                and _formula_context_related(coordinates[index], coordinates[left_index])
                and _formula_context_related(coordinates[index], coordinates[right_index])
            ]
            best = pair
            for bridge_index in bridge_candidates:
                proposed = sorted(
                    [*pair, bridge_index],
                    key=lambda index: _sort_key(candidates[index]),
                )
                if len(proposed) <= _EQUIVALENCE_CLUSTER_MAX_SIZE and _formula_group_is_safe(
                    [candidates[index] for index in proposed]
                ):
                    best = proposed
                    break
            if _formula_group_is_safe([candidates[index] for index in best]):
                groups.append(best)
                used.update(best)
                break
    return groups


def _table_equivalence_pair_reason(
    left: DiffItem,
    right: DiffItem,
    left_coordinate: dict[str, Any],
    right_coordinate: dict[str, Any],
) -> tuple[bool, str]:
    left_values = _candidate_table_values(left, left_coordinate)
    right_values = _candidate_table_values(right, right_coordinate)
    common_fields = set(left_values) & set(right_values)
    if not common_fields:
        left_texts = set(_populated_texts(left))
        right_texts = set(_populated_texts(right))
        if not left_coordinate.get("fields") and left_texts & right_texts:
            return True, "EXACT_CONTENT_EQUIVALENCE"
        return False, "FIELD_MISMATCH"
    if any(left_values[field] & right_values[field] for field in common_fields):
        return True, "TABLE_FIELD_VALUE_EQUIVALENCE"
    return False, "VALUE_MISMATCH"


def _equivalence_candidate_pair_reason(
    left: DiffItem,
    right: DiffItem,
    *,
    baseline: ParsedDocument | None,
    target: ParsedDocument | None,
    left_coordinate: dict[str, Any] | None = None,
    right_coordinate: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Return both the equivalence decision and a safe diagnostic reason."""

    left_coordinate = left_coordinate or _candidate_coordinate(
        left, baseline=baseline, target=target
    )
    right_coordinate = right_coordinate or _candidate_coordinate(
        right, baseline=baseline, target=target
    )
    if left_coordinate["kind"] != right_coordinate["kind"]:
        return False, "COORDINATE_MISMATCH"
    table_equivalence: tuple[bool, str] | None = None
    if left_coordinate["kind"] == "TABLE":
        if left_coordinate.get("table_pair") != right_coordinate.get("table_pair"):
            return False, "COORDINATE_MISMATCH"
        table_equivalence = _table_equivalence_pair_reason(
            left, right, left_coordinate, right_coordinate
        )
    if not _equivalence_coordinate_compatible(
        left,
        right,
        baseline=baseline,
        target=target,
        left_coordinate=left_coordinate,
        right_coordinate=right_coordinate,
    ):
        if table_equivalence and table_equivalence[0]:
            common_fields = set(_candidate_field_keys(left, left_coordinate)) & set(
                _candidate_field_keys(right, right_coordinate)
            )
            one_sided = (
                left.baseline is None
                or left.target is None
            ) and (right.baseline is None or right.target is None)
            if (
                one_sided
                and common_fields & _UNIQUE_EQUIVALENCE_FIELDS
                and left_coordinate.get("table_pair")
                == right_coordinate.get("table_pair")
            ):
                # For a confirmed key/value field, row numbers are parser
                # coordinates rather than business identity.  This is the
                # deliberate escape hatch for shifted rows and flattened
                # merged cells; table pair + canonical field + canonical value
                # remain mandatory above.
                return True, table_equivalence[1]
        left_fields = set(left_coordinate.get("fields", ()))
        right_fields = set(right_coordinate.get("fields", ()))
        return False, "FIELD_MISMATCH" if left_fields and right_fields and not (
            left_fields & right_fields
            or _field_sets_are_compatible(tuple(left_fields), tuple(right_fields))
        ) else "COORDINATE_MISMATCH"

    left_baseline = _normalized(left.baseline)
    left_target = _normalized(left.target)
    right_baseline = _normalized(right.baseline)
    right_target = _normalized(right.target)
    if any(
        baseline_text
        and target_text
        and baseline_text != target_text
        for baseline_text, target_text in (
            (left_baseline, left_target),
            (right_baseline, right_target),
        )
    ):
        return False, "VALUE_MISMATCH"
    if left_coordinate["kind"] == "TABLE":
        assert table_equivalence is not None
        return table_equivalence
    add_delete = {left.diff_type, right.diff_type} == {"ADDED", "DELETED"}
    if add_delete and any(
        _FORMULA_MARKERS.search(value or "")
        for value in (left_baseline, left_target, right_baseline, right_target)
    ):
        return True, "COMPOSITE_EQUIVALENCE_REQUIRED"
    left_populated = {item for item in (left_baseline, left_target) if item}
    right_populated = {item for item in (right_baseline, right_target) if item}
    if (
        left_populated & right_populated
        and _candidate_value_tokens(left) == _candidate_value_tokens(right)
    ):
        return True, "EXACT_CONTENT_EQUIVALENCE"
    if add_delete and left_populated and right_populated:
        return False, "COMPOSITE_EQUIVALENCE_REQUIRED"
    return False, "VALUE_MISMATCH"


def _equivalent_candidate_pair(
    left: DiffItem,
    right: DiffItem,
    *,
    baseline: ParsedDocument | None,
    target: ParsedDocument | None,
) -> bool:
    """Find a conservative same-content pair missed by the change graph."""

    return _equivalence_candidate_pair_reason(
        left, right, baseline=baseline, target=target
    )[0]


def _equivalent_group_is_safe(
    diffs: list[DiffItem],
    *,
    baseline: ParsedDocument | None = None,
    target: ParsedDocument | None = None,
) -> bool:
    """Check all fragments of an equivalent group without model assistance."""

    coordinates = [
        _candidate_coordinate(diff, baseline=baseline, target=target) for diff in diffs
    ]
    if coordinates and all(item.get("kind") == "TABLE" for item in coordinates):
        # A two-sided cell with different normalized text is a real change,
        # not an add/delete layout duplicate.  Identical two-sided text is
        # still allowed through the existing layout-noise equality gate.
        if any(
            diff.baseline is not None
            and diff.target is not None
            and _normalized(diff.baseline) != _normalized(diff.target)
            for diff in diffs
        ):
            return False
        baseline_values: dict[str, set[str]] = {}
        target_values: dict[str, set[str]] = {}
        for diff, coordinate in zip(diffs, coordinates, strict=True):
            for field, values in _candidate_table_values(diff, coordinate).items():
                for value in values:
                    if diff.baseline is not None:
                        baseline_values.setdefault(field, set()).add(value)
                    if diff.target is not None:
                        target_values.setdefault(field, set()).add(value)
        if baseline_values and target_values:
            shared_fields = set(baseline_values) & set(target_values)
            if (
                shared_fields
                and set(baseline_values) == set(target_values)
                and all(
                    baseline_values[field] == target_values[field]
                    for field in shared_fields
                )
            ):
                return True
    all_text = " ".join(
        value
        for diff in diffs
        for value in (_normalized(diff.baseline), _normalized(diff.target))
        if value
    )
    if _FORMULA_MARKERS.search(all_text) and _formula_group_is_safe(diffs):
        return True

    baseline_texts = {
        value for diff in diffs if (value := _normalized(diff.baseline))
    }
    target_texts = {
        value for diff in diffs if (value := _normalized(diff.target))
    }
    if not baseline_texts or not target_texts or baseline_texts != target_texts:
        return False
    baseline_values = {
        token
        for diff in diffs
        for token in _value_tokens(diff.baseline.text if diff.baseline else None)
    }
    target_values = {
        token
        for diff in diffs
        for token in _value_tokens(diff.target.text if diff.target else None)
    }
    if baseline_values != target_values:
        return False
    return not any(
        re.search(r"不|无|禁止|不得|除外|但书", text)
        for text in (*baseline_texts, *target_texts)
    )


def _is_boundary_noise(diff: DiffItem) -> bool:
    """Recognize explicitly parser-labelled, content-preserving noise."""

    if diff.validation_reason_code == "BOUNDARY_NOISE":
        return True
    if diff.review_reason != "OCR_READING_ORDER_VARIANCE":
        return False
    if diff.baseline is None or diff.target is None:
        return False
    left = _normalized(diff.baseline)
    right = _normalized(diff.target)
    if not left or not right:
        return False
    return re.sub(r"[，。；：、,.!?！？\s]", "", left) == re.sub(
        r"[，。；：、,.!?！？\s]", "", right
    )


_BOUNDARY_WORDS = frozenset(
    {
        "续",
        "续上",
        "接上页",
        "见下页",
        "分页",
        "页眉",
        "页脚",
        "目录",
        "以及",
        "并且",
        "其中",
        "如下",
    }
)

_BOUNDARY_CONNECTORS = frozenset(
    {"和", "及", "或", "与", "其中", "如下", "续", "续上", "接上页", "见下页"}
)


def _boundary_residual_is_safe(
    diff: DiffItem,
    *,
    expanded_side: DiffSide,
    document: ParsedDocument,
    core_text: str,
) -> bool:
    """Require every text fragment outside the contained body to be noise.

    A whole candidate being contained is insufficient: a real clause can be
    a prefix of a longer clause.  The only safe case is when the additional
    physical blocks are a heading, a page marker, or a conjunction left at a
    parser boundary.
    """

    indexes = {
        location.paragraph_index
        for location in _locations(expanded_side)
        if location.paragraph_index is not None
    }
    pages = {
        location.page
        for location in _locations(expanded_side)
        if location.page is not None
    }
    if not indexes:
        return False
    residuals: list[tuple[str, Any]] = []
    for block in document.blocks:
        if (
            block.type != "PARAGRAPH"
            or not block.normalized_text
            or block.location.paragraph_index not in indexes
            or (pages and block.location.page not in pages)
        ):
            continue
        normalized = comparison_normalize(block.raw_text)[1]
        if normalized != core_text:
            residuals.append((normalized, block))
    if not residuals:
        return False
    for value, _block in residuals:
        compact = _compact_equivalence_text(value)
        if not compact:
            continue
        if (
            _VALUE_TOKEN.search(compact)
            or _AMOUNT_CONTEXT.search(compact)
            or _DATE_CONTEXT.search(compact)
            or _TERM_CONTEXT.search(compact)
            or _IDENTIFIER_CONTEXT.search(compact)
        ):
            return False
        if compact in _BOUNDARY_CONNECTORS or compact in _BOUNDARY_WORDS:
            continue
        # Short standalone heading fragments can be emitted in a separate
        # paragraph at a page break.  A parser-provided section value is the
        # required identity signal; arbitrary short text is not enough.
        sections = {
            _compact_equivalence_text(location.section)
            for location in _locations(expanded_side)
            if location.section
        }
        if (
            compact in sections
            and len(compact) <= 24
            and not re.search(r"[，。；：、,.!?！？]", value)
        ):
            continue
        if compact.startswith("分页") or compact in {"页眉", "页脚"}:
            continue
        return False
    return True


def _boundary_candidate_kind(diff: DiffItem) -> bool:
    texts = [
        _compact_equivalence_text(side.text)
        for side in (diff.baseline, diff.target)
        if side is not None
    ]
    if not texts:
        return False
    # A boundary fragment must not carry a business value.  This keeps a
    # short-but-real amount, date, term, or identifier from being classified
    # as parser noise merely because it is near another block.
    if any(
        _VALUE_TOKEN.search(value)
        or _AMOUNT_CONTEXT.search(value)
        or _DATE_CONTEXT.search(value)
        or _TERM_CONTEXT.search(value)
        or _IDENTIFIER_CONTEXT.search(value)
        for value in texts
    ):
        return False
    return any(
        value in _BOUNDARY_WORDS
        or value.startswith("分页")
        or (len(value) <= 10 and value in {"和", "及", "或", "如下", "续"})
        for value in texts
    )


def _boundary_containment_evidence(
    diff: DiffItem,
    *,
    baseline: ParsedDocument | None,
    target: ParsedDocument | None,
) -> bool:
    """Detect a parser split only when an adjacent block contains the core."""

    if baseline is None or target is None or diff.baseline is None or diff.target is None:
        return False
    left, right = _normalized(diff.baseline), _normalized(diff.target)
    if not left or not right or left == right:
        return False
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if shorter not in longer or len(longer) - len(shorter) > 8:
        return False
    # A short, value-free suffix/prefix is only noise when the side with the
    # expanded text was split into multiple physical paragraph locations.
    expanded_side = diff.target if len(right) > len(left) else diff.baseline
    if len(_locations(expanded_side)) < 2:
        return False
    documents = (target, baseline) if expanded_side is diff.target else (baseline, target)
    core_side = diff.baseline if shorter == left else diff.target
    core_text = _normalized(core_side)
    if not core_text:
        return False
    location_indexes = {
        location.paragraph_index
        for location in _locations(expanded_side)
        if location.paragraph_index is not None
    }
    for document in documents:
        for block in document.blocks:
            if block.type != "PARAGRAPH" or not block.normalized_text:
                continue
            if (
                location_indexes
                and block.location.paragraph_index not in location_indexes
            ):
                continue
            if comparison_normalize(block.raw_text)[1] == core_text:
                if _boundary_residual_is_safe(
                    diff,
                    expanded_side=expanded_side,
                    document=documents[0],
                    core_text=core_text,
                ):
                    return True
    return False


def _boundary_locations_are_near(left: DiffItem, right: DiffItem) -> bool:
    for left_side, right_side in (
        (left.baseline, right.baseline),
        (left.target, right.target),
    ):
        if left_side is None or right_side is None:
            continue
        if left_side.file_id != right_side.file_id:
            continue
        if _near_or_overlapping(left_side, right_side):
            return True
        left_pages, right_pages = _pages(left_side), _pages(right_side)
        if left_pages and right_pages and min(
            abs(a - b) for a in left_pages for b in right_pages
        ) <= 1:
            return True
    return False


def _boundary_noise_reason(
    diff: DiffItem,
    candidates: list[DiffItem],
    index: int,
    *,
    baseline: ParsedDocument | None = None,
    target: ParsedDocument | None = None,
) -> str | None:
    if _is_boundary_noise(diff):
        return "BOUNDARY_NOISE"
    texts = [
        _compact_equivalence_text(side.text)
        for side in (diff.baseline, diff.target)
        if side is not None
    ]
    if (
        texts
        and not any(
            _VALUE_TOKEN.search(value)
            or _AMOUNT_CONTEXT.search(value)
            or _DATE_CONTEXT.search(value)
            or _TERM_CONTEXT.search(value)
            or _IDENTIFIER_CONTEXT.search(value)
            for value in texts
        )
        and _boundary_containment_evidence(
            diff, baseline=baseline, target=target
        )
    ):
        return "BOUNDARY_NOISE"
    if not _boundary_candidate_kind(diff):
        return None
    compact_values = {
        _compact_equivalence_text(side.text)
        for side in (diff.baseline, diff.target)
        if side is not None
    }
    for other_index, other in enumerate(candidates):
        if other_index == index or not _boundary_locations_are_near(diff, other):
            continue
        other_values = {
            _compact_equivalence_text(side.text)
            for side in (other.baseline, other.target)
            if side is not None
        }
        if any(
            small != large and small and small in large
            for small in compact_values
            for large in other_values
        ):
            return "BOUNDARY_NOISE"
    return "BOUNDARY_EVIDENCE_MISSING"


def _component_indexes(adjacency: dict[int, set[int]]) -> list[list[int]]:
    components: list[list[int]] = []
    unseen = set(adjacency)
    while unseen:
        root = min(unseen)
        queue = [root]
        unseen.remove(root)
        component: list[int] = []
        while queue:
            current = queue.pop(0)
            component.append(current)
            for neighbor in sorted(adjacency[current]):
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    queue.append(neighbor)
        components.append(sorted(component))
    return components


def _complete_relation_groups(
    component: list[int],
    candidates: list[DiffItem],
    *,
    compatible: Any,
    max_size: int,
) -> tuple[list[list[int]], int]:
    """Partition a relation component into disjoint complete-compatible groups.

    Connected components are useful for finding possible relationships, but
    they are not themselves evidence that every member belongs together.  A
    candidate is added only when it is compatible with every member already
    in the group.  Unpaired candidates remain in the ordinary result rather
    than being silently dropped.
    """

    ordered = sorted(component, key=lambda index: _sort_key(candidates[index]))

    # A relation component may contain several complete groups connected by
    # weaker cross-edges.  Building only one greedy group per seed can consume
    # a member needed by another complete group (for example two independent
    # three-fragment replacements in one component).  Small components use an
    # exact, memoized set-packing search; larger components use a bounded
    # maximal-group fallback so a noisy relation graph cannot make dry-run
    # unbounded.
    possible: list[tuple[int, ...]] = []
    if len(ordered) <= 18:
        for size in range(2, min(max_size, len(ordered)) + 1):
            for group in combinations(ordered, size):
                if all(
                    compatible(candidates[left], candidates[right])
                    for left_offset, left in enumerate(group)
                    for right in group[left_offset + 1 :]
                ):
                    possible.append(group)
    else:
        # Preserve the old bounded discovery shape for unusually large
        # components.  It still requires every member to be compatible with
        # every other member in the returned group.
        for seed in ordered:
            group = [seed]
            for index in ordered:
                if index == seed or len(group) >= max_size:
                    continue
                if all(
                    compatible(candidates[index], candidates[member])
                    for member in group
                ):
                    group.append(index)
            if len(group) >= 2:
                possible.append(tuple(group))

    possible = list(dict.fromkeys(possible))
    if possible:
        possible_masks = {
            group: sum(1 << index for index in group)
            for group in possible
        }
        possible = [
            group
            for group in possible
            if not any(
                len(other) > len(group)
                and possible_masks[group] & possible_masks[other]
                == possible_masks[group]
                for other in possible
            )
        ]
    possible.sort(
        key=lambda group: (
            -len(group),
            tuple(_sort_key(candidates[index]) for index in group),
        )
    )

    if len(ordered) <= 18:
        groups_by_position: dict[int, list[tuple[int, tuple[int, ...]]]] = {
            position: [] for position in range(len(ordered))
        }
        position_by_index = {index: position for position, index in enumerate(ordered)}
        group_masks: list[int] = []
        for group_index, group in enumerate(possible):
            mask = 0
            for index in group:
                mask |= 1 << position_by_index[index]
            group_masks.append(mask)
            for index in group:
                groups_by_position[position_by_index[index]].append((group_index, group))

        def better(
            left: tuple[
                int,
                tuple[int, ...],
                tuple[tuple[int, ...], ...],
                tuple[tuple[int, ...], ...],
            ],
            right: tuple[
                int,
                tuple[int, ...],
                tuple[tuple[int, ...], ...],
                tuple[tuple[int, ...], ...],
            ],
        ) -> bool:
            if left[0] != right[0]:
                return left[0] > right[0]
            if left[1] != right[1]:
                return left[1] > right[1]
            return left[2] < right[2]

        @cache
        def solve(mask: int) -> tuple[
            int, tuple[int, ...], tuple[tuple[int, ...], ...], tuple[tuple[int, ...], ...]
        ]:
            if not mask:
                return 0, (), (), ()
            lowest_bit = mask & -mask
            position = lowest_bit.bit_length() - 1
            best = solve(mask ^ lowest_bit)
            for group_index, group in groups_by_position[position]:
                group_mask = group_masks[group_index]
                if mask & group_mask != group_mask:
                    continue
                covered, sizes, _signature, selected = solve(mask ^ group_mask)
                merged_groups = tuple(sorted((*selected, group)))
                candidate = (
                    covered + len(group),
                    tuple(sorted((*sizes, len(group)), reverse=True)),
                    merged_groups,
                    merged_groups,
                )
                if better(candidate, best):
                    best = candidate
            return best

        _, _, _, selected_groups = solve((1 << len(ordered)) - 1)
        groups = [list(group) for group in selected_groups]
    else:
        # ``possible`` is already source-stable and bounded to one group per
        # seed.  Prefer maximum coverage, then larger groups, with a stable
        # source-order tie break.
        groups = []
        remaining = set(ordered)
        for group in possible:
            if set(group) <= remaining:
                groups.append(list(group))
                remaining.difference_update(group)

    groups.sort(
        key=lambda group: tuple(_sort_key(candidates[index]) for index in group)
    )
    remaining = set(ordered)
    for group in groups:
        remaining.difference_update(group)

    # A singleton is not a cluster, but it remains unclustered so the
    # original candidate is still published as evidence.
    return groups, len(remaining)


def _table_equivalence_bucket_key(
    diff: DiffItem,
    *,
    baseline: ParsedDocument | None,
    target: ParsedDocument | None,
    coordinate: dict[str, Any] | None = None,
) -> tuple[Any, ...] | None:
    coordinate = coordinate or _candidate_coordinate(
        diff, baseline=baseline, target=target
    )
    if coordinate.get("kind") != "TABLE":
        return None
    coordinate_fields = {
        value for value in coordinate.get("fields", ()) if isinstance(value, str) and value
    }
    # A row-level candidate may carry all fields from a flattened OCR row.  It
    # is not a valid field-equivalence candidate: allowing it into a bucket
    # would connect unrelated rent fields through the shared group label.
    if len(coordinate_fields) != 1:
        return None
    values = _candidate_table_values(diff, coordinate)
    pairs = sorted(
        (field, value)
        for field, field_values in values.items()
        for value in field_values
        if field and value
    )
    if len(pairs) != 1:
        return None
    field, value = pairs[0]
    if field not in _UNIQUE_EQUIVALENCE_FIELDS:
        return None
    if diff.baseline is not None and diff.target is not None:
        return None
    return (coordinate.get("table_pair"), field, value)


def _formula_equivalence_pair(
    left: DiffItem,
    right: DiffItem,
    *,
    baseline: ParsedDocument | None,
    target: ParsedDocument | None,
) -> bool:
    left_coordinate = _candidate_coordinate(left, baseline=baseline, target=target)
    right_coordinate = _candidate_coordinate(right, baseline=baseline, target=target)
    if left_coordinate.get("kind") != "PARAGRAPH" or right_coordinate.get("kind") != "PARAGRAPH":
        return False
    if not _formula_pair_is_eligible(left, right):
        return False
    return _formula_group_is_safe([left, right])


def _topology_descriptor(
    diff: DiffItem,
    *,
    baseline: ParsedDocument | None,
    target: ParsedDocument | None,
) -> dict[str, Any]:
    """Create a body-free descriptor for a fixed-pair gold fingerprint."""

    coordinate = _candidate_coordinate(diff, baseline=baseline, target=target)

    def side_descriptor(
        side: DiffSide | None,
        document: ParsedDocument | None,
        direction: str,
    ) -> dict[str, Any] | None:
        if side is None:
            return None
        return {
            "direction": direction,
            "file_sha256": document.sha256 if document is not None else None,
            "present": True,
            "locations": sorted(_audit_side_locations(side)),
            "text_sha256": hashlib.sha256(
                (_normalized(side) or "").encode("utf-8")
            ).hexdigest()[:16],
        }

    return {
        "diff_type": diff.diff_type,
        "baseline": side_descriptor(diff.baseline, baseline, "BASELINE"),
        "target": side_descriptor(diff.target, target, "TARGET"),
        "coordinate": {
            "kind": coordinate.get("kind"),
            "table_pair": coordinate.get("table_pair"),
            "fields": coordinate.get("fields", ()),
            "row_keys": coordinate.get("row_keys", ()),
            "logical_ids": coordinate.get("logical_ids", ()),
            "chapters_sha256": hashlib.sha256(
                json.dumps(
                    coordinate.get("chapters", ()), ensure_ascii=False, sort_keys=True
                ).encode("utf-8")
            ).hexdigest()[:16],
        },
    }


def candidate_topology_fingerprint(
    candidate_ids: list[str] | tuple[str, ...],
    by_id: dict[str, DiffItem],
    *,
    baseline: ParsedDocument | None = None,
    target: ParsedDocument | None = None,
) -> str:
    """Return an opaque, stable fingerprint without exposing source text."""

    ordered_ids = sorted(
        candidate_ids,
        key=lambda candidate_id: _sort_key(by_id[candidate_id]),
    )
    descriptors = [
        _topology_descriptor(by_id[candidate_id], baseline=baseline, target=target)
        for candidate_id in ordered_ids
    ]
    value = json.dumps(
        descriptors,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _sort_key(diff: DiffItem) -> tuple[Any, ...]:
    first = (_locations(diff.baseline) or _locations(diff.target) or [None])[0]
    return (
        getattr(first, "page", None) or 0,
        getattr(first, "table_index", None) or 0,
        getattr(first, "row", None) or 0,
        getattr(first, "column", None) or 0,
        getattr(first, "paragraph_index", None) or 0,
        diff.candidate_id or diff.diff_id,
    )


def _cluster_id(candidate_ids: list[str]) -> str:
    digest = hashlib.sha256("\x1f".join(sorted(candidate_ids)).encode()).hexdigest()[:20]
    return f"cluster_{digest}"


def _group_id(candidate_ids: list[str]) -> str:
    digest = hashlib.sha256("\x1f".join(sorted(candidate_ids)).encode()).hexdigest()[:20]
    return f"group_{digest}"


def _candidate_payload(
    diff: DiffItem,
    baseline: ParsedDocument | None,
    target: ParsedDocument | None,
) -> dict[str, Any]:
    return {
        "candidate_id": diff.candidate_id,
        "diff_type": diff.diff_type,
        "baseline": _side_payload(diff.baseline, baseline),
        "target": _side_payload(diff.target, target),
    }


def _context_key(
    diff: DiffItem,
    *,
    baseline: ParsedDocument | None,
    target: ParsedDocument | None,
) -> tuple[str | None, str | None]:
    def header(side: DiffSide | None, document: ParsedDocument | None) -> str | None:
        payload = _side_payload(side, document)
        contexts = payload.get("contexts", []) if payload else []
        value = contexts[0].get("header") if contexts else None
        return comparison_normalize(value)[1] if isinstance(value, str) and value else None

    return (
        header(diff.baseline, baseline),
        header(diff.target, target),
    )


def build_suspected_duplicate_clusters(
    comparison: ComparisonResult,
    *,
    baseline: ParsedDocument | None = None,
    target: ParsedDocument | None = None,
) -> list[DuplicateCluster]:
    """Build deterministic logical and equivalent candidate components.

    The ordinary graph finds fragments of one logical change.  A second,
    disjoint graph finds exact-content add/delete pairs that are layout
    artefacts.  Keeping the graphs disjoint prevents an equivalent pair from
    weakening the evidence for a real change.
    """

    # Every V2 diff is eligible for discovery.  ``candidate_id`` is an
    # internal catalog identity, not a public review verdict; restricting the
    # graph to the comparator's earlier ambiguous subset was the reason real
    # confirmed add/delete and numeric changes never reached this stage.
    catalog = [
        diff
        if diff.candidate_id
        else diff.model_copy(update={"candidate_id": _candidate_id(diff)})
        for diff in comparison.diff_items
    ]
    comparison.diff_items = catalog
    comparison.candidate_records = [
        {"candidate_id": str(diff.candidate_id), "diff_id": diff.diff_id}
        for diff in catalog
        if diff.candidate_id
    ]
    candidates = sorted(
        (diff for diff in catalog if diff.candidate_id),
        key=_sort_key,
    )
    candidate_index_by_object = {
        id(candidate): index for index, candidate in enumerate(candidates)
    }
    adjacency: dict[int, set[int]] = {index: set() for index in range(len(candidates))}
    equivalence_adjacency: dict[int, set[int]] = {
        index: set() for index in range(len(candidates))
    }
    relation_reasons: dict[tuple[int, int], str] = {}
    relation_compatibility: dict[tuple[int, int], bool] = {}
    equivalence_compatibility: dict[tuple[int, int], bool] = {}
    equivalence_rejections: Counter[str] = Counter()
    coordinates = {
        index: _candidate_coordinate(diff, baseline=baseline, target=target)
        for index, diff in enumerate(candidates)
    }
    boundary_reasons = {
        index: _boundary_noise_reason(
            diff,
            candidates,
            index,
            baseline=baseline,
            target=target,
        )
        for index, diff in enumerate(candidates)
    }
    equivalence_rejections["BOUNDARY_EVIDENCE_MISSING"] = sum(
        reason == "BOUNDARY_EVIDENCE_MISSING" for reason in boundary_reasons.values()
    )
    formula_equivalence_components = _discover_formula_equivalence_groups(
        candidates,
        baseline=baseline,
        target=target,
        coordinates=coordinates,
    )
    formula_equivalence_indexes = {
        index for component in formula_equivalence_components for index in component
    }
    table_bucket_by_index: dict[int, tuple[Any, ...] | None] = {}
    table_equivalence_buckets: dict[tuple[Any, ...], list[int]] = {}
    for index, candidate in enumerate(candidates):
        bucket_key = _table_equivalence_bucket_key(
            candidate,
            baseline=baseline,
            target=target,
            coordinate=coordinates[index],
        )
        table_bucket_by_index[index] = bucket_key
        if bucket_key is not None:
            table_equivalence_buckets.setdefault(bucket_key, []).append(index)
    overmerged_equivalence_buckets = {
        key
        for key, indexes in table_equivalence_buckets.items()
        if len(indexes) > _EQUIVALENCE_CLUSTER_MAX_SIZE
    }
    overmerged_equivalence_indexes = {
        index
        for key, indexes in table_equivalence_buckets.items()
        if key in overmerged_equivalence_buckets
        for index in indexes
    }
    for left_index, left in enumerate(candidates):
        for right_index in range(left_index + 1, len(candidates)):
            right = candidates[right_index]
            # Boundary-labelled parser noise is deterministic and must not be
            # allowed to acquire a logical-change or equivalent cluster.
            if boundary_reasons[left_index] == "BOUNDARY_NOISE" or boundary_reasons[
                right_index
            ] == "BOUNDARY_NOISE":
                continue
            left_coordinate = coordinates[left_index]
            right_coordinate = coordinates[right_index]
            left_bucket = table_bucket_by_index[left_index]
            right_bucket = table_bucket_by_index[right_index]
            if left_bucket is not None or right_bucket is not None:
                if left_bucket == right_bucket and left_bucket in overmerged_equivalence_buckets:
                    equivalent, equivalence_reason = (
                        False,
                        "EQUIVALENT_COMPONENT_OVERMERGED",
                    )
                elif left_bucket is not None and left_bucket == right_bucket:
                    equivalent, equivalence_reason = True, "TABLE_FIELD_VALUE_EQUIVALENCE"
                else:
                    equivalent, equivalence_reason = False, "FIELD_MISMATCH"
            elif (
                left_coordinate.get("kind") == "PARAGRAPH"
                and right_coordinate.get("kind") == "PARAGRAPH"
                and (baseline is not None or target is not None)
            ):
                # Formula equivalence is decided at group level below.  Pair
                # edges here would make unrelated fragments transitively
                # equivalent before their complete composite is checked.
                equivalent, equivalence_reason = False, "COMPOSITE_EQUIVALENCE_REQUIRED"
            else:
                equivalent, equivalence_reason = _equivalence_candidate_pair_reason(
                    left,
                    right,
                    baseline=baseline,
                    target=target,
                    left_coordinate=left_coordinate,
                    right_coordinate=right_coordinate,
                )
            if not equivalent and equivalence_reason in _EQUIVALENCE_REJECTION_CODES:
                equivalence_rejections[equivalence_reason] += 1
            equivalence_compatibility[(left_index, right_index)] = equivalent
            equivalence_compatibility[(right_index, left_index)] = equivalent
            if equivalent:
                equivalence_adjacency[left_index].add(right_index)
                equivalence_adjacency[right_index].add(left_index)
            related, reason = _candidate_relation(
                left,
                right,
                baseline=baseline,
                target=target,
                left_coordinate=coordinates[left_index],
                right_coordinate=coordinates[right_index],
            )
            relation_compatibility[(left_index, right_index)] = related
            relation_compatibility[(right_index, left_index)] = related
            if not related:
                continue
            adjacency[left_index].add(right_index)
            adjacency[right_index].add(left_index)
            relation_reasons[(left_index, right_index)] = reason

    # Two target-side fragments are related only when they share the same
    # already-discovered baseline replacement block.  This keeps a local
    # split such as ``25/26 -> 27`` together without turning every pair of
    # nearby additions in one chapter into a logical change.
    for left_index, left in enumerate(candidates):
        for right_index in range(left_index + 1, len(candidates)):
            right = candidates[right_index]
            if not _same_side_paragraph_fragment_relation(
                left,
                right,
                coordinates[left_index],
                coordinates[right_index],
            ):
                continue
            shared_counterpart = any(
                counterpart_index not in {left_index, right_index}
                and candidates[counterpart_index].diff_type == "DELETED"
                and relation_compatibility.get(
                    (left_index, counterpart_index), False
                )
                and relation_compatibility.get(
                    (right_index, counterpart_index), False
                )
                for counterpart_index in range(len(candidates))
            )
            if not shared_counterpart:
                continue
            relation_compatibility[(left_index, right_index)] = True
            relation_compatibility[(right_index, left_index)] = True
            adjacency[left_index].add(right_index)
            adjacency[right_index].add(left_index)
            relation_reasons[(left_index, right_index)] = (
                "PARAGRAPH_SPLIT_SHARED_COUNTERPART"
            )

    # A heading plus its list/body fragments may be consecutive deletions
    # whose pairwise wording is unrelated.  Add a stronger, scoped relation
    # for that case before complete-compatible grouping.  The complete-group
    # solver still requires every pair in a returned group to carry this
    # relation and applies the four-fragment limit.
    contiguous_deletion_runs = _contiguous_deletion_block_runs(
        candidates, coordinates
    )
    contiguous_deletion_relation_count = 0
    for run in contiguous_deletion_runs:
        for left_offset, left_index in enumerate(run):
            for right_index in run[left_offset + 1 :]:
                if relation_compatibility.get((left_index, right_index), False):
                    continue
                relation_compatibility[(left_index, right_index)] = True
                relation_compatibility[(right_index, left_index)] = True
                adjacency[left_index].add(right_index)
                adjacency[right_index].add(left_index)
                relation_reasons[(left_index, right_index)] = (
                    "CONTIGUOUS_DELETION_BLOCK"
                )
                contiguous_deletion_relation_count += 1

    relation_components = [
        sorted(component, key=lambda index: _sort_key(candidates[index]))
        for component in _component_indexes(adjacency)
    ]
    equivalence_components = [
        sorted(component, key=lambda index: _sort_key(candidates[index]))
        for component in _component_indexes(equivalence_adjacency)
        if len(component) >= 2
        and not set(component) & formula_equivalence_indexes
    ]
    equivalence_indexes = {
        index for component in equivalence_components for index in component
    }
    logical_components: list[list[int]] = []
    logical_singleton_count = 0
    def cached_relation(candidate: DiffItem, member: DiffItem) -> bool:
        candidate_index = candidate_index_by_object[id(candidate)]
        member_index = candidate_index_by_object[id(member)]
        return relation_compatibility.get((candidate_index, member_index), False)

    for component in relation_components:
        eligible = [
            index
            for index in component
            if index not in equivalence_indexes
            and index not in formula_equivalence_indexes
            and index not in overmerged_equivalence_indexes
        ]
        if not eligible:
            continue
        complete_groups, singleton_count = _complete_relation_groups(
            eligible,
            candidates,
            compatible=cached_relation,
            max_size=_LOGICAL_CLUSTER_MAX_SIZE,
        )
        logical_components.extend(complete_groups)
        logical_singleton_count += singleton_count
    complete_equivalence_components: list[list[int]] = []
    equivalence_singleton_count = 0
    def cached_equivalence(candidate: DiffItem, member: DiffItem) -> bool:
        candidate_index = candidate_index_by_object[id(candidate)]
        member_index = candidate_index_by_object[id(member)]
        return equivalence_compatibility.get(
            (candidate_index, member_index), False
        )

    for component in equivalence_components:
        complete_groups, singleton_count = _complete_relation_groups(
            component,
            candidates,
            compatible=cached_equivalence,
            max_size=_EQUIVALENCE_CLUSTER_MAX_SIZE,
        )
        complete_equivalence_components.extend(complete_groups)
        equivalence_singleton_count += singleton_count
    # Formula groups have already passed the composite, group-level check;
    # rechecking them pairwise would incorrectly split fragments whose
    # individual text is only meaningful after concatenation.
    complete_equivalence_components.extend(formula_equivalence_components)

    clusters: list[DuplicateCluster] = []
    clustered_indexes: set[int] = set()
    for component in logical_components:
        indexes = component
        clustered_indexes.update(indexes)
        group = [candidates[index] for index in indexes]
        candidate_ids = [str(diff.candidate_id) for diff in group]
        group_id = _group_id(candidate_ids)
        coordinate_keys = {
            repr(_coordinate_key(_candidate_coordinate(diff, baseline=baseline, target=target)))
            for diff in group
        }
        relation_reason = "COMPLETE_BUSINESS_COMPATIBILITY"
        if len(coordinate_keys) == 1:
            relation_reason = "SAME_BUSINESS_COORDINATE"
        cluster_id = _cluster_id(candidate_ids)
        candidate_kind = _cluster_kind(group)
        discovery_action = (
            "EQUIVALENT_NO_CHANGE"
            if _equivalent_group_is_safe(group, baseline=baseline, target=target)
            else "SAME_LOGICAL_CHANGE"
        )
        coordinate_kinds = {
            _candidate_coordinate(item, baseline=baseline, target=target).get("kind")
            for item in group
        }
        clusters.append(
            DuplicateCluster(
                cluster_id=cluster_id,
                candidate_ids=tuple(candidate_ids),
                relation_reason=relation_reason,
                discovery_action=discovery_action,
                payload={
                    "group_id": group_id,
                    "cluster_id": cluster_id,
                    "candidate_ids": candidate_ids,
                    "candidate_kind": candidate_kind,
                    "candidate_coordinate_kind": (
                        next(iter(coordinate_kinds))
                        if len(coordinate_kinds) == 1
                        else "MIXED"
                    ),
                    "discovery_action": discovery_action,
                    "coordinate_keys": sorted(coordinate_keys),
                    "candidates": [
                        _candidate_payload(diff, baseline, target) for diff in group
                    ],
                    "requirements": {
                        "same_logical_diff_only": True,
                        "preserve_all_locations": True,
                        "allowed_decisions": [
                            "SAME_LOGICAL_CHANGE",
                            "EQUIVALENT_NO_CHANGE",
                            "DISTINCT_CHANGES",
                            "UNCERTAIN",
                        ],
                    },
                },
            )
        )
    # Equivalent groups are built from strong table buckets and formula
    # relations only.  They never consume candidates already assigned to a
    # complete logical-change group.
    for component in complete_equivalence_components:
        if set(component) & clustered_indexes:
            continue
        indexes = component
        clustered_indexes.update(indexes)
        group = [candidates[index] for index in indexes]
        candidate_ids = [str(diff.candidate_id) for diff in group]
        cluster_id = _cluster_id(candidate_ids)
        group_id = _group_id(candidate_ids)
        coordinate_kinds = {
            _candidate_coordinate(item, baseline=baseline, target=target).get("kind")
            for item in group
        }
        clusters.append(
            DuplicateCluster(
                cluster_id=cluster_id,
                candidate_ids=tuple(candidate_ids),
                relation_reason="COMPLETE_EQUIVALENCE_COMPATIBILITY",
                discovery_action="EQUIVALENT_NO_CHANGE",
                payload={
                    "group_id": group_id,
                    "cluster_id": cluster_id,
                    "candidate_ids": candidate_ids,
                    "candidate_kind": "TABLE_LAYOUT_EQUIVALENCE"
                    if coordinate_kinds == {"TABLE"}
                    else "FORMULA_LAYOUT_EQUIVALENCE",
                    "candidate_coordinate_kind": (
                        next(iter(coordinate_kinds))
                        if len(coordinate_kinds) == 1
                        else "MIXED"
                    ),
                    "discovery_action": "EQUIVALENT_NO_CHANGE",
                    "coordinate_keys": sorted(
                        repr(
                            _coordinate_key(
                                _candidate_coordinate(
                                    diff, baseline=baseline, target=target
                                )
                            )
                        )
                        for diff in group
                    ),
                    "candidates": [
                        _candidate_payload(diff, baseline, target) for diff in group
                    ],
                    "requirements": {
                        "same_logical_diff_only": True,
                        "preserve_all_locations": True,
                        "allowed_decisions": [
                            "SAME_LOGICAL_CHANGE",
                            "EQUIVALENT_NO_CHANGE",
                            "DISTINCT_CHANGES",
                            "UNCERTAIN",
                        ],
                    },
                },
            )
        )
    sort_keys = {
        str(candidate.candidate_id): _sort_key(candidate)
        for candidate in candidates
        if candidate.candidate_id
    }
    clusters.sort(
        key=lambda cluster: min(
            sort_keys[candidate_id] for candidate_id in cluster.candidate_ids
        )
    )
    boundary_noise_ids = [
        str(diff.candidate_id)
        for index, diff in enumerate(candidates)
        if boundary_reasons[index] == "BOUNDARY_NOISE"
    ]
    comparison.validation_metadata = {
        **comparison.validation_metadata,
        "candidate_discovery": {
            "candidate_count": len(candidates),
            "relation_edge_count": sum(len(items) for items in adjacency.values()) // 2,
            "connected_component_count": len(relation_components),
            "relation_component_count": len(relation_components),
            "complete_logical_group_count": len(logical_components),
            "complete_logical_singleton_count": logical_singleton_count,
            "complete_equivalence_group_count": len(complete_equivalence_components),
            "complete_equivalence_singleton_count": equivalence_singleton_count,
            "equivalence_overmerged_count": len(overmerged_equivalence_buckets),
            "equivalence_overmerged_candidate_count": sum(
                len(indexes)
                for key, indexes in table_equivalence_buckets.items()
                if key in overmerged_equivalence_buckets
            ),
            "failure_code": (
                "EQUIVALENT_COMPONENT_OVERMERGED"
                if overmerged_equivalence_buckets
                else None
            ),
            "cluster_count": len(clusters),
            "clustered_candidate_count": sum(len(item.candidate_ids) for item in clusters),
            "unclustered_candidate_count": len(candidates)
            - sum(len(item.candidate_ids) for item in clusters),
            "relation_reasons": dict(Counter(relation_reasons.values())),
            "contiguous_deletion_block_relation_count": (
                contiguous_deletion_relation_count
            ),
            "equivalence_edge_count": sum(
                len(items) for items in equivalence_adjacency.values()
            )
            // 2,
            "equivalence_rejection_counts": dict(equivalence_rejections),
            "boundary_noise_candidate_ids": boundary_noise_ids,
            "boundary_noise_count": len(boundary_noise_ids),
            "boundary_noise_rejection_count": sum(
                reason == "BOUNDARY_EVIDENCE_MISSING"
                for reason in boundary_reasons.values()
            ),
        },
    }
    return clusters


def apply_deterministic_final_compare_filters(
    comparison: ComparisonResult,
    *,
    baseline: ParsedDocument | None = None,
    target: ParsedDocument | None = None,
) -> ComparisonResult:
    """Apply only the deterministic, evidence-preserving V2 filters.

    This is the stable production path.  It may remove a candidate only when
    the existing programmatic equivalence and boundary gates prove that the
    candidate is parser/layout noise.  Logical-change clusters are diagnostic
    only here: without the explicit adjudication switch they remain ordinary
    risks, so a model can never silently remove a real change.
    """

    raw_candidate_count = len(comparison.diff_items)
    clusters = build_suspected_duplicate_clusters(
        comparison, baseline=baseline, target=target
    )
    by_id = {
        str(diff.candidate_id): diff
        for diff in comparison.diff_items
        if diff.candidate_id
    }
    discovery = comparison.validation_metadata.get("candidate_discovery", {})
    boundary_ids = {
        str(candidate_id)
        for candidate_id in discovery.get("boundary_noise_candidate_ids", ())
        if str(candidate_id) in by_id
    }
    equivalent_groups: list[DuplicateCluster] = []
    equivalent_ids: set[str] = set()
    for cluster in clusters:
        if cluster.discovery_action != "EQUIVALENT_NO_CHANGE":
            continue
        group = [
            by_id[candidate_id]
            for candidate_id in cluster.candidate_ids
            if candidate_id in by_id
        ]
        if len(group) != len(cluster.candidate_ids):
            continue
        if not _equivalent_no_change_is_safe(
            group, baseline=baseline, target=target
        ):
            continue
        equivalent_groups.append(cluster)
        equivalent_ids.update(cluster.candidate_ids)

    removed_ids = equivalent_ids | boundary_ids
    for cluster in equivalent_groups:
        comparison.dedup_groups.append(
            {
                "reason_code": "EQUIVALENT_NO_CHANGE",
                "group_id": cluster.group_id,
                "cluster_id": cluster.cluster_id,
                "kept_candidate_id": None,
                "removed_candidate_ids": list(cluster.candidate_ids),
                "baseline_locations": [
                    location
                    for candidate_id in cluster.candidate_ids
                    for location in _location_payload(by_id[candidate_id].baseline)
                ],
                "target_locations": [
                    location
                    for candidate_id in cluster.candidate_ids
                    for location in _location_payload(by_id[candidate_id].target)
                ],
            }
        )
    for candidate_id in sorted(boundary_ids):
        comparison.dedup_groups.append(
            {
                "reason_code": "BOUNDARY_NOISE",
                "group_id": None,
                "cluster_id": None,
                "kept_candidate_id": None,
                "removed_candidate_ids": [candidate_id],
                "baseline_locations": _location_payload(by_id[candidate_id].baseline),
                "target_locations": _location_payload(by_id[candidate_id].target),
            }
        )

    comparison.diff_items = [
        diff for diff in comparison.diff_items if str(diff.candidate_id) not in removed_ids
    ]
    comparison.diff_items = [
        diff.model_copy(update={"diff_id": f"diff_{index:06d}"})
        for index, diff in enumerate(comparison.diff_items, start=1)
    ]
    comparison.candidate_records = [
        record
        for record in comparison.candidate_records
        if str(record.get("candidate_id")) not in removed_ids
    ]

    stats = dict(comparison.validation_stats)
    stats.update(
        {
            "raw_candidate_count": raw_candidate_count,
            "equivalent_filtered_count": len(equivalent_ids),
            "boundary_noise_filtered_count": len(boundary_ids),
            "final_published_risk_count": len(comparison.diff_items),
            "llm_diff_adjudication_calls": 0,
            "suspected_cluster_count": len(clusters),
            "deterministic_equivalent_group_count": len(equivalent_groups),
            "review_required_count": sum(
                diff.validation_status == "REVIEW_REQUIRED"
                for diff in comparison.diff_items
            ),
        }
    )
    metadata = dict(comparison.validation_metadata)
    metadata["deterministic_filter"] = {
        "raw_candidate_count": raw_candidate_count,
        "equivalent_filtered_count": len(equivalent_ids),
        "boundary_noise_filtered_count": len(boundary_ids),
        "final_published_risk_count": len(comparison.diff_items),
        "llm_diff_adjudication_calls": 0,
        "equivalent_group_count": len(equivalent_groups),
    }
    comparison.validation_stats = stats
    comparison.validation_metadata = metadata
    comparison.diagnostics = comparison.diagnostics.model_copy(
        update={"emitted_diff_count": len(comparison.diff_items)}
    )
    return comparison


def _cluster_kind(group: list[DiffItem]) -> str:
    """Classify a cluster for safe diagnostics and Canary selection only."""

    if any(_candidate_value_tokens(diff) for diff in group):
        return "VALUE_CHANGE_OR_VALUE_CONTEXT"
    if any(diff.diff_type in {"ADDED", "DELETED"} for diff in group):
        return "PARAGRAPH_OR_BLOCK_REPLACEMENT"
    if all(
        diff.baseline is not None
        and diff.target is not None
        and _normalized(diff.baseline) == _normalized(diff.target)
        for diff in group
    ):
        return "EQUIVALENT_LAYOUT_OR_REPETITION"
    return "INDEPENDENT_OR_AMBIGUOUS_CHANGE"


def _merge_locations(left: DiffSide | None, right: DiffSide | None) -> DiffSide | None:
    if left is None:
        return right
    if right is None:
        return left
    seen: set[tuple[Any, ...]] = set()
    locations = []
    for location in [*_locations(left), *_locations(right)]:
        key = (
            location.page,
            location.paragraph_index,
            location.table_index,
            location.row,
            location.column,
            location.section,
            location.source,
        )
        if key not in seen:
            seen.add(key)
            locations.append(location)
    return left.model_copy(update={"location": locations[0], "locations": locations})


def _merge_diff(left: DiffItem, right: DiffItem) -> DiffItem:
    return left.model_copy(
        update={
            "baseline": _merge_locations(left.baseline, right.baseline),
            "target": _merge_locations(left.target, right.target),
        }
    )


_DIFF_TITLES = {
    "ADDED": "目标文件新增内容",
    "DELETED": "目标文件缺少内容",
    "MODIFIED": "文字内容发生变化",
    "NUMERIC_CHANGED": "数值、金额、比例、日期或期限发生变化",
    "TABLE_ROW_ADDED": "目标表格新增行",
    "TABLE_ROW_DELETED": "目标表格缺少行",
    "TABLE_CELL_CHANGED": "表格单元格发生变化",
    "TABLE_STRUCTURE_EXPANDED": "模板表格结构发生变化",
    "PAGE_MISSING": "页面内容缺失",
    "CONTENT_BLOCK_MISSING": "连续内容缺失",
}


def _merged_diff_type(diffs: list[DiffItem]) -> str:
    if any(diff.diff_type == "NUMERIC_CHANGED" for diff in diffs):
        return "NUMERIC_CHANGED"
    has_baseline = any(diff.baseline is not None for diff in diffs)
    has_target = any(diff.target is not None for diff in diffs)
    if has_baseline and has_target:
        if any(diff.diff_type == "TABLE_CELL_CHANGED" for diff in diffs):
            return "TABLE_CELL_CHANGED"
        return "MODIFIED"
    if has_baseline:
        if any(diff.diff_type == "CONTENT_BLOCK_MISSING" for diff in diffs):
            return "CONTENT_BLOCK_MISSING"
        return "DELETED"
    return "ADDED"


def _merge_group_diffs(diffs: list[DiffItem]) -> DiffItem:
    """Merge evidence only after an application-layer decision is safe."""

    representative = min(
        diffs,
        key=lambda diff: (_DIFF_PRIORITY.get(diff.diff_type, 99), _sort_key(diff)),
    )
    merged = representative
    for diff in diffs:
        if diff is not representative:
            merged = _merge_diff(merged, diff)
    diff_type = _merged_diff_type(diffs)
    return merged.model_copy(
        update={
            "diff_type": diff_type,
            "title": _DIFF_TITLES.get(diff_type, representative.title),
        }
    )


def _mark_review(diff: DiffItem, reason: str) -> DiffItem:
    return diff.model_copy(
        update={
            "validation_status": "REVIEW_REQUIRED",
            "validation_source": "LLM",
            "validation_reason_code": reason,
        }
    )


def _mark_confirmed(diff: DiffItem, reason: str) -> DiffItem:
    return diff.model_copy(
        update={
            "validation_status": "CONFIRMED",
            "validation_source": "RULE_AND_LLM",
            "validation_reason_code": reason,
        }
    )


def _safe_decision(value: Any, cluster: DuplicateCluster) -> dict[str, Any] | None:
    """Normalize the current group wire contract and the legacy fixture shape."""

    if not isinstance(value, dict):
        return None
    if "group_id" in value or "candidate_ids" in value:
        group_id = value.get("group_id")
        candidate_ids = value.get("candidate_ids")
        decision = value.get("decision")
        if not isinstance(group_id, str) or not isinstance(candidate_ids, list):
            return None
        if not all(isinstance(item, str) for item in candidate_ids):
            return None
        return {
            "group_id": group_id,
            "candidate_ids": candidate_ids,
            "decision": decision,
            "reason_code": value.get("reason_code"),
            "confidence": value.get("confidence"),
        }
    try:
        legacy = FinalCompareDuplicateClusterDecision.model_validate(value)
    except Exception:  # noqa: BLE001 - invalid model decisions are non-fatal
        return None
    decision_map = {
        "SAME_LOGICAL_DIFF": "SAME_LOGICAL_CHANGE",
        "DISTINCT_DIFFS": "DISTINCT_CHANGES",
    }
    candidate_ids = list(cluster.candidate_ids)
    if legacy.decision == "SAME_LOGICAL_DIFF":
        candidate_ids = [
            legacy.representative_candidate_id,
            *legacy.duplicate_candidate_ids,
        ]
    return {
        "group_id": cluster.group_id,
        "candidate_ids": candidate_ids,
        "decision": decision_map.get(legacy.decision, "UNCERTAIN"),
        "reason_code": legacy.reason_code,
        "confidence": legacy.confidence,
    }


def _decision_ids_are_safe(
    decision: dict[str, Any], cluster: DuplicateCluster
) -> bool:
    candidate_ids = decision.get("candidate_ids")
    return (
        decision.get("group_id") == cluster.group_id
        and isinstance(candidate_ids, list)
        and len(candidate_ids) == len(set(candidate_ids))
        and set(candidate_ids) == set(cluster.candidate_ids)
    )


def _equivalent_no_change_is_safe(
    diffs: list[DiffItem],
    *,
    baseline: ParsedDocument | None,
    target: ParsedDocument | None,
) -> bool:
    return _equivalent_group_is_safe(diffs, baseline=baseline, target=target)


def _decision_is_safe(
    decision: dict[str, Any],
    cluster: DuplicateCluster,
    by_id: dict[str, DiffItem],
    *,
    baseline: ParsedDocument | None = None,
    target: ParsedDocument | None = None,
) -> bool:
    if not _decision_ids_are_safe(decision, cluster):
        return False
    confidence = decision.get("confidence")
    if not isinstance(confidence, (int, float)):
        return False
    diffs = [by_id[item] for item in cluster.candidate_ids]
    action = decision.get("decision")
    required_confidence = (
        _EQUIVALENT_CONFIDENCE
        if action == "EQUIVALENT_NO_CHANGE"
        else _CLUSTER_CONFIDENCE
    )
    if confidence < required_confidence:
        return False
    if action == "EQUIVALENT_NO_CHANGE":
        return _equivalent_no_change_is_safe(
            diffs, baseline=baseline, target=target
        )
    if action != "SAME_LOGICAL_CHANGE":
        return False
    # A connected path is not enough evidence: every member must be directly
    # compatible with every other member in the proposed logical group.
    return all(
        _can_group_logical_candidates(
            left,
            right,
            baseline=baseline,
            target=target,
        )
        for index, left in enumerate(diffs)
        for right in diffs[index + 1 :]
    )


async def validate_final_compare_duplicate_clusters(
    comparison: ComparisonResult,
    llm: Any,
    *,
    baseline: ParsedDocument | None = None,
    target: ParsedDocument | None = None,
    batch_size: int = _CLUSTER_BATCH_SIZE,
    recovery_batch_size: int = _CLUSTER_RECOVERY_SIZE,
) -> ComparisonResult:
    """Validate only suspected duplicate clusters and retain failures safely."""

    clusters = build_suspected_duplicate_clusters(
        comparison, baseline=baseline, target=target
    )
    stats = Counter(comparison.validation_stats)
    stats.update(
        {
            "suspected_cluster_count": len(clusters),
            "suspected_candidate_count": sum(len(item.candidate_ids) for item in clusters),
            "llm_cluster_count": 0,
            "llm_logical_group_count": 0,
            "llm_same_logical_count": 0,
            "llm_same_logical_change_count": 0,
            "llm_equivalent_no_change_count": 0,
            "llm_distinct_count": 0,
            "llm_distinct_change_count": 0,
            "llm_uncertain_count": 0,
            "llm_removed_candidate_count": 0,
            "validation_failure_count": 0,
            "llm_reviewed_count": 0,
            "llm_duplicate_removed_count": 0,
            "candidate_validation_failures": 0,
        }
    )
    metadata: dict[str, Any] = {
        "purpose": "FINAL_COMPARE_DUPLICATE_CLUSTER_VALIDATION",
        "logical_call_count": 0,
        "configured_model": None,
        "actual_model": None,
        "finish_reasons": {},
        "response_formats": {},
        "failure_codes": {},
    }
    if not clusters:
        comparison.validation_stats = dict(stats)
        comparison.validation_metadata = metadata
        return comparison

    by_id = {
        str(diff.candidate_id): diff
        for diff in comparison.diff_items
        if diff.candidate_id
    }
    cluster_by_id = {cluster.cluster_id: cluster for cluster in clusters}
    group_by_id = {cluster.group_id: cluster for cluster in clusters}
    decisions: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    logical_calls = 0

    async def execute(batch: list[DuplicateCluster]) -> None:
        nonlocal missing, logical_calls
        if logical_calls >= _CLUSTER_MAX_LOGICAL_CALLS:
            missing.extend(cluster.cluster_id for cluster in batch)
            return
        stats["llm_cluster_count"] += len(batch)
        stats["llm_logical_group_count"] += len(batch)
        stats["llm_reviewed_count"] += len(batch)
        try:
            # ``clusters`` remains in the payload for old in-repo fixtures;
            # production clients consume the new ``groups`` key.
            payload = {
                "groups": [cluster.payload for cluster in batch],
                "clusters": [cluster.payload for cluster in batch],
            }
            logical_calls += 1
            metadata["logical_call_count"] += 1
            result = await llm.validate_final_compare_duplicate_clusters(
                payload
            )
            metadata["configured_model"] = result.configured_model
            metadata["actual_model"] = result.actual_model
            if result.finish_reason:
                finish_reasons = metadata["finish_reasons"]
                finish_reasons[result.finish_reason] = (
                    finish_reasons.get(result.finish_reason, 0) + 1
                )
            if result.response_format:
                response_formats = metadata["response_formats"]
                response_formats[result.response_format] = response_formats.get(
                    result.response_format, 0
                ) + 1
            returned_ids: set[str] = set()
            raw_decisions = result.value.get("groups")
            response_key = "group_id"
            if not isinstance(raw_decisions, list):
                raw_decisions = result.value.get("clusters", [])
                response_key = "cluster_id"
            for raw in raw_decisions:
                group_hint = raw.get(response_key) if isinstance(raw, dict) else None
                cluster = group_by_id.get(group_hint) or cluster_by_id.get(group_hint)
                if cluster is None:
                    raise ValueError("invalid logical group decision")
                decision = _safe_decision(raw, cluster)
                if decision is None or decision["group_id"] in returned_ids:
                    raise ValueError("duplicate or invalid logical group decision")
                returned_ids.add(decision["group_id"])
                decisions[cluster.cluster_id] = decision
            expected_ids = {cluster.group_id for cluster in batch}
            missing.extend(
                cluster_by_id[group_by_id[item].cluster_id].cluster_id
                for item in sorted(expected_ids - returned_ids)
            )
        except Exception as error:  # noqa: BLE001 - cluster validation is non-fatal
            stats["validation_failure_count"] += 1
            code = str(
                getattr(error, "failure_code", None)
                or getattr(error, "code", None)
                or type(error).__name__
            )
            failure_codes = metadata["failure_codes"]
            failure_codes[code] = failure_codes.get(code, 0) + 1
            missing.extend(cluster.cluster_id for cluster in batch)

    for start in range(0, len(clusters), min(batch_size, _CLUSTER_BATCH_SIZE)):
        await execute(clusters[start : start + min(batch_size, _CLUSTER_BATCH_SIZE)])
        if logical_calls >= _CLUSTER_MAX_LOGICAL_CALLS:
            break
    if missing:
        recovery = [cluster_by_id[item] for item in dict.fromkeys(missing)]
        missing = []
        for start in range(0, len(recovery), min(recovery_batch_size, _CLUSTER_RECOVERY_SIZE)):
            await execute(
                recovery[start : start + min(recovery_batch_size, _CLUSTER_RECOVERY_SIZE)]
            )
            if logical_calls >= _CLUSTER_MAX_LOGICAL_CALLS:
                break
        if logical_calls >= _CLUSTER_MAX_LOGICAL_CALLS:
            missing.extend(
                cluster.cluster_id for cluster in recovery if cluster.cluster_id not in decisions
            )

    removed_ids: set[str] = set()
    updated: dict[str, DiffItem] = {}
    for cluster in clusters:
        decision = decisions.get(cluster.cluster_id)
        if decision is None:
            for candidate_id in cluster.candidate_ids:
                updated[candidate_id] = _mark_review(
                    by_id[candidate_id], "LLM_CLUSTER_VALIDATION_FAILED"
                )
            continue
        action = decision.get("decision")
        if action == "DISTINCT_CHANGES":
            stats["llm_distinct_count"] += 1
            stats["llm_distinct_change_count"] += 1
            for candidate_id in cluster.candidate_ids:
                updated[candidate_id] = _mark_confirmed(
                    by_id[candidate_id], "DISTINCT_CHANGES"
                )
            continue
        if action == "UNCERTAIN":
            stats["llm_uncertain_count"] += 1
            for candidate_id in cluster.candidate_ids:
                updated[candidate_id] = _mark_review(by_id[candidate_id], "REVIEW_REQUIRED")
            continue
        if action == "EQUIVALENT_NO_CHANGE":
            safe = _decision_is_safe(
                decision, cluster, by_id, baseline=baseline, target=target
            )
            if not safe:
                stats["validation_failure_count"] += 1
                for candidate_id in cluster.candidate_ids:
                    updated[candidate_id] = _mark_review(
                        by_id[candidate_id], "LLM_LOGICAL_DECISION_INVALID"
                    )
                continue
            stats["llm_equivalent_no_change_count"] += 1
            removed_ids.update(cluster.candidate_ids)
            comparison.dedup_groups.append(
                {
                    "reason_code": decision.get("reason_code") or "EQUIVALENT_NO_CHANGE",
                    "group_id": cluster.group_id,
                    "cluster_id": cluster.cluster_id,
                    "kept_candidate_id": None,
                    "removed_candidate_ids": list(cluster.candidate_ids),
                    "baseline_locations": [
                        location
                        for candidate_id in cluster.candidate_ids
                        for location in _location_payload(by_id[candidate_id].baseline)
                    ],
                    "target_locations": [
                        location
                        for candidate_id in cluster.candidate_ids
                        for location in _location_payload(by_id[candidate_id].target)
                    ],
                }
            )
            stats["llm_removed_candidate_count"] += len(cluster.candidate_ids)
            continue
        if not _decision_is_safe(
            decision, cluster, by_id, baseline=baseline, target=target
        ):
            stats["validation_failure_count"] += 1
            for candidate_id in cluster.candidate_ids:
                updated[candidate_id] = _mark_review(
                    by_id[candidate_id], "LLM_LOGICAL_DECISION_INVALID"
                )
            continue
        stats["llm_same_logical_count"] += 1
        stats["llm_same_logical_change_count"] += 1
        representative = min(
            (by_id[candidate_id] for candidate_id in cluster.candidate_ids),
            key=lambda diff: (_DIFF_PRIORITY.get(diff.diff_type, 99), _sort_key(diff)),
        )
        merged = _merge_group_diffs(
            [by_id[candidate_id] for candidate_id in cluster.candidate_ids]
        )
        removed_from_cluster: list[str] = []
        for candidate_id in cluster.candidate_ids:
            if candidate_id != representative.candidate_id:
                removed_ids.add(candidate_id)
                removed_from_cluster.append(candidate_id)
        updated[str(representative.candidate_id)] = _mark_confirmed(
            merged, decision.get("reason_code") or "SAME_LOGICAL_CHANGE"
        )
        stats["llm_removed_candidate_count"] += len(removed_from_cluster)
        comparison.dedup_groups.append(
            {
                "reason_code": decision.get("reason_code") or "SAME_LOGICAL_CHANGE",
                "group_id": cluster.group_id,
                "cluster_id": cluster.cluster_id,
                "kept_candidate_id": representative.candidate_id,
                "removed_candidate_ids": removed_from_cluster,
                "baseline_locations": _location_payload(merged.baseline),
                "target_locations": _location_payload(merged.target),
            }
        )

    comparison.diff_items = [
        updated.get(str(diff.candidate_id), diff)
        for diff in comparison.diff_items
        if not diff.candidate_id or str(diff.candidate_id) not in removed_ids
    ]
    comparison.diff_items = [
        diff.model_copy(update={"diff_id": f"diff_{index:06d}"})
        for index, diff in enumerate(comparison.diff_items, start=1)
    ]
    comparison.candidate_records = [
        record
        for record in comparison.candidate_records
        if record.get("candidate_id") not in removed_ids
    ]
    stats["final_diff_count"] = len(comparison.diff_items)
    stats["review_required_count"] = sum(
        diff.validation_status == "REVIEW_REQUIRED" for diff in comparison.diff_items
    )
    comparison.diagnostics = comparison.diagnostics.model_copy(
        update={"emitted_diff_count": len(comparison.diff_items)}
    )
    comparison.validation_stats = dict(stats)
    comparison.validation_metadata = metadata
    return comparison


def cluster_audit_summary(
    clusters: list[DuplicateCluster],
    *,
    comparison: ComparisonResult | None = None,
    baseline: ParsedDocument | None = None,
    target: ParsedDocument | None = None,
) -> list[dict[str, Any]]:
    """Return safe local diagnostics without storing contract text."""

    result: list[dict[str, Any]] = []
    by_id = (
        {
            str(diff.candidate_id): diff
            for diff in comparison.diff_items
            if diff.candidate_id
        }
        if comparison is not None
        else {}
    )
    for cluster in clusters:
        candidates = cluster.payload.get("candidates", [])
        summary: dict[str, Any] = {
                "group_id": cluster.group_id,
                "cluster_id": cluster.cluster_id,
                "candidate_kind": cluster.payload.get("candidate_kind"),
                "relation_reason": cluster.relation_reason,
                "candidate_count": len(cluster.candidate_ids),
                "candidate_ids": list(cluster.candidate_ids),
                "candidate_diff_types": [
                    item.get("diff_type") for item in candidates
                ],
                "candidate_texts": [
                    {
                        "baseline": _safe_text(
                            (item.get("baseline") or {}).get("text")
                        ),
                        "target": _safe_text(
                            (item.get("target") or {}).get("text")
                        ),
                    }
                    for item in candidates
                ],
                "locations": [
                    {
                        "baseline": (item.get("baseline") or {}).get("locations", []),
                        "target": (item.get("target") or {}).get("locations", []),
                    }
                    for item in candidates
                ],
        }
        if by_id and all(candidate_id in by_id for candidate_id in cluster.candidate_ids):
            summary["topology_fingerprint"] = candidate_topology_fingerprint(
                cluster.candidate_ids,
                by_id,
                baseline=baseline,
                target=target,
            )
        result.append(summary)
    return result


def build_candidate_discovery_audit(
    comparison: ComparisonResult,
    clusters: list[DuplicateCluster],
) -> dict[str, Any]:
    """Return safe diagnostics for the deterministic discovery graph."""

    discovery = comparison.validation_metadata.get("candidate_discovery", {})
    kind_counts = Counter(
        str(cluster.payload.get("candidate_kind") or "UNKNOWN")
        for cluster in clusters
    )
    action_counts = Counter(cluster.discovery_action for cluster in clusters)
    action_fragment_counts = Counter()
    for cluster in clusters:
        action_fragment_counts[cluster.discovery_action] += len(cluster.candidate_ids)
    boundary_noise_ids = list(discovery.get("boundary_noise_candidate_ids", ()))
    return {
        "candidate_count": int(discovery.get("candidate_count", len(comparison.diff_items))),
        "relation_edge_count": int(discovery.get("relation_edge_count", 0)),
        "connected_component_count": int(discovery.get("connected_component_count", 0)),
        "relation_component_count": int(discovery.get("relation_component_count", 0)),
        "complete_logical_group_count": int(
            discovery.get("complete_logical_group_count", 0)
        ),
        "complete_logical_singleton_count": int(
            discovery.get("complete_logical_singleton_count", 0)
        ),
        "complete_equivalence_group_count": int(
            discovery.get("complete_equivalence_group_count", 0)
        ),
        "complete_equivalence_singleton_count": int(
            discovery.get("complete_equivalence_singleton_count", 0)
        ),
        "equivalence_overmerged_count": int(
            discovery.get("equivalence_overmerged_count", 0)
        ),
        "equivalence_overmerged_candidate_count": int(
            discovery.get("equivalence_overmerged_candidate_count", 0)
        ),
        "failure_code": discovery.get("failure_code"),
        "cluster_count": len(clusters),
        "candidate_group_count": len(clusters),
        "clustered_candidate_count": sum(len(cluster.candidate_ids) for cluster in clusters),
        "candidate_fragment_count": sum(len(cluster.candidate_ids) for cluster in clusters),
        "unclustered_candidate_count": max(
            0,
            len(comparison.diff_items)
            - sum(len(cluster.candidate_ids) for cluster in clusters),
        ),
        "cluster_kind_counts": dict(kind_counts),
        "cluster_action_counts": dict(action_counts),
        "action_fragment_counts": dict(action_fragment_counts),
        "same_logical_group_count": action_counts.get("SAME_LOGICAL_CHANGE", 0),
        "same_logical_fragment_count": action_fragment_counts.get(
            "SAME_LOGICAL_CHANGE", 0
        ),
        "equivalent_group_count": action_counts.get("EQUIVALENT_NO_CHANGE", 0),
        "equivalent_fragment_count": action_fragment_counts.get(
            "EQUIVALENT_NO_CHANGE", 0
        ),
        "false_positive_discovered_count": action_fragment_counts.get(
            "EQUIVALENT_NO_CHANGE", 0
        )
        + int(discovery.get("boundary_noise_count", len(boundary_noise_ids))),
        "boundary_noise_count": int(
            discovery.get("boundary_noise_count", len(boundary_noise_ids))
        ),
        "boundary_noise_candidate_ids": boundary_noise_ids,
        "cluster_size_histogram": dict(
            Counter(str(len(cluster.candidate_ids)) for cluster in clusters)
        ),
        "relation_reasons": discovery.get("relation_reasons", {}),
        "equivalence_rejection_counts": discovery.get(
            "equivalence_rejection_counts", {}
        ),
        "boundary_noise_rejection_count": int(
            discovery.get("boundary_noise_rejection_count", 0)
        ),
    }


def build_candidate_discovery_gold_audit(
    comparison: ComparisonResult,
    clusters: list[DuplicateCluster],
    manifest: dict[str, Any],
    *,
    baseline: ParsedDocument | None = None,
    target: ParsedDocument | None = None,
) -> dict[str, Any]:
    """Match actual groups to a deidentified topology gold manifest.

    Counts in a manifest are expectations, never evidence.  A group is only a
    match when its opaque topology fingerprint, action, and fragment count
    match exactly once.
    """

    logical_gold = manifest.get("logical_gold") if isinstance(manifest, dict) else None
    if not isinstance(logical_gold, dict):
        return {"status": "FAILED", "failure_code": "GOLD_MANIFEST_INVALID"}
    declared_manifest_digest = manifest.get("manifest_sha256")
    if declared_manifest_digest is not None and (
        not isinstance(declared_manifest_digest, str)
        or declared_manifest_digest != _gold_manifest_digest(manifest)
    ):
        return {"status": "FAILED", "failure_code": "GOLD_MANIFEST_HASH_MISMATCH"}
    capture_metadata = manifest.get("capture_metadata")
    if isinstance(capture_metadata, dict):
        for prefix, document in (("baseline", baseline), ("target", target)):
            if document is None:
                continue
            expected_sha = capture_metadata.get(f"{prefix}_sha256")
            if expected_sha and expected_sha != document.sha256:
                return {"status": "FAILED", "failure_code": "GOLD_MANIFEST_STALE"}
            expected_parser = capture_metadata.get(f"{prefix}_parser_name")
            if expected_parser and expected_parser != document.parser_name:
                return {"status": "FAILED", "failure_code": "GOLD_MANIFEST_STALE"}
            expected_parser_version = capture_metadata.get(
                f"{prefix}_parser_version"
            )
            actual_parser_version = document.parser_metadata.get("parser_version")
            if (
                expected_parser_version
                and expected_parser_version != actual_parser_version
            ):
                return {"status": "FAILED", "failure_code": "GOLD_MANIFEST_STALE"}
    expected_groups = [
        *logical_gold.get("fragment_groups", []),
        *logical_gold.get("equivalent_groups", []),
    ]
    if not expected_groups or any(
        not isinstance(item, dict)
        or not isinstance(item.get("topology_fingerprint"), str)
        or not isinstance(item.get("expected"), str)
        or not isinstance(item.get("fragment_count"), int)
        or item.get("fragment_count", 0) < 2
        for item in expected_groups
    ):
        return {
            "status": "FAILED",
            "failure_code": "GOLD_TOPOLOGY_SIGNATURE_MISSING",
        }
    placeholder_ids = {
        str(item.get("gold_group_id") or item.get("gold_case_id"))
        for item in expected_groups
        if set(str(item["topology_fingerprint"])) == {"0"}
    }
    placeholder_ids.update(
        str(item.get("gold_case_id"))
        for item in logical_gold.get("boundary_noise", [])
        if isinstance(item, dict)
        and set(str(item.get("topology_fingerprint", ""))) == {"0"}
    )
    false_positives = logical_gold.get("false_positives")
    expected_by_id = {
        str(item.get("gold_group_id") or item.get("gold_case_id")): item
        for item in expected_groups
    }
    expected_by_id.update(
        {
            str(item.get("gold_case_id")): item
            for item in logical_gold.get("boundary_noise", [])
            if isinstance(item, dict) and item.get("gold_case_id")
        }
    )
    if false_positives is not None:
        if not isinstance(false_positives, list):
            return {
                "status": "FAILED",
                "failure_code": "GOLD_FALSE_POSITIVE_BINDING_INVALID",
            }
        for item in false_positives:
            if not isinstance(item, dict):
                return {
                    "status": "FAILED",
                    "failure_code": "GOLD_FALSE_POSITIVE_BINDING_INVALID",
                }
            group_id = str(item.get("fragment_group_ref") or "")
            expected = expected_by_id.get(group_id)
            if expected is None or item.get("topology_fingerprint") != expected.get(
                "topology_fingerprint"
            ):
                return {
                    "status": "FAILED",
                    "failure_code": "GOLD_FALSE_POSITIVE_BINDING_INVALID",
                }
    by_id = {
        str(diff.candidate_id): diff
        for diff in comparison.diff_items
        if diff.candidate_id
    }
    actual_by_fingerprint: dict[str, list[DuplicateCluster]] = {}
    for cluster in clusters:
        fingerprint = candidate_topology_fingerprint(
            cluster.candidate_ids,
            by_id,
            baseline=baseline,
            target=target,
        )
        actual_by_fingerprint.setdefault(fingerprint, []).append(cluster)
    used: set[str] = set()
    matched: list[dict[str, Any]] = []
    missing: list[str] = []
    for expected in expected_groups:
        expected_id = str(expected.get("gold_group_id") or expected.get("gold_case_id"))
        if expected_id in placeholder_ids:
            missing.append(expected_id)
            continue
        fingerprint = expected["topology_fingerprint"]
        candidates = [
            cluster
            for cluster in actual_by_fingerprint.get(fingerprint, [])
            if cluster.cluster_id not in used
            and len(cluster.candidate_ids) == int(expected["fragment_count"])
            and cluster.discovery_action == expected["expected"]
        ]
        if len(candidates) != 1:
            missing.append(str(expected.get("gold_group_id") or expected.get("gold_case_id")))
            continue
        cluster = candidates[0]
        used.add(cluster.cluster_id)
        matched.append(
            {
                "gold_id": expected.get("gold_group_id") or expected.get("gold_case_id"),
                "cluster_id": cluster.cluster_id,
                "action": cluster.discovery_action,
                "fragment_count": len(cluster.candidate_ids),
                "topology_fingerprint": fingerprint,
            }
        )
    boundary_expected = logical_gold.get("boundary_noise", [])
    actual_boundary_ids = set(
        comparison.validation_metadata.get("candidate_discovery", {}).get(
            "boundary_noise_candidate_ids", []
        )
    )
    boundary_fingerprints = {
        candidate_topology_fingerprint(
            (candidate_id,), by_id, baseline=baseline, target=target
        )
        for candidate_id in actual_boundary_ids
        if candidate_id in by_id
    }
    boundary_missing: list[str] = []
    for expected in boundary_expected:
        fingerprint = expected.get("topology_fingerprint") if isinstance(expected, dict) else None
        if not isinstance(fingerprint, str) or fingerprint not in boundary_fingerprints:
            boundary_missing.append(
                str(expected.get("gold_case_id") if isinstance(expected, dict) else expected)
            )
    boundary_extra = max(0, len(actual_boundary_ids) - len(boundary_expected))
    expected_action_counts = Counter(item["expected"] for item in expected_groups)
    actual_action_counts = Counter(cluster.discovery_action for cluster in clusters)
    expected_fragment_counts = {
        action: sum(
            int(item["fragment_count"])
            for item in expected_groups
            if item["expected"] == action
        )
        for action in expected_action_counts
    }
    actual_fragment_counts = Counter()
    for cluster in clusters:
        actual_fragment_counts[cluster.discovery_action] += len(cluster.candidate_ids)
    expected_false_positive_count = sum(
        int(item["fragment_count"])
        for item in logical_gold.get("equivalent_groups", [])
    ) + len(boundary_expected)
    actual_matched_false_positive_count = sum(
        item["fragment_count"]
        for item in matched
        if item["action"] == "EQUIVALENT_NO_CHANGE"
    ) + len(boundary_expected) - len(boundary_missing)
    matched_all = (
        not missing
        and not boundary_missing
        and boundary_extra == 0
        and len(used) == len(clusters)
        and (
            false_positives is None
            or len(false_positives) == expected_false_positive_count
        )
    )
    return {
        "status": "PASSED" if matched_all else "FAILED",
        "failure_code": (
            None
            if matched_all
            else "GOLD_TOPOLOGY_SIGNATURE_PLACEHOLDER"
            if placeholder_ids
            else "CANDIDATE_GOLD_FALSE_POSITIVE_MISSING"
        ),
        "expected_group_count": len(expected_groups),
        "actual_group_count": len(clusters),
        "expected_group_action_counts": dict(expected_action_counts),
        "actual_group_action_counts": dict(actual_action_counts),
        "expected_fragment_action_counts": dict(expected_fragment_counts),
        "actual_fragment_action_counts": dict(actual_fragment_counts),
        "expected_boundary_noise_count": len(boundary_expected),
        "actual_boundary_noise_count": len(actual_boundary_ids),
        "expected_false_positive_count": expected_false_positive_count,
        "actual_matched_false_positive_count": actual_matched_false_positive_count,
        "false_positive_binding_count": (
            len(false_positives) if isinstance(false_positives, list) else None
        ),
        "extra_boundary_noise_count": boundary_extra,
        "matched_group_count": len(matched),
        "matched_false_positive_count": actual_matched_false_positive_count,
        "missing_gold_ids": missing,
        "missing_boundary_noise_ids": boundary_missing,
        "matched_groups": matched,
        "expected_replay": logical_gold.get("local_replay", {}),
    }


def replay_final_compare_gold(
    comparison: ComparisonResult,
    clusters: list[DuplicateCluster],
    gold_audit: dict[str, Any],
    *,
    page_coverage: dict[str, int] | None = None,
    documents: list[ParsedDocument] | None = None,
) -> dict[str, Any]:
    """Apply the deidentified gold decisions locally without model or DB calls."""

    if gold_audit.get("status") != "PASSED":
        return {
            "status": "BLOCKED",
            "failure_code": "CANDIDATE_GOLD_MISMATCH",
            "formal_risk_count": None,
            "review_required_count": None,
        }
    by_id = {
        str(diff.candidate_id): diff
        for diff in comparison.diff_items
        if diff.candidate_id
    }
    action_by_cluster = {
        item["cluster_id"]: item["action"]
        for item in gold_audit.get("matched_groups", [])
    }
    replay_expectations = gold_audit.get("expected_replay", {})
    expected_risk_count = int(replay_expectations.get("formal_risk_count", 57))
    expected_review_count = int(replay_expectations.get("review_required_count", 0))
    expected_date_conflicts = int(
        replay_expectations.get("date_passed_conflict_count", 0)
    )
    expected_rent_missing = int(
        replay_expectations.get("rent_payment_plan_missing_count", 0)
    )
    page_required = bool(replay_expectations.get("page_coverage_required", False))
    page_ok = (
        page_coverage is not None
        and page_coverage.get("required_evidence_count", 0)
        == page_coverage.get("covered_evidence_count", 0)
        if page_required
        else True
    )
    removed: set[str] = set(
        comparison.validation_metadata.get("candidate_discovery", {}).get(
            "boundary_noise_candidate_ids", []
        )
    )
    retained: list[DiffItem] = []
    removed_by_action = Counter()
    for cluster in clusters:
        action = action_by_cluster.get(cluster.cluster_id)
        if action == "EQUIVALENT_NO_CHANGE":
            removed.update(cluster.candidate_ids)
            removed_by_action[action] += len(cluster.candidate_ids)
        elif action == "SAME_LOGICAL_CHANGE":
            representative = min(
                (by_id[candidate_id] for candidate_id in cluster.candidate_ids),
                key=lambda diff: (_DIFF_PRIORITY.get(diff.diff_type, 99), _sort_key(diff)),
            )
            merged = _merge_group_diffs(
                [by_id[candidate_id] for candidate_id in cluster.candidate_ids]
            )
            retained.append(_mark_confirmed(merged, "GOLD_SAME_LOGICAL_CHANGE"))
            removed.update(
                candidate_id
                for candidate_id in cluster.candidate_ids
                if candidate_id != representative.candidate_id
            )
            removed_by_action[action] += len(cluster.candidate_ids) - 1
    retained.extend(
        diff
        for diff in comparison.diff_items
        if diff.candidate_id not in removed
        and not any(diff.candidate_id in cluster.candidate_ids for cluster in clusters)
    )
    review_required = sum(item.validation_status == "REVIEW_REQUIRED" for item in retained)
    if documents is not None:
        from app.results.passed_checks import build_comparison_passed_checks

        replay_passed_checks = build_comparison_passed_checks(
            documents,
            retained,
            comparison.diagnostics,
            check_prefix="gold_replay",
            module_code="VERSION_CHANGE",
            content_title="合同内容未发生变化",
            numeric_sensitive=True,
            pending_differences=retained,
        )
        date_risk_count = sum(
            item.get("check_id", "").endswith("_date")
            for item in replay_passed_checks
        )
    else:
        date_risk_count = int(
            comparison.validation_stats.get("date_passed_check_count", 0)
        )
    return {
        "status": (
            "PASSED"
            if len(retained) == expected_risk_count
            and review_required == expected_review_count
            and date_risk_count == expected_date_conflicts
            and sum(
                diff.diff_type == "CONTENT_BLOCK_MISSING" for diff in retained
            )
            == expected_rent_missing
            and page_ok
            else "FAILED"
        ),
        "failure_code": (
            None
            if len(retained) == expected_risk_count
            and review_required == expected_review_count
            and date_risk_count == expected_date_conflicts
            and sum(
                diff.diff_type == "CONTENT_BLOCK_MISSING" for diff in retained
            )
            == expected_rent_missing
            and page_ok
            else "GOLD_REPLAY_MISMATCH"
        ),
        "formal_risk_count": len(retained),
        "review_required_count": review_required,
        "date_passed_conflict_count": date_risk_count,
        "rent_payment_plan_missing_count": sum(
            diff.diff_type == "CONTENT_BLOCK_MISSING" for diff in retained
        ),
        "expected_rent_payment_plan_missing_count": expected_rent_missing,
        "removed_candidate_count": len(removed),
        "removed_by_action": dict(removed_by_action),
        "page_coverage": page_coverage,
        "ocr_calls": 0,
        "llm_calls": 0,
        "database_writes": 0,
    }


def select_canary_clusters(
    clusters: list[DuplicateCluster],
    *,
    required_categories: tuple[str, ...] = (
        "PARAGRAPH_MERGE",
        "TABLE_MERGE",
        "FORMULA_EQUIVALENCE",
        "TABLE_FIELD_EQUIVALENCE",
    ),
) -> tuple[list[DuplicateCluster], str | None]:
    """Select one real cluster per Canary category in a stable order."""

    selected: list[DuplicateCluster] = []
    used: set[str] = set()
    for category in required_categories:
        matches = [
            cluster
            for cluster in clusters
            if cluster.cluster_id not in used and cluster.canary_category == category
        ]
        if not matches:
            return [], "CANDIDATE_CANARY_CATEGORY_MISSING"
        selected.append(matches[0])
        used.add(matches[0].cluster_id)
    return selected, None
