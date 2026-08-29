from __future__ import annotations

from app.adapters.llm.schemas import AdviceResponse
from scripts.advice_quality_diagnostic import (
    _batch_result,
    _canary_status,
    _extract_dynamic_anchors,
    _validate_response_quality,
    _validate_specificity,
)


def _result_with_risks(count: int = 17) -> dict:
    risks = []
    diffs = []
    for index in range(1, count + 1):
        risk_id = f"risk_{index}"
        diff_id = f"diff_{index}"
        description = (
            "这是一个非常具体的长差异描述，用于确保该正式八项批次的 payload 更大。"
            if 9 <= index <= 16
            else "具体差异。"
        )
        risks.append(
            {
                "risk_id": risk_id,
                "risk_type": "MODIFICATION",
                "title": f"差异项{index}",
                "description": description,
                "related_diff_ids": [diff_id],
                "source_evidence": [],
            }
        )
        diffs.append(
            {
                "diff_id": diff_id,
                "diff_type": "TEXT_MODIFIED",
                "title": f"差异项{index}",
                "baseline": {"file_id": "baseline", "text": "原文"},
                "target": {"file_id": "target", "text": "变更"},
                "segments": [],
            }
        )
    return {"files": [], "risk_items": risks, "diff_items": diffs, "fact_matrix": []}


def test_canary_uses_a_production_sized_eight_risk_batch() -> None:
    selected, risk_count = _batch_result(_result_with_risks())

    assert risk_count == 8
    assert [risk["risk_id"] for risk in selected["risk_items"]] == [
        f"risk_{index}" for index in range(9, 17)
    ]
    assert {diff["diff_id"] for diff in selected["diff_items"]} == {
        f"diff_{index}" for index in range(9, 17)
    }


def test_canary_specificity_check_is_safe_and_requires_risk_context() -> None:
    result = _result_with_risks(1)
    result["diff_items"][0]["target"]["text"] = "租赁期限24个月"
    response = AdviceResponse.model_validate(
        {
            "overall_advice": "请核对差异项。",
            "priority_actions": [],
            "manual_review_focus": [],
            "limitations": [],
            "evidence_refs": [],
            "risk_advices": [
                {
                    "risk_id": "risk_1",
                    "analysis_advice": "请核对租赁期限24个月的审批依据。",
                }
            ],
        }
    )

    assert _validate_specificity(result, response) == 1


def test_canary_reports_item_quality_categories_without_response_text() -> None:
    result = _result_with_risks(4)
    response = AdviceResponse.model_validate(
        {
            "overall_advice": "请核对差异项。",
            "priority_actions": [],
            "manual_review_focus": [],
            "limitations": [],
            "evidence_refs": [],
            "risk_advices": [
                {
                    "risk_id": "risk_1",
                    "analysis_advice": "请核对差异项1。\n确认审批依据。",
                },
                {
                    "risk_id": "risk_2",
                    "analysis_advice": "请核对差异项1；确认审批依据。",
                },
                {
                    "risk_id": "risk_3",
                    "analysis_advice": "请检查 risk_1 对应位置。",
                },
                {
                    "risk_id": "risk_4",
                    "analysis_advice": "请检查 confidence 对应的审批依据。",
                },
            ],
        }
    )

    (
        normalized,
        accepted_count,
        counts,
        normalized_count,
        not_specific_count,
    ) = _validate_response_quality(
        result, response, require_dynamic_anchor=False
    )

    assert accepted_count == 1
    assert counts == {
        "MULTI_SENTENCE": 1,
        "DUPLICATED": 1,
        "INTERNAL_ID": 1,
        "TECHNICAL_TERM": 1,
    }
    assert normalized_count == 1
    assert not_specific_count == 0
    assert [item.risk_id for item in normalized.risk_advices] == ["risk_1"]


