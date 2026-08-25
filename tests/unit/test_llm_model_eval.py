import json

import httpx
import pytest

from app.adapters.llm.schemas import (
    CompactDocumentFactExtraction,
    DocumentFactExtraction,
    FactReview,
)
from app.core.config import Settings
from app.documents.models import DocumentBlock, DocumentLocation
from app.draft_review.facts import (
    expand_compact_extraction,
    numeric_candidate_metrics,
    numeric_candidates,
)
from scripts.llm_model_eval import (
    CallBudget,
    LogicalResult,
    assess_extraction,
    assess_review,
    extraction_fixture,
    fact_eval_id,
    review_fixture,
    send_attempt,
    stopped_by_transport,
)


def valid_extraction() -> dict:
    payload = extraction_fixture()
    facts = [
        ("amount", "金额", "MONEY", "人民币1200万元", 2),
        ("term", "期限", "DURATION", "24个月", 2),
        ("rate", "年利率", "PERCENTAGE", "4.25%", 2),
        ("date", "方案日期", "DATE", "2026年8月20日", 3),
        ("quantity", "设备数量", "QUANTITY", "8台", 3),
    ]
    return {
        "profile": {
            "file_id": payload["file_id"],
            "document_kind": "项目审批摘要",
            "title": "项目审批摘要",
            "confidence": 0.95,
            "evidence_locations": [{"paragraph_index": 0}],
        },
        "facts": [
            {
                "field_key": key,
                "display_name": display,
                "value_type": value_type,
                "raw_value": raw,
                "normalized_hint": None,
                "source_file_id": payload["file_id"],
                "evidence_text": payload["blocks"][paragraph]["text"],
                "location": {"paragraph_index": paragraph},
                "confidence": 0.95,
            }
            for key, display, value_type, raw, paragraph in facts
        ],
        "missing_field_keys": [],
        "semantic_concepts": [],
        "validation_specs": [],
    }


def valid_compact_extraction() -> dict:
    value = valid_extraction()
    return {
        "profile": value["profile"],
        "facts": [
            {
                key: fact[key]
                for key in (
                    "field_key",
                    "concept_id",
                    "display_name",
                    "value_type",
                    "raw_value",
                    "location",
                    "confidence",
                )
                if key in fact
            }
            for fact in value["facts"]
        ],
    }


def test_compact_extraction_expands_dynamic_facts_and_numeric_candidates() -> None:
    payload = extraction_fixture()
    compact = CompactDocumentFactExtraction.model_validate({
        "profile": {
            "file_id": payload["file_id"],
            "document_kind": "项目审批摘要",
            "title": None,
            "confidence": 0.95,
            "evidence_locations": [{"paragraph_index": 0}],
        },
        "facts": [
            {
                "field_key": field_key,
                "display_name": field_key,
                "value_type": value_type,
                "raw_value": raw_value,
                "location": {"paragraph_index": paragraph_index},
                "confidence": 0.95,
            }
            for field_key, value_type, raw_value, paragraph_index in (
                ("approving_party", "ENTITY", "甲公司", 1),
                ("project_identifier", "IDENTIFIER", "SYN-2026-001", 1),
                ("financing_amount", "MONEY", "人民币1200万元", 2),
                ("lease_term", "DURATION", "24个月", 2),
                ("annual_rate", "RATE", "4.25%", 2),
                ("plan_date", "DATE", "2026年8月20日", 3),
                ("equipment_count", "QUANTITY", "8台", 3),
                ("down_payment_ratio", "PERCENTAGE", "10%", 3),
            )
        ],
    })
    expanded = expand_compact_extraction(payload, compact)

    assert expanded.profile.file_id == payload["file_id"]
    assert {fact.field_key for fact in expanded.facts} == {
        "approving_party",
        "project_identifier",
        "financing_amount",
        "lease_term",
        "annual_rate",
        "plan_date",
        "equipment_count",
        "down_payment_ratio",
    }
    assert all(fact.source_file_id == payload["file_id"] for fact in expanded.facts)
    assert all(fact.evidence_text for fact in expanded.facts)

    block = DocumentBlock(
        block_id="numeric-test",
        type="PARAGRAPH",
        order=0,
        raw_text="金额1200万元，期限24个月，日期2026年8月20日，比例4.25%，数量8台。",
        normalized_text="金额1200万元，期限24个月，日期2026年8月20日，比例4.25%，数量8台。",
        location=DocumentLocation(paragraph_index=0),
    )
    candidates = numeric_candidates([block])
    candidate_values = {item["raw_value"] for item in candidates}
    assert {"1200万元", "24个月", "2026年8月20日", "4.25%", "8台"} <= candidate_values


def test_numeric_candidates_prioritize_typed_spans_and_suppress_structural_numbers() -> None:
    block = DocumentBlock(
        block_id="numeric-quality",
        type="PARAGRAPH",
        order=0,
        raw_text=(
            "第3条约定页码2，编号ZX-2026-01；金额1,200万元，期限24个月，"
            "日期2026年8月20日，比例5%，数量8台。"
        ),
        normalized_text="",
        location=DocumentLocation(paragraph_index=0),
    )

    candidates = numeric_candidates([block])
    metrics = numeric_candidate_metrics([block])
    kinds = {item["candidate_kind"] for item in candidates}

    assert {"IDENTIFIER", "MONEY", "DURATION", "DATE", "PERCENTAGE", "QUANTITY"} <= kinds
    assert not any(item["raw_value"] in {"3", "2"} for item in candidates)
    assert metrics["candidate_unique"] == len(candidates)
    assert metrics["suppressed_count"] > 0
    assert metrics["type_counts"]["MONEY"] == 1
    assert metrics["batch_count"] == 1


