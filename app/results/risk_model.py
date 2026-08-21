from __future__ import annotations

from typing import Any

from app.comparison.engine import is_ocr_review_only_diff
from app.comparison.models import DiffItem
from app.documents.models import ProcessingWarning

DELETION_CHANGE_TYPES = {"DELETED", "TABLE_ROW_DELETED"}


def _diff_evidence(diff: DiffItem) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for side_name, side in (("BASELINE", diff.baseline), ("TARGET", diff.target)):
        if side is None:
            continue
        evidence.append(
            {
                "side": side_name,
                "file_id": side.file_id,
                "text": side.text,
                "locations": [
                    location.model_dump(mode="json", exclude_none=True)
                    for location in (side.locations or [side.location])
                ],
            }
        )
    return evidence


def build_risk_items(
    differences: list[DiffItem],
    *,
    module_code: str,
    failed_rules: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    for diff in differences:
        if is_ocr_review_only_diff(diff):
            continue
        risk_type = (
            "DELETION_OR_MISSING"
            if diff.diff_type in DELETION_CHANGE_TYPES
            else "ADDITION_OR_CHANGE"
        )
        risks.append(
            {
                "risk_id": f"risk_{diff.diff_id}",
                "module_code": module_code,
                "risk_type": risk_type,
                "change_type": diff.diff_type,
                "title": diff.title,
                "description": "检测到未经允许的确定性内容差异，请结合来源证据处理。",
                "source_evidence": _diff_evidence(diff),
                "related_diff_ids": [diff.diff_id],
                "related_rule_ids": [],
                "requires_manual_action": True,
            }
        )
    for rule in failed_rules or []:
        risks.append(
            {
                "risk_id": f"risk_{rule['rule_id'].replace('.', '_')}",
                "module_code": module_code,
                "risk_type": (
                    "DELETION_OR_MISSING"
                    if "empty" in rule["rule_id"] or "unresolved" in rule["rule_id"]
                    else "ADDITION_OR_CHANGE"
                ),
                "change_type": "RULE_FAILED",
                "title": rule["rule_name"],
                "description": rule["message"],
                "source_evidence": (
                    [{"location": rule["location"]}] if rule.get("location") else []
                ),
                "related_diff_ids": [],
                "related_rule_ids": [rule["rule_id"]],
                "requires_manual_action": True,
            }
        )
    return risks


def build_review_items(
    differences: list[DiffItem],
    warnings: list[ProcessingWarning],
    *,
    module_code: str,
) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    for diff in differences:
        if not is_ocr_review_only_diff(diff):
            continue
        reviews.append(
            {
                "review_id": f"review_{diff.diff_id}",
                "module_code": module_code,
                "reason_code": diff.review_reason,
                "title": "OCR 差异需要人工确认",
                "description": "当前证据不足以确认是否构成合同内容变化。",
                "source_evidence": _diff_evidence(diff),
                "related_diff_ids": [diff.diff_id],
                "requires_manual_action": True,
            }
        )
    for index, warning in enumerate(warnings, start=1):
        if not warning.requires_manual_review:
            continue
        reviews.append(
            {
                "review_id": f"review_{warning.code.lower()}_{index:04d}",
                "module_code": module_code,
                "reason_code": warning.code,
                "title": warning.message,
                "description": "解析、OCR 或对齐可靠性需要人工确认。",
                "source_evidence": [
                    {
                        key: value
                        for key, value in {
                            "file_id": warning.file_id,
                            "page": warning.page,
                            "confidence": warning.confidence,
                        }.items()
                        if value is not None
                    }
                ],
                "related_diff_ids": [],
                "requires_manual_action": True,
            }
        )
    return reviews


def build_statistics(
    risk_items: list[dict[str, Any]],
    review_items: list[dict[str, Any]],
    passed_checks: list[dict[str, Any]],
) -> dict[str, int | bool]:
    return {
        "risk_count": len(risk_items),
        "deletion_or_missing_count": sum(
            item["risk_type"] == "DELETION_OR_MISSING" for item in risk_items
        ),
        "addition_or_change_count": sum(
            item["risk_type"] == "ADDITION_OR_CHANGE" for item in risk_items
        ),
        "review_count": len(review_items),
        "passed_check_count": len(passed_checks),
        "legacy_statistics": False,
    }
