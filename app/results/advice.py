from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.adapters.llm.schemas import AdviceResponse

ADVICE_QUALITY_CODES = (
    "MULTI_SENTENCE",
    "DUPLICATED",
    "INTERNAL_ID",
    "TECHNICAL_TERM",
)
_ADVICE_FORBIDDEN_TERMS = {
    "file_id",
    "source_file_id",
    "fact_id",
    "field_key",
    "concept_id",
    "validation_id",
    "value_type",
    "source_evidence",
    "related_diff_ids",
    "confidence",
    "independent_review",
    "same_model_diagnostic",
    "ast",
}
_SENTENCE_MARKS = "。！？!?"
GENERIC_ADVICE_PHRASES = (
    "请核对相关内容",
    "请结合实际内容核对",
    "请核对相关差异",
    "请关注相关问题",
)
_GENERIC_ANCHOR_WORDS = frozenset(
    {
        "合同",
        "内容",
        "差异",
        "相关",
        "核对",
        "确认",
        "文件",
        "资料",
        "目标",
        "模板",
        "当前",
        "基准",
        "文字",
        "表格",
        "单元格",
        "位置",
        "发生",
        "变化",
        "新增",
        "删除",
        "修改",
        "变更",
        "固定",
        "项目",
        "文档",
        "摘要",
        "金额",
        "比例",
        "日期",
        "期限",
        "编号",
        "万元",
        "万",
        "元",
        "个月",
        "月",
        "年",
        "天",
        "期",
        "次",
        "号",
        "日",
        "请",
        "将",
        "由",
        "变为",
        "原为",
        "改为",
        "涉及",
        "的",
        "为",
        "是",
        "和",
        "与",
        "及",
        "或",
        "中",
        "上",
        "下",
        "未",
        "有",
        "无",
        "存在",
        "进行",
        "可能",
        "需要",
        "应当",
        "应",
        "是否",
        "其中",
        "该",
        "本",
        "项",
        "部分",
        "连续",
    }
)
_DYNAMIC_VALUE_PATTERN = re.compile(
    r"(?:"
    r"\d{4}年\d{1,2}月(?:\d{1,2}日)?"
    r"|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d+(?:\.\d+)?%"
    r"|\d+(?:,\d{3})*(?:\.\d+)?(?:万元|万|元|个月|月|年|天|期|次|号|日)"
    r"|[A-Za-z][A-Za-z0-9]*(?:[-_/][A-Za-z0-9]+)+"
    r"|\d{2,}"
    r")"
)


@dataclass(frozen=True)
class AdviceItemValidation:
    """Safe result of validating one model-generated advice item."""

    accepted: bool
    normalized_advice: str
    reason_code: str | None = None
    normalized_multi_sentence: bool = False


def empty_advice_quality_counts() -> dict[str, int]:
    return {code: 0 for code in ADVICE_QUALITY_CODES}


def _normalize_match_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def extract_dynamic_advice_anchors(
    result: dict[str, Any], risk: dict[str, Any]
) -> list[tuple[str, bool]]:
    """Extract safe business anchors from the program-owned risk evidence."""

    file_names = {
        str(file.get("file_id")): str(file.get("file_name") or "")
        for file in result.get("files", [])
        if file.get("file_id") and file.get("file_name")
    }
    diff_by_id = {diff.get("diff_id"): diff for diff in result.get("diff_items", [])}
    source_values: list[str] = []
    related_file_names: set[str] = set()
    for diff_id in risk.get("related_diff_ids", []):
        diff = diff_by_id.get(diff_id) or {}
        for side_name in ("baseline", "target"):
            side = diff.get(side_name) or {}
            if side.get("text"):
                source_values.append(str(side["text"]))
            if side.get("file_id") in file_names:
                related_file_names.add(file_names[side["file_id"]])
        for segment in diff.get("segments", []):
            if segment.get("operation") in {"DELETE", "INSERT"} and segment.get("text"):
                source_values.append(str(segment["text"]))
        detail = diff.get("missing_detail") or {}
        if detail.get("content_summary"):
            source_values.append(str(detail["content_summary"]))

    for evidence in risk.get("source_evidence", []):
        file_id = evidence.get("file_id")
        if file_id in file_names:
            related_file_names.add(file_names[file_id])

    anchors: dict[str, bool] = {}

    def add_anchor(value: str, *, strict: bool) -> None:
        normalized = _normalize_match_text(value)
        if normalized and len(normalized) >= 2:
            anchors[normalized] = anchors.get(normalized, False) or strict

    for value in source_values:
        for match in _DYNAMIC_VALUE_PATTERN.finditer(value):
            add_anchor(match.group(), strict=True)
        cleaned = value
        for word in sorted(_GENERIC_ANCHOR_WORDS, key=len, reverse=True):
            cleaned = cleaned.replace(word, " ")
        for word in re.findall(r"[\u4e00-\u9fff]{2,}", cleaned):
            add_anchor(word, strict=False)

    for file_name in related_file_names:
        add_anchor(file_name, strict=False)
        stem = re.sub(r"\.[A-Za-z0-9]+$", "", file_name)
        add_anchor(stem, strict=False)

    return list(anchors.items())


