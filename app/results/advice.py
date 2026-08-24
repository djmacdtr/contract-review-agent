from __future__ import annotations

from typing import Any

from app.adapters.llm.schemas import AdviceResponse


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
        item.get("file_id"): item.get("file_name", "相关文件")
        for item in result.get("files", [])
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


def advice_payload(result: dict[str, Any]) -> dict[str, Any]:
    related_ids = {
        diff_id
        for risk in result.get("risk_items", [])
        for diff_id in risk.get("related_diff_ids", [])
    }
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
            for item in result.get("risk_items", [])
        ],
        "diff_items": [
            {
                "diff_id": item.get("diff_id"),
                "title": item.get("title"),
                "baseline": _advice_side(item.get("baseline")),
                "target": _advice_side(item.get("target")),
                "segments": item.get("segments", []),
            }
            for item in result.get("diff_items", [])
            if item.get("diff_id") in related_ids
        ],
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
    current_ids = {item["risk_id"] for item in result.get("risk_items", [])}
    technical_ids = {
        *current_ids,
        *(item.get("file_id") for item in result.get("files", [])),
        *(item.get("diff_id") for item in result.get("diff_items", [])),
    }
    technical_ids.discard(None)
    seen: set[str] = set()
    advice_texts: set[str] = set()
    for item in response.risk_advices:
        if item.risk_id not in current_ids or item.risk_id in seen:
            raise ValueError("advice risk_id does not belong to current task or is duplicated")
        normalized_advice = " ".join(item.analysis_advice.split())
        if normalized_advice in advice_texts:
            raise ValueError("risk advice must be specific and not duplicated")
        if any(str(identifier) in item.analysis_advice for identifier in technical_ids):
            raise ValueError("risk advice exposes an internal identifier")
        seen.add(item.risk_id)
        advice_texts.add(normalized_advice)
    by_id = {item.risk_id: item.analysis_advice for item in response.risk_advices}
    for risk in result.get("risk_items", []):
        if risk["risk_id"] in by_id:
            risk["analysis_advice"] = by_id[risk["risk_id"]]
    result["advice"] = response.model_dump(mode="json")
    ensure_fallback_risk_advices(result)