def _dynamic_anchor_result() -> dict:
    return {
        "files": [
            {
                "file_id": "baseline",
                "file_name": "融资租赁合同（回租）模版.docx",
            },
            {"file_id": "target", "file_name": "融资租赁合同（回租）.docx"},
        ],
        "risk_items": [
            {
                "risk_id": "risk_1",
                "title": "文字内容发生变化",
                "related_diff_ids": ["diff_1"],
            }
        ],
        "diff_items": [
            {
                "diff_id": "diff_1",
                "diff_type": "MODIFIED",
                "title": "目标文件新增内容",
                "baseline": {
                    "file_id": "baseline",
                    "text": "承租人名称为甲方，租赁期限为24个月，合同编号ABC-001",
                },
                "target": {
                    "file_id": "target",
                    "text": "承租人名称为乙方，租赁期限为36个月，合同编号ABC-002",
                },
                "segments": [
                    {"operation": "DELETE", "text": "甲方24个月ABC-001"},
                    {"operation": "INSERT", "text": "乙方36个月ABC-002"},
                ],
            }
        ],
    }


def _advice_response_for(advice: str) -> AdviceResponse:
    return AdviceResponse.model_validate(
        {
            "overall_advice": "请核对差异项。",
            "priority_actions": [],
            "manual_review_focus": [],
            "limitations": [],
            "evidence_refs": [],
            "risk_advices": [{"risk_id": "risk_1", "analysis_advice": advice}],
        }
    )


def test_canary_specificity_uses_dynamic_business_anchors_not_titles() -> None:
    result = _dynamic_anchor_result()
    response = _advice_response_for(
        "请核对承租人名称由甲方变为乙方，并确认36个月期限及编号ABC-002的依据。"
    )

    assert _validate_specificity(result, response) == 1
    anchors = dict(_extract_dynamic_anchors(result, result["risk_items"][0]))
    assert "承租人名称" in anchors
    assert "36个月" in anchors
    assert "abc-002" in anchors
    assert "文字内容发生变化" not in anchors


def test_canary_specificity_uses_missing_summary_and_file_name_anchors() -> None:
    result = _dynamic_anchor_result()
    result["diff_items"][0] = {
        "diff_id": "diff_1",
        "diff_type": "CONTENT_BLOCK_MISSING",
        "title": "目标文件新增内容",
        "baseline": {"file_id": "baseline", "text": ""},
        "target": {"file_id": "target", "text": ""},
        "segments": [],
        "missing_detail": {
            "content_summary": "承租人名称和租金支付日",
        },
    }

    assert _validate_specificity(
        result,
        _advice_response_for("请根据融资租赁合同（回租）模版.docx核对承租人名称和租金支付日。"),
    ) == 1


def test_canary_specificity_keeps_generic_blacklist_and_numeric_boundaries() -> None:
    result = _dynamic_anchor_result()
    result["diff_items"][0]["target"]["text"] = "金额1000万元"

    assert _validate_specificity(
        result,
        _advice_response_for("请核对相关内容，文件为融资租赁合同（回租）模版.docx。"),
    ) == 0
    assert _validate_specificity(
        result,
        _advice_response_for("请核对金额100万元的业务依据。"),
    ) == 0
    assert _validate_specificity(
        result,
        _advice_response_for("请核对金额1000万元的业务依据。"),
    ) == 1


def test_canary_specificity_rejects_generic_titles_without_dynamic_anchors() -> None:
    result = _dynamic_anchor_result()
    result["diff_items"][0]["baseline"]["text"] = "目标文件新增内容"
    result["diff_items"][0]["target"]["text"] = "目标文件新增内容"
    result["diff_items"][0]["segments"] = []

    assert _validate_specificity(
        result,
        _advice_response_for("请核对目标文件新增内容的相关差异。"),
    ) == 0


def test_canary_allows_partial_not_specific_as_recoverable() -> None:
    assert (
        _canary_status(
            accepted_count=7,
            risk_count=8,
            quality_counts={
                "MULTI_SENTENCE": 0,
                "DUPLICATED": 0,
                "INTERNAL_ID": 0,
                "TECHNICAL_TERM": 0,
            },
            not_specific_count=1,
        )
        == "RECOVERABLE"
    )
    assert (
        _canary_status(
            accepted_count=7,
            risk_count=8,
            quality_counts={
                "MULTI_SENTENCE": 0,
                "DUPLICATED": 1,
                "INTERNAL_ID": 0,
                "TECHNICAL_TERM": 0,
            },
            not_specific_count=0,
        )
        == "FAILED"
    )