def advice_has_dynamic_anchor(result: dict[str, Any], risk: dict[str, Any], advice: str) -> bool:
    normalized_advice = " ".join(advice.split())
    lowered = normalized_advice.casefold()
    if any(phrase in lowered for phrase in GENERIC_ADVICE_PHRASES):
        return False
    return any(
        _advice_hits_anchor(normalized_advice, anchor, strict=strict)
        for anchor, strict in extract_dynamic_advice_anchors(result, risk)
    )


def _advice_hits_anchor(advice: str, anchor: str, *, strict: bool) -> bool:
    normalized_advice = _normalize_match_text(advice)
    if strict:
        pattern = rf"(?<![A-Za-z0-9]){re.escape(anchor)}(?![A-Za-z0-9])"
        return re.search(pattern, normalized_advice) is not None
    return anchor in normalized_advice


def _has_multiple_sentences(value: str, *, original: str | None = None) -> bool:
    return (
        bool(original and any(mark in original for mark in "\r\n"))
        or sum(value.count(mark) for mark in _SENTENCE_MARKS) > 1
    )


def _normalize_single_sentence(value: str) -> str:
    """Collapse formatting and sentence boundaries without changing facts."""

    collapsed = re.sub(r"[\r\n]+", "；", value)
    collapsed = " ".join(collapsed.split()).strip()
    matches = list(re.finditer(r"[。！？!?]+", collapsed))
    if len(matches) <= 1:
        return collapsed

    pieces: list[str] = []
    cursor = 0
    for match in matches[:-1]:
        pieces.extend((collapsed[cursor : match.start()], "；"))
        cursor = match.end()
    last = matches[-1]
    pieces.extend((collapsed[cursor : last.start()], last.group()[-1]))
    return re.sub(r"；{2,}", "；", "".join(pieces)).strip()


def _technical_identifiers(result: dict[str, Any]) -> set[str]:
    current_ids = {item.get("risk_id") for item in result.get("risk_items", [])}
    technical_ids = {
        *current_ids,
        *(item.get("file_id") for item in result.get("files", [])),
        *(item.get("diff_id") for item in result.get("diff_items", [])),
    }
    technical_ids.discard(None)
    return {str(identifier) for identifier in technical_ids}