def eval_settings() -> Settings:
    return Settings(
        _env_file=None,
        LLM_ENABLED=True,
        LLM_BASE_URL="https://llm.example.com/v1",
        LLM_API_KEY="secret",
    )


async def test_send_attempt_records_safe_first_byte_and_strict_schema() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["temperature"] == 0
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                valid_compact_extraction(), ensure_ascii=False
                            )
                        },
                    }
                ],
            },
        )

    attempt = await send_attempt(
        eval_settings(),
        CallBudget(1),
        model="model-a",
        role="extraction",
        response_mode="json_schema",
        system="return JSON",
        payload=extraction_fixture(),
        schema=CompactDocumentFactExtraction,
        correction=0,
        transport=httpx.MockTransport(handler),
    )

    assert attempt.first_byte_ms is not None
    assert attempt.base_compliant is True
    assert attempt.output_metrics["array_counts"]["facts"] == 5
    assert attempt.output_metrics["string_max_lengths"]["facts[].evidence_text"] > 0
    assert attempt.request_body_chars > attempt.schema_chars > 0
    assert attempt.request_metrics["candidate_unique"] == 8
    assert "1200万元" not in json.dumps(attempt.safe_dict(), ensure_ascii=False)
    assert "secret" not in json.dumps(attempt.safe_dict())
    assert "facts" not in attempt.safe_dict()


async def test_fenced_output_is_classified_without_relaxed_json_acceptance() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        content = json.dumps(valid_compact_extraction(), ensure_ascii=False)
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [
                    {"finish_reason": "stop", "message": {"content": f"```json\n{content}\n```"}}
                ],
            },
        )

    attempt = await send_attempt(
        eval_settings(),
        CallBudget(1),
        model="model-a",
        role="extraction",
        response_mode="prompt_only",
        system="return JSON",
        payload=extraction_fixture(),
        schema=CompactDocumentFactExtraction,
        correction=0,
        transport=httpx.MockTransport(handler),
    )

    assert attempt.fenced is True
    assert attempt.strict_json is False
    assert attempt.base_compliant is False


def test_fact_eval_identity_and_grounding_are_deterministic() -> None:
    value = DocumentFactExtraction.model_validate(valid_extraction())
    first = value.facts[0]
    assert fact_eval_id(first) == fact_eval_id(first)
    assert fact_eval_id(first) != fact_eval_id(first.model_copy(update={"value_type": "TEXT"}))
    result = LogicalResult(
        model="model-a",
        role="extraction",
        response_mode="prompt_only",
        attempts=[],
    )
    result.attempts.append(type("A", (), {"value": value})())

    assess_extraction(result, extraction_fixture())

    assert result.evidence_valid is True
    assert result.identity_valid is True
    assert result.quality_score == 100
    assert result.detail_counts["fact_count"] == 5


def test_call_budget_and_transport_stop_are_bounded() -> None:
    budget = CallBudget(1)
    budget.take()
    with pytest.raises(RuntimeError, match="budget"):
        budget.take()

    result = LogicalResult(
        model="model-a",
        role="extraction",
        response_mode="prompt_only",
        attempts=[],
    )
    result.attempts.append(type("A", (), {"error_code": "TIMEOUT", "http_status": None})())
    assert stopped_by_transport(result) is True


def test_review_fixture_accepts_both_grounded_term_facts() -> None:
    payload, expected = review_fixture()
    decisions = []
    for fact in payload["facts"]:
        identity = (
            fact["field_key"],
            fact["source_file_id"],
            (
                None,
                fact["location"].get("paragraph_index"),
                None,
                None,
                None,
            ),
        )
        decision = "ACCEPT" if fact["field_key"] in {
            "financing_amount",
            "lease_term_primary",
            "lease_term_conflict",
        } else "REJECT"
        assert decision in expected[identity]
        decisions.append(
            {
                "field_key": fact["field_key"],
                "source_file_id": fact["source_file_id"],
                "location": fact["location"],
                "decision": decision,
                "evidence_text": fact["evidence_text"],
                "confidence": 0.95,
                "reason_code": "EVIDENCE_MATCH" if decision == "ACCEPT" else "EVIDENCE_MISMATCH",
            }
        )
    review = FactReview.model_validate(
        {
            "file_id": payload["file_id"],
            "decisions": decisions,
            "semantic_concepts": [],
            "validation_specs": [],
            "confidence": 0.95,
            "evidence_complete": True,
        }
    )
    result = LogicalResult(
        model="model-a",
        role="review",
        response_mode="json_schema",
        attempts=[],
    )
    result.attempts.append(
        type(
            "A",
            (),
            {"value": review, "base_compliant": True},
        )()
    )

    assess_review(result, payload, expected)

    assert result.quality_score == 100
    assert result.evidence_valid is True
    assert result.identity_valid is True

    empty = review.model_copy(update={"decisions": []})
    empty_result = LogicalResult(
        model="model-a",
        role="review",
        response_mode="json_schema",
        attempts=[type("A", (), {"value": empty, "base_compliant": True})()],
    )
    assess_review(empty_result, payload, expected)
    assert empty_result.evidence_valid is False
    assert empty_result.identity_valid is False
