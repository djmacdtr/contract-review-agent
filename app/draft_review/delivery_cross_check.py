"""Small, deterministic cross-document candidate preparation for delivery mode.

This module deliberately does not know about the legacy fact-extraction chain.
It turns parser-owned text into bounded, traceable candidates.  A model may
classify those candidates later, but it cannot add evidence or identities.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from typing import Any, Iterable

from app.comparison.models import DiffItem, DiffSegment, DiffSide
from app.documents.models import DocumentBlock, DocumentLocation, ParsedDocument
from app.documents.normalization import normalize_text

MAX_CROSS_CANDIDATES = 60
MAX_REFERENCE_CANDIDATES = 3
MAX_CROSS_BATCH_SIZE = 20

_NUMBER = r"[-+]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?"
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("IDENTIFIER", re.compile(r"(?:编号|代码|证号|合同号|项目号)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{2,})")),
    ("DATE", re.compile(r"\d{4}[年./-]\d{1,2}[月./-]\d{1,2}日?")),
    ("PERCENTAGE", re.compile(rf"{_NUMBER}\s*(?:%|％|百分之)")),
    ("MONEY", re.compile(rf"{_NUMBER}\s*(?:人民币|元|万元|亿元|万|亿|CNY|RMB|￥|¥)")),
    ("DURATION", re.compile(rf"{_NUMBER}\s*(?:年|个月|月|周|星期|天|日)")),
    ("QUANTITY", re.compile(rf"{_NUMBER}\s*(?:台|件|个|套|期|BP|基点)")),
)
_BARE_NUMBER_CONTEXT = re.compile(
    r"(?:金额|数额|数量|期限|利率|费率|租金|租赁|融资|价款|比例|份数|期数|余额|本金|利息)",
    re.IGNORECASE,
)
_STOP_WORDS = frozenset(
    "合同 文件 项目 资料 相关 其中 本次 以及 甲方 乙方 目标 辅助 内容".split()
)


def normalize_candidate_value(value: str, value_type: str) -> str:
    """Normalize presentation noise while keeping business units intact."""

    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = re.sub(r"\s+", "", normalized).replace("，", ",")
    normalized = normalized.replace(",", "")
    if value_type == "PERCENTAGE":
        normalized = normalized.replace("％", "%").replace("百分之", "") + (
            "%" if "%" not in normalized else ""
        )
    return normalized.casefold()


def _location_dict(location: DocumentLocation) -> dict[str, Any]:
    return location.model_dump(mode="json", exclude_none=True)


def _context_terms(text: str) -> set[str]:
    terms: set[str] = set()
    for value in re.findall(r"[\u3400-\u9fff]{2,8}|[A-Za-z]{2,}|\d+", text):
        if value not in _STOP_WORDS:
            terms.add(value.casefold())
    return terms


def _iter_units(document: ParsedDocument) -> Iterable[tuple[DocumentBlock, str, DocumentLocation]]:
    for block in sorted(document.blocks, key=lambda item: item.order):
        if block.table is None:
            yield block, block.raw_text, block.location
            continue
        for row in block.table.rows:
            row_text = "\t".join(cell.raw_text for cell in row.cells)
            for cell in row.cells:
                yield block, cell.raw_text, cell.location.model_copy(
                    update={"table_index": block.table.table_index, "row": row.row}
                )
            if not row.cells and row_text:
                yield block, row_text, DocumentLocation(
                    table_index=block.table.table_index, row=row.row
                )


def _candidate_id(
    document: ParsedDocument, location: DocumentLocation, value: str, value_type: str
) -> str:
    canonical = [document.sha256, location.model_dump(mode="json", exclude_none=True), value, value_type]
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return f"cross_{digest}"


def _make_candidate(
    document: ParsedDocument,
    text: str,
    location: DocumentLocation,
    match: re.Match[str],
    value_type: str,
) -> dict[str, Any]:
    raw_value = match.group(1) if value_type == "IDENTIFIER" else match.group(0)
    context = text[max(0, match.start() - 96) : min(len(text), match.end() + 96)]
    return {
        "candidate_id": _candidate_id(document, location, raw_value, value_type),
        "file_id": document.file_id,
        "role": document.role,
        "value_type": value_type,
        "raw_value": raw_value,
        "normalized_value": normalize_candidate_value(raw_value, value_type),
        "context": context,
        "location": _location_dict(location),
        "_terms": sorted(_context_terms(text)),
    }


def _make_text_candidate(
    document: ParsedDocument,
    text: str,
    location: DocumentLocation,
    *,
    diff_ids: list[str] | None = None,
) -> dict[str, Any]:
    value = text.strip()
    bounded = value[:600]
    return {
        "candidate_id": _candidate_id(document, location, bounded, "TEXT"),
        "file_id": document.file_id,
        "role": document.role,
        "value_type": "TEXT",
        "raw_value": bounded,
        "normalized_value": normalize_candidate_value(bounded, "TEXT"),
        "context": bounded,
        "location": _location_dict(location),
        "diff_ids": diff_ids or [],
        "_terms": sorted(_context_terms(value)),
    }


def extract_document_text_candidates(document: ParsedDocument) -> list[dict[str, Any]]:
    """Return bounded paragraph/table text units for contextual matching."""

    candidates: list[dict[str, Any]] = []
    for block in sorted(document.blocks, key=lambda item: item.order):
        if block.table is not None:
            continue
        if block.raw_text.strip() and len(block.raw_text.strip()) <= 600:
            candidates.append(_make_text_candidate(document, block.raw_text, block.location))
    return candidates


def extract_document_candidates(document: ParsedDocument) -> list[dict[str, Any]]:
    """Extract bounded numeric/business tokens with parser-owned locations."""

    candidates: dict[tuple[str, str, str], dict[str, Any]] = {}
    for _block, text, location in _iter_units(document):
        if not text.strip():
            continue
        typed_spans: list[tuple[int, int]] = []
        for value_type, pattern in _PATTERNS:
            for match in pattern.finditer(text):
                typed_spans.append(match.span())
                item = _make_candidate(document, text, location, match, value_type)
                key = (str(item["location"]), item["normalized_value"], value_type)
                candidates.setdefault(key, item)
        for match in re.finditer(_NUMBER, text):
            if any(
                start <= match.start() < end or start < match.end() <= end
                for start, end in typed_spans
            ):
                continue
            context = text[max(0, match.start() - 24) : min(len(text), match.end() + 24)]
            if not _BARE_NUMBER_CONTEXT.search(context):
                continue
            item = _make_candidate(document, text, location, match, "NUMBER")
            key = (str(item["location"]), item["normalized_value"], "NUMBER")
            candidates.setdefault(key, item)
    return list(candidates.values())


def _diff_candidates(target: ParsedDocument, template_review: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    diffs = list(getattr(template_review, "diff_items", []))
    diagnostics = getattr(template_review, "diagnostics", None)
    diffs.extend(
        item.diff
        for item in getattr(diagnostics, "filtered_diff_items", [])
        if getattr(item, "diff", None) is not None
    )
    for diff in diffs:
        side = getattr(diff, "target", None)
        if side is None:
            continue
        text = side.text or ""
        if text.strip():
            result.append(
                _make_text_candidate(
                    target,
                    text,
                    side.location,
                    diff_ids=[diff.diff_id],
                )
            )
        for value_type, pattern in _PATTERNS:
            for match in pattern.finditer(text):
                item = _make_candidate(target, text, side.location, match, value_type)
                item["diff_ids"] = [diff.diff_id]
                result.append(item)
    return result


def _related_score(target: dict[str, Any], reference: dict[str, Any]) -> int:
    score = 0
    if target["normalized_value"] == reference["normalized_value"]:
        score += 100
    if target["value_type"] == reference["value_type"]:
        score += 20
    score += 5 * len(set(target.get("_terms", [])) & set(reference.get("_terms", [])))
    return score


def _public_candidate(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def build_reference_candidate_groups(
    target: ParsedDocument,
    references: list[ParsedDocument],
    template_review: Any,
    *,
    max_groups: int = MAX_CROSS_CANDIDATES,
) -> dict[str, Any]:
    """Create at most three relevant reference candidates per target value."""

    target_candidates = extract_document_candidates(target)
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {
        (item["file_id"], item["normalized_value"], item["value_type"]): item
        for item in target_candidates
    }
    for item in _diff_candidates(target, template_review):
        key = (item["file_id"], item["normalized_value"], item["value_type"])
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = item
        else:
            existing["diff_ids"] = list(
                dict.fromkeys([*existing.get("diff_ids", []), *item.get("diff_ids", [])])
            )
    target_candidates = list(by_key.values())

    reference_candidates = {
        document.file_id: [
            *extract_document_candidates(document),
            *extract_document_text_candidates(document),
        ]
        for document in references
        if document.blocks
    }
    groups: list[dict[str, Any]] = []
    omitted = 0
    pending_groups: list[tuple[int, dict[str, Any]]] = []
    for target_candidate in target_candidates:
        related_by_file: dict[str, list[dict[str, Any]]] = {}
        for file_id, candidates in reference_candidates.items():
            ranked = sorted(
                candidates,
                key=lambda item: (
                    -_related_score(target_candidate, item),
                    item["location"].get("paragraph_index", 10**9),
                    item["candidate_id"],
                ),
            )
            related = [item for item in ranked if _related_score(target_candidate, item) > 0][
                :MAX_REFERENCE_CANDIDATES
            ]
            if related:
                related_by_file[file_id] = related
        if not related_by_file:
            continue
        priority = 0 if target_candidate.get("diff_ids") else 1
        if any(
            item["normalized_value"] != target_candidate["normalized_value"]
            for items in related_by_file.values()
            for item in items
        ):
            priority = min(priority, 0)
        pending_groups.append(
            (
                priority,
                {
                    "candidate_id": target_candidate["candidate_id"],
                    "target": _public_candidate(target_candidate),
                    "references": {
                        file_id: [_public_candidate(item) for item in candidates]
                        for file_id, candidates in related_by_file.items()
                    },
                    "diff_ids": target_candidate.get("diff_ids", []),
                },
            )
        )
    pending_groups.sort(key=lambda item: (item[0], item[1]["candidate_id"]))
    groups = [item[1] for item in pending_groups[:max_groups]]
    omitted = max(0, len(pending_groups) - len(groups))
    warnings: list[dict[str, Any]] = []
    if omitted:
        warnings.append(
            {
                "code": "CROSS_CANDIDATE_LIMITED",
                "message": "跨资料候选数量较多，已优先保留数值冲突和模板变化候选。",
                "requires_manual_review": False,
                "details": {"omitted_candidate_count": omitted, "max_candidate_count": max_groups},
            }
        )
    return {
        "groups": groups,
        "warnings": warnings,
        "target_candidate_count": len(target_candidates),
        "reference_candidate_count": sum(len(items) for items in reference_candidates.values()),
        "group_count": len(groups),
        "omitted_count": omitted,
    }


def build_cross_validation_payload(groups: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "candidates": groups,
        "requirements": {
            "decisions": ["MATCH", "CONFLICT", "UNRELATED", "UNCERTAIN"],
            "each_candidate_exactly_once": True,
            "return_only_candidate_id_decision_reason": True,
            "do_not_create_evidence": True,
        },
    }


def candidate_to_fact_matrix_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "field_key": f"cross_{candidate['value_type'].lower()}",
        "display_name": candidate["value_type"],
        "value_type": candidate["value_type"],
            "raw_value": candidate["raw_value"],
        "normalized_hint": candidate["normalized_value"],
        "normalized_value": candidate["normalized_value"],
        "source_file_id": candidate["file_id"],
        "evidence_text": candidate["context"],
        "location": candidate["location"],
        "confidence": 0.9,
    }


def _diff_id(target_id: str, reference_id: str) -> str:
    digest = hashlib.sha256(f"{target_id}:{reference_id}".encode()).hexdigest()[:20]
    return f"cross_diff_{digest}"


def build_cross_conflict_diff(
    target: dict[str, Any], reference: dict[str, Any]
) -> DiffItem:
    target_text = str(target["raw_value"])
    reference_text = str(reference["raw_value"])
    operation = "NUMERIC_CHANGED" if target["value_type"] in {
        "MONEY", "PERCENTAGE", "DURATION", "QUANTITY", "NUMBER"
    } else "MODIFIED"
    return DiffItem(
        diff_id=_diff_id(target["candidate_id"], reference["candidate_id"]),
        diff_type=operation,
        title="辅助资料中的对应数值或内容与目标合同不一致",
        baseline=DiffSide(
            file_id=reference["file_id"],
            location=DocumentLocation.model_validate(reference["location"]),
            text=reference_text,
        ),
        target=DiffSide(
            file_id=target["file_id"],
            location=DocumentLocation.model_validate(target["location"]),
            text=target_text,
        ),
        segments=[
            DiffSegment(operation="DELETE", text=reference_text),
            DiffSegment(operation="INSERT", text=target_text),
        ],
        confidence=0.9,
        certainty="CONFIRMED",
    )


def group_decision_to_results(
    groups: list[dict[str, Any]], decisions: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Materialize only model decisions that point to existing candidates."""

    fact_matrix: list[dict[str, Any]] = []
    conflict_diffs: list[DiffItem] = []
    passed: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for group in groups:
        decision = decisions.get(group["candidate_id"])
        if not decision:
            continue
        status = decision.get("decision")
        target = group["target"]
        refs = [item for values in group["references"].values() for item in values]
        references_by_file = defaultdict(list)
        for item in refs:
            references_by_file[item["file_id"]].append(item)
        if status == "CONFLICT":
            for reference in refs:
                if reference["normalized_value"] != target["normalized_value"]:
                    diff = build_cross_conflict_diff(target, reference)
                    conflict_diffs.append(diff)
                    break
        elif status == "MATCH":
            consistent = [
                reference
                for reference in refs
                if reference["normalized_value"] == target["normalized_value"]
            ]
            if consistent:
                fact_matrix.append(
                    {
                        "target_fact_id": group["candidate_id"],
                        "field_key": f"cross_{target['value_type'].lower()}",
                        "display_name": target["value_type"],
                        "status": "CONSISTENT",
                        "target_candidate": candidate_to_fact_matrix_candidate(target),
                        "candidates": [candidate_to_fact_matrix_candidate(item) for item in consistent],
                        "reference_results": [
                            {
                                "source_file_id": file_id,
                                "status": "CONSISTENT",
                                "candidate": candidate_to_fact_matrix_candidate(items[0]),
                                "reason_code": "MATCH",
                                "requires_manual_review": False,
                            }
                            for file_id, items in references_by_file.items()
                            if any(item["normalized_value"] == target["normalized_value"] for item in items)
                        ],
                        "missing_source_file_ids": [],
                    }
                )
                passed.append(
                    {
                        "check_id": f"check_cross_{group['candidate_id']}",
                        "module_code": "FACT_CONSISTENCY",
                        "title": f"{target['value_type']}跨资料一致",
                        "description": "目标合同与辅助资料中的对应内容一致。",
                    }
                )
        elif status == "UNCERTAIN":
            warnings.append(
                {
                    "code": "CROSS_CANDIDATE_UNCERTAIN",
                    "message": "部分辅助资料对应关系不明确，未生成风险或通过项。",
                    "requires_manual_review": False,
                }
            )
    return {
        "fact_matrix": fact_matrix,
        "diff_items": conflict_diffs,
        "passed_checks": passed,
        "warnings": warnings,
    }