def validate_advice_item(
    result: dict[str, Any],
    item: Any,
    *,
    seen_risk_ids: set[str] | None = None,
    seen_advice_texts: set[str] | None = None,
    normalize_multi_sentence: bool = True,
    require_dynamic_anchor: bool = False,
) -> AdviceItemValidation:
    """Validate one advice item using the production and Canary quality gates.

    Callers own the supplied ``seen_*`` sets: add the risk ID after this call,
    and add ``normalized_advice`` only when the outcome is accepted.
    """

    if seen_risk_ids is None:
        seen_risk_ids = set()
    if seen_advice_texts is None:
        seen_advice_texts = set()
    risk_id = str(getattr(item, "risk_id", ""))
    current_ids = {
        str(candidate.get("risk_id"))
        for candidate in result.get("risk_items", [])
        if candidate.get("risk_id") is not None
    }
    if risk_id not in current_ids:
        return AdviceItemValidation(False, "", "RISK_ID_INVALID")
    if risk_id in seen_risk_ids:
        return AdviceItemValidation(False, "", "DUPLICATED")

    original_advice = str(getattr(item, "analysis_advice", ""))
    normalized_advice = " ".join(original_advice.split())
    had_multiple_sentences = _has_multiple_sentences(
        normalized_advice,
        original=original_advice,
    )
    if had_multiple_sentences:
        if not normalize_multi_sentence:
            return AdviceItemValidation(False, normalized_advice, "MULTI_SENTENCE")
        normalized_advice = _normalize_single_sentence(original_advice)
        if _has_multiple_sentences(normalized_advice):
            return AdviceItemValidation(False, normalized_advice, "MULTI_SENTENCE")

    if normalized_advice in seen_advice_texts:
        return AdviceItemValidation(False, normalized_advice, "DUPLICATED")

    if any(identifier in normalized_advice for identifier in _technical_identifiers(result)):
        return AdviceItemValidation(False, normalized_advice, "INTERNAL_ID")

    lowered_advice = normalized_advice.casefold()
    if any(term in lowered_advice for term in _ADVICE_FORBIDDEN_TERMS):
        return AdviceItemValidation(False, normalized_advice, "TECHNICAL_TERM")

    if require_dynamic_anchor and not advice_has_dynamic_anchor(
        result,
        next(risk for risk in result.get("risk_items", []) if str(risk.get("risk_id")) == risk_id),
        normalized_advice,
    ):
        return AdviceItemValidation(False, normalized_advice, "NOT_SPECIFIC")

    return AdviceItemValidation(
        True,
        normalized_advice,
        "MULTI_SENTENCE" if had_multiple_sentences else None,
        normalized_multi_sentence=had_multiple_sentences,
    )


