import pytest

from app.adapters.llm.schemas import AdviceResponse
from app.results.advice import (
    advice_payload,
    ensure_fallback_risk_advices,
    merge_model_advice,
)


def result_fixture() -> dict:
    return {
        "files": [
            {"file_id": "fil_base", "file_name": "融资租赁合同.docx", "role": "BASELINE"},
            {"file_id": "fil_target", "file_name": "当前合同.docx", "role": "TARGET"},
        ],
        "risk_items": [
            {
                "risk_id": "risk_000001",
                "risk_type": "ADDITION_OR_CHANGE",
                "title": "租赁期限发生变化",
                "related_diff_ids": ["diff_000001"],
                "source_evidence": [],
            }
        ],
        "diff_items": [
            {
                "diff_id": "diff_000001",
                "baseline": {
                    "file_id": "fil_base",
                    "text": "租赁期限为24个月",
                    "location": {"page": 3, "paragraph_index": 11},
                },
                "target": {
                    "file_id": "fil_target",
                    "text": "租赁期限为36个月",
                    "location": {"page": 3, "paragraph_index": 11},
                },
                "segments": [
                    {"operation": "EQUAL", "text": "租赁期限为"},
                    {"operation": "DELETE", "text": "24"},
                    {"operation": "INSERT", "text": "36"},
                    {"operation": "EQUAL", "text": "个月"},
                ],
            }
        ],
        "passed_checks": [],
    }


def advice_response(*risk_advices: dict) -> AdviceResponse:
    return AdviceResponse.model_validate(
        {
            "overall_advice": "请处理已确认差异。",
            "priority_actions": [],
            "manual_review_focus": [],
            "limitations": [],
            "evidence_refs": [],
            "risk_advices": list(risk_advices),
        }
    )


def test_fallback_advice_uses_current_change_file_and_business_location() -> None:
    result = result_fixture()

    ensure_fallback_risk_advices(result)

    advice = result["risk_items"][0]["analysis_advice"]
    assert "融资租赁合同.docx" in advice
    assert "第 3 页" in advice
    assert "第 12 段" in advice
    assert "24" in advice
    assert "36" in advice
    payload = advice_payload(result)
    assert [item["diff_id"] for item in payload["diff_items"]] == ["diff_000001"]
    assert "confidence" not in payload["diff_items"][0]
    assert "passed_checks" not in payload


def test_model_advice_merges_only_current_unique_risk_ids() -> None:
    result = result_fixture()
    merge_model_advice(
        result,
        advice_response(
            {
                "risk_id": "risk_000001",
                "analysis_advice": "请核对租赁期限由24个月变为36个月的审批依据。",
            }
        ),
    )

    assert "24个月变为36个月" in result["risk_items"][0]["analysis_advice"]


@pytest.mark.parametrize(
    "risk_advices",
    [
        [
            {
                "risk_id": "risk_unknown",
                "analysis_advice": "未知风险建议。",
            }
        ],
        [
            {"risk_id": "risk_000001", "analysis_advice": "第一条。"},
            {"risk_id": "risk_000001", "analysis_advice": "重复条目。"},
        ],
        [
            {
                "risk_id": "risk_000001",
                "analysis_advice": "请检查 fil_target 对应位置。",
            }
        ],
    ],
)
def test_invalid_or_technical_model_risk_advice_is_rejected(
    risk_advices: list[dict],
) -> None:
    with pytest.raises(ValueError, match="current task|duplicated|internal identifier"):
        merge_model_advice(result_fixture(), advice_response(*risk_advices))
