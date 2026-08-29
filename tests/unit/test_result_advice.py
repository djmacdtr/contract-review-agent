import pytest

from app.adapters.llm.schemas import AdviceResponse
from app.results.advice import (
    advice_payload,
    ensure_fallback_risk_advices,
    merge_model_advice,
    validate_advice_item,
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


def test_advice_payload_includes_every_formal_risk() -> None:
    result = result_fixture()
    result["risk_items"].append(
        {
            "risk_id": "risk_internal_rule",
            "risk_type": "ADDITION_OR_CHANGE",
            "title": "内部规则失败",
            "related_diff_ids": [],
            "source_evidence": [],
        }
    )

    assert [item["risk_id"] for item in advice_payload(result)["risk_items"]] == [
        "risk_000001",
        "risk_internal_rule",
    ]


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


def test_multi_sentence_advice_is_deterministically_normalized() -> None:
    result = result_fixture()
    response = advice_response(
        {
            "risk_id": "risk_000001",
            "analysis_advice": "请核对租赁期限。\n确认审批依据。",
        }
    )

    outcome = validate_advice_item(
        result,
        response.risk_advices[0],
        seen_risk_ids=set(),
        seen_advice_texts=set(),
    )

    assert outcome.accepted is True
    assert outcome.reason_code == "MULTI_SENTENCE"
    assert outcome.normalized_multi_sentence is True
    assert outcome.normalized_advice == "请核对租赁期限；确认审批依据。"


def test_dynamic_specificity_is_shared_and_opt_in() -> None:
    result = result_fixture()
    generic = advice_response(
        {
            "risk_id": "risk_000001",
            "analysis_advice": "请核对相关内容。",
        }
    )

    outcome = validate_advice_item(
        result,
        generic.risk_advices[0],
        seen_risk_ids=set(),
        seen_advice_texts=set(),
        require_dynamic_anchor=True,
    )

    assert outcome.accepted is False
    assert outcome.reason_code == "NOT_SPECIFIC"

    merge_model_advice(result, generic)
    assert result["risk_items"][0]["analysis_advice"] == "请核对相关内容。"


@pytest.mark.parametrize(
    ("analysis_advice", "expected_code"),
    [
        ("请核对已有建议。", "DUPLICATED"),
        ("请检查 fil_target 对应位置。", "INTERNAL_ID"),
        ("请检查 confidence 对应的审批依据。", "TECHNICAL_TERM"),
    ],
)
def test_advice_quality_categories_are_safe_and_item_scoped(
    analysis_advice: str,
    expected_code: str,
) -> None:
    result = result_fixture()
    response = advice_response(
        {
            "risk_id": "risk_000001",
            "analysis_advice": analysis_advice,
        }
    )
    seen_texts = {"请核对已有建议。"} if expected_code == "DUPLICATED" else set()

    outcome = validate_advice_item(
        result,
        response.risk_advices[0],
        seen_risk_ids=set(),
        seen_advice_texts=seen_texts,
    )

    assert outcome.accepted is False
    assert outcome.reason_code == expected_code
    assert outcome.normalized_advice


def test_merge_model_advice_persists_normalized_text() -> None:
    result = result_fixture()

    merge_model_advice(
        result,
        advice_response(
            {
                "risk_id": "risk_000001",
                "analysis_advice": "请核对租赁期限。\n确认审批依据。",
            }
        ),
    )

    assert result["risk_items"][0]["analysis_advice"] == "请核对租赁期限；确认审批依据。"
    assert result["advice"]["risk_advices"][0]["analysis_advice"] == (
        "请核对租赁期限；确认审批依据。"
    )


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


def test_missing_page_advice_and_payload_use_structured_context() -> None:
    result = result_fixture()
    result["risk_items"][0].update(
        {
            "risk_type": "DELETION_OR_MISSING",
            "title": "疑似整页或连续大段内容缺失",
        }
    )
    result["diff_items"][0] = {
        "diff_id": "diff_000001",
        "diff_type": "PAGE_MISSING",
        "certainty": "INFERRED",
        "title": "疑似整页或连续大段内容缺失",
        "baseline": {
            "file_id": "fil_base",
            "text": "设备交付和验收的连续约定",
            "location": {"paragraph_index": 119},
        },
        "target": {
            "file_id": "fil_target",
            "text": "",
            "location": {"page": 18},
        },
        "segments": [
            {"operation": "DELETE", "text": "设备交付和验收的连续约定"}
        ],
        "missing_detail": {
            "boundary": "MIDDLE",
            "estimated_page_equivalent": 1.1,
            "target_anchor_before_page": 18,
            "target_anchor_after_page": 19,
            "structure_unit_count": 4,
            "aggregated_diff_count": 4,
            "content_summary": "设备交付和验收的连续约定",
        },
    }

    ensure_fallback_risk_advices(result)

    advice = result["risk_items"][0]["analysis_advice"]
    assert "当前合同.docx" in advice
    assert "融资租赁合同.docx" in advice
    assert "疑似缺少一页" in advice
    assert "页码连续性" in advice
    payload_diff = advice_payload(result)["diff_items"][0]
    assert payload_diff["diff_type"] == "PAGE_MISSING"
    assert payload_diff["certainty"] == "INFERRED"
    assert payload_diff["missing_detail"]["estimated_page_equivalent"] == 1.1