def _index(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return str(int(value) + 1)
    except (TypeError, ValueError):
        return None


def _location_text(file_name: str, location: dict[str, Any] | None) -> str:
    parts = [f"《{file_name}》"]
    location = location or {}
    if location.get("page") is not None:
        parts.append(f"第 {location['page']} 页")
    for key, suffix in (
        ("paragraph_index", "段"),
        ("table_index", "个表格"),
        ("row", "行"),
        ("column", "列"),
    ):
        value = _index(location.get(key))
        if value:
            parts.append(f"第 {value} {suffix}")
    return " · ".join(parts)


def _short(value: str, limit: int = 100) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else f"{value[:limit]}…"


def _risk_context(result: dict[str, Any], risk: dict[str, Any]) -> tuple[str, str, str]:
    file_names = {
        item.get("file_id"): item.get("file_name", "相关文件") for item in result.get("files", [])
    }
    diff_by_id = {item.get("diff_id"): item for item in result.get("diff_items", [])}
    locations: list[str] = []
    deleted: list[str] = []
    inserted: list[str] = []
    for diff_id in risk.get("related_diff_ids", []):
        diff = diff_by_id.get(diff_id) or {}
        for side_name in ("baseline", "target"):
            side = diff.get(side_name) or {}
            if side:
                file_name = file_names.get(side.get("file_id"), "相关文件")
                locations.append(_location_text(file_name, side.get("location")))
        for segment in diff.get("segments", []):
            if segment.get("operation") == "DELETE" and segment.get("text"):
                deleted.append(str(segment["text"]))
            elif segment.get("operation") == "INSERT" and segment.get("text"):
                inserted.append(str(segment["text"]))
        if not diff.get("segments"):
            if diff.get("baseline") and not diff.get("target"):
                deleted.append(str(diff["baseline"].get("text", "")))
            if diff.get("target") and not diff.get("baseline"):
                inserted.append(str(diff["target"].get("text", "")))
    for evidence in risk.get("source_evidence", []):
        if evidence.get("location"):
            evidence_location = evidence.get("location") or {}
            evidence_file_id = evidence.get("file_id") or evidence_location.get("file_id")
            file_name = file_names.get(evidence_file_id, "相关文件")
            locations.append(_location_text(file_name, evidence_location))
    return (
        "；".join(dict.fromkeys(locations)) or "相关文件对应位置",
        _short("".join(deleted)),
        _short("".join(inserted)),
    )


def fallback_analysis_advice(result: dict[str, Any], risk: dict[str, Any]) -> str:
    file_names = {
        item.get("file_id"): item.get("file_name", "相关文件") for item in result.get("files", [])
    }
    diff_by_id = {item.get("diff_id"): item for item in result.get("diff_items", [])}
    if risk.get("validation_status") == "REVIEW_REQUIRED":
        location, deleted, inserted = _risk_context(result, risk)
        detail = "；".join(item for item in (deleted, inserted) if item)
        detail_text = f"，涉及“{detail}”" if detail else ""
        return (
            f"该项差异需要人工复核，请核对双方原文及对应位置（{location}）"
            f"{detail_text}，确认是否构成实际版本变化。"
        )
    missing_diff = next(
        (
            diff_by_id.get(diff_id)
            for diff_id in risk.get("related_diff_ids", [])
            if (diff_by_id.get(diff_id) or {}).get("diff_type")
            in {"PAGE_MISSING", "CONTENT_BLOCK_MISSING"}
        ),
        None,
    )
    if missing_diff:
        baseline = missing_diff.get("baseline") or {}
        target = missing_diff.get("target") or {}
        detail = missing_diff.get("missing_detail") or {}
        baseline_name = file_names.get(baseline.get("file_id"), "基准文件")
        target_name = file_names.get(target.get("file_id"), "当前文件")
        summary = detail.get("content_summary") or "对应连续合同内容"
        boundary = detail.get("boundary")
        boundary_text = {
            "START": "文档开头",
            "MIDDLE": "文档中部",
            "END": "文档末尾",
        }.get(boundary, "对应位置")
        if missing_diff.get("diff_type") == "PAGE_MISSING":
            qualifier = (
                "确认缺少页面内容"
                if missing_diff.get("certainty") == "CONFIRMED"
                else "疑似缺少一页或连续大段内容"
            )
            return (
                f"《{target_name}》{boundary_text}{qualifier}，对应《{baseline_name}》中的"
                f"“{_short(str(summary), 80)}”。请核对原始扫描件、纸质合同及页码连续性，"
                "确认是否需要补齐后重新签署或归档。"
            )
        return (
            f"《{target_name}》{boundary_text}缺少《{baseline_name}》中的连续内容"
            f"“{_short(str(summary), 80)}”。请核对版本形成过程和原始文件，"
            "确认是否需要补回并同步检查相邻条款。"
        )
    location, deleted, inserted = _risk_context(result, risk)
    title = risk.get("title") or "该项差异"
    if risk.get("risk_type") == "DELETION_OR_MISSING":
        detail = f"缺少内容“{deleted}”" if deleted else "缺失或未填写内容"
        return (
            f"请在{location}核对{detail}，确认是否应按原文件或业务依据补回，"
            f"并同步检查“{title}”涉及的关联条款。"
        )
    if deleted and inserted:
        return (
            f"请在{location}逐项核对“{deleted}”变更为“{inserted}”的审批依据，"
            f"确认“{title}”是否符合本次业务约定。"
        )
    if inserted:
        return (
            f"请确认{location}新增内容“{inserted}”是否具有有效业务依据，"
            f"并核对其对“{title}”及关联条款的影响。"
        )
    return f"请结合{location}的实际内容核对“{title}”，确认差异来源、业务依据及需要同步修订的文件。"


def ensure_fallback_risk_advices(result: dict[str, Any]) -> None:
    for risk in result.get("risk_items", []):
        if not risk.get("analysis_advice"):
            risk["analysis_advice"] = fallback_analysis_advice(result, risk)


def advice_payload(result: dict[str, Any], risk_ids: set[str] | None = None) -> dict[str, Any]:
    selected_risk_items = [
        item
        for item in result.get("risk_items", [])
        if risk_ids is None or str(item.get("risk_id")) in risk_ids
    ]
    related_ids = {
        diff_id for risk in selected_risk_items for diff_id in risk.get("related_diff_ids", [])
    }
    evidence_keys = {
        (
            evidence.get("file_id") or (evidence.get("location") or {}).get("file_id"),
            str(evidence.get("location") or {}),
        )
        for risk in result.get("risk_items", [])
        for evidence in risk.get("source_evidence", [])
        if isinstance(evidence, dict)
    }

    def compact_fact(candidate: Any) -> dict[str, Any] | None:
        if not isinstance(candidate, dict):
            return None
        return {
            key: candidate.get(key)
            for key in (
                "source_file_id",
                "display_name",
                "value_type",
                "raw_value",
                "normalized_hint",
                "evidence_text",
                "location",
            )
            if candidate.get(key) is not None
        }

    related_facts: list[dict[str, Any]] = []
    for matrix in result.get("fact_matrix", []):
        target = matrix.get("target_candidate") or {}
        references = [
            relation
            for relation in matrix.get("reference_results", [])
            if isinstance(relation, dict) and relation.get("candidate")
        ]
        candidates = [target, *(relation["candidate"] for relation in references)]
        if not any(
            (
                candidate.get("source_file_id"),
                str(candidate.get("location") or {}),
            )
            in evidence_keys
            for candidate in candidates
        ):
            continue
        related_facts.append(
            {
                "status": matrix.get("status"),
                "target": compact_fact(target),
                "references": [
                    {
                        "status": relation.get("status"),
                        "fact": compact_fact(relation.get("candidate")),
                    }
                    for relation in references
                ],
            }
        )

    return {
        "files": [
            {key: item.get(key) for key in ("file_id", "file_name", "role")}
            for item in result.get("files", [])
        ],
        "risk_items": [
            {
                "risk_id": item.get("risk_id"),
                "risk_type": item.get("risk_type"),
                "title": item.get("title"),
                "description": item.get("description"),
                "related_diff_ids": item.get("related_diff_ids", []),
                "source_evidence": [
                    {
                        key: evidence.get(key)
                        for key in ("file_id", "text", "location")
                        if evidence.get(key) is not None
                    }
                    for evidence in item.get("source_evidence", [])
                ],
            }
            for item in selected_risk_items
        ],
        "diff_items": [
            {
                "diff_id": item.get("diff_id"),
                "diff_type": item.get("diff_type"),
                "title": item.get("title"),
                "certainty": item.get("certainty"),
                "missing_detail": item.get("missing_detail"),
                "baseline": _advice_side(item.get("baseline")),
                "target": _advice_side(item.get("target")),
                "segments": item.get("segments", []),
            }
            for item in result.get("diff_items", [])
            if item.get("diff_id") in related_ids
        ],
        "related_facts": related_facts,
    }


def _advice_side(side: Any) -> dict[str, Any] | None:
    if not isinstance(side, dict):
        return None
    return {
        key: side.get(key)
        for key in ("file_id", "location", "locations", "text")
        if side.get(key) is not None
    }


def merge_model_advice(result: dict[str, Any], response: AdviceResponse) -> None:
    seen: set[str] = set()
    advice_texts: set[str] = set()
    normalized_items = []
    for item in response.risk_advices:
        outcome = validate_advice_item(
            result,
            item,
            seen_risk_ids=seen,
            seen_advice_texts=advice_texts,
        )
        seen.add(item.risk_id)
        if not outcome.accepted:
            if outcome.reason_code == "RISK_ID_INVALID":
                raise ValueError("advice risk_id does not belong to current task")
            if outcome.reason_code == "DUPLICATED":
                raise ValueError("risk advice must be specific and not duplicated")
            if outcome.reason_code == "MULTI_SENTENCE":
                raise ValueError("risk advice must be one concise sentence")
            if outcome.reason_code == "INTERNAL_ID":
                raise ValueError("risk advice exposes an internal identifier")
            raise ValueError("risk advice exposes technical fields or terminology")
        advice_texts.add(outcome.normalized_advice)
        normalized_items.append(
            item.model_copy(update={"analysis_advice": outcome.normalized_advice})
        )
    by_id = {item.risk_id: item.analysis_advice for item in normalized_items}
    for risk in result.get("risk_items", []):
        if risk["risk_id"] in by_id:
            risk["analysis_advice"] = by_id[risk["risk_id"]]
    normalized_response = response.model_copy(update={"risk_advices": normalized_items})
    result["advice"] = normalized_response.model_dump(mode="json")
    ensure_fallback_risk_advices(result)
