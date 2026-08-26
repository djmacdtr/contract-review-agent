import httpx
import pytest
from pydantic import ValidationError

from app.adapters.llm.base import LlmResult
from app.adapters.llm.openai_client import LlmClientError, OpenAIContractLlmClient
from app.adapters.llm.schemas import NumericCandidateExtraction, TextFactExtraction
from app.core.config import Settings
from app.core.errors import WorkflowError
from app.documents.models import DocumentBlock, DocumentLocation, ParsedDocument
from app.draft_review.extraction import extract_documents_with_wave_map_reduce
from app.draft_review.facts import (
    EvidenceValidationError,
    build_numeric_candidate_payload,
    build_text_fact_payload,
    expand_numeric_candidate_response,
    expand_text_fact_response,
    plan_simplified_document_batches,
    stable_batch_id,
)


def document() -> ParsedDocument:
    return ParsedDocument(
        file_id="file_a",
        role="REFERENCE",
        file_name="synthetic.docx",
        sha256="a" * 64,
        page_count=None,
        parser_name="fixture",
        blocks=[
            DocumentBlock(
                block_id="p0",
                type="PARAGRAPH",
                order=0,
                raw_text="保证人为甲方，期限为12个月。",
                normalized_text="保证人为甲方，期限为12个月。",
                location=DocumentLocation(paragraph_index=0),
            ),
            DocumentBlock(
                block_id="p1",
                type="PARAGRAPH",
                order=1,
                raw_text="项目名称为测试项目。",
                normalized_text="项目名称为测试项目。",
                location=DocumentLocation(paragraph_index=1),
            ),
        ],
    )


def test_v2_schemas_forbid_model_owned_evidence_and_identity() -> None:
    with pytest.raises(ValidationError):
        NumericCandidateExtraction.model_validate(
            {
                "items": [
                    {
                        "candidate_index": 1,
                        "semantic_key": "term",
                        "display_name": "期限",
                        "value_type": "DURATION",
                        "decision": "FACT",
                        "reason_code": "VALUE",
                        "confidence": 0.9,
                        "source_file_id": "secret",
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        TextFactExtraction.model_validate(
            {
                "items": [
                    {
                        "unit_id": "unit_0123456789abcdef",
                        "semantic_key": "party",
                        "display_name": "主体",
                        "value_type": "ENTITY",
                        "quote": "甲方",
                        "confidence": 0.9,
                        "location": {"paragraph_index": 0},
                    }
                ]
            }
        )


def test_numeric_candidates_must_be_exactly_once_and_program_rehydrates() -> None:
    doc = document()
    payload = build_numeric_candidate_payload(doc, [doc.blocks[0]], batch_id="batch_a")
    facts, classified = expand_numeric_candidate_response(
        payload,
        {
            "items": [
                {
                    "candidate_index": 1,
                    "semantic_key": "lease_term",
                    "display_name": "期限",
                    "value_type": "DURATION",
                    "decision": "FACT",
                    "reason_code": "BUSINESS_VALUE",
                    "confidence": 0.9,
                }
            ]
        },
    )
    assert classified == {1}
    assert facts[0].raw_value.startswith("12")
    assert facts[0].source_file_id == doc.file_id
    assert facts[0].evidence_text == doc.blocks[0].raw_text

    with pytest.raises(EvidenceValidationError, match="exactly once"):
        expand_numeric_candidate_response(payload, {"items": []})


def test_text_quote_is_exact_and_saturated_batches_are_rejected() -> None:
    doc = document()
    payload = build_text_fact_payload(doc, [doc.blocks[0]], batch_id="batch_b")
    facts = expand_text_fact_response(
        payload,
        {
            "items": [
                {
                    "unit_id": payload["units"][0]["unit_id"],
                    "semantic_key": "guarantor",
                    "display_name": "保证人",
                    "value_type": "ENTITY",
                    "quote": "甲方",
                    "confidence": 0.9,
                }
            ]
        },
    )
    assert facts[0].location.paragraph_index == 0
    with pytest.raises(EvidenceValidationError, match="exact substring"):
        expand_text_fact_response(
            payload,
            {
                "items": [
                    {
                        "unit_id": payload["units"][0]["unit_id"],
                        "semantic_key": "guarantor",
                        "display_name": "保证人",
                        "value_type": "ENTITY",
                        "quote": "乙方",
                        "confidence": 0.9,
                    }
                ]
            },
        )
    saturated = {
        "items": [
            {
                "unit_id": payload["units"][0]["unit_id"],
                "semantic_key": f"field_{index}",
                "display_name": "主体",
                "value_type": "ENTITY",
                "quote": "甲方",
                "confidence": 0.9,
            }
            for index in range(12)
        ]
    }
    with pytest.raises(EvidenceValidationError, match="saturation"):
        expand_text_fact_response(payload, saturated)


def test_v2_batch_id_is_file_content_and_version_addressed() -> None:
    doc = document()
    first = stable_batch_id("a" * 64, [doc.blocks[0]])
    second = stable_batch_id("b" * 64, [doc.blocks[0]])
    assert first != second
    plans = plan_simplified_document_batches(
        doc,
        max_payload_chars=12000,
        max_numeric_candidates=24,
        estimated_output_token_limit=2000,
    )
    assert plans
    assert all(plan["batch_id"].startswith("batch_") for plan in plans)
    assert all("file_id" not in item for item in plans[0]["numeric_payload"]["numeric_candidates"])


class WaveLlm:
    def __init__(self) -> None:
        self.profile_calls = 0
        self.numeric_calls = 0
        self.text_calls = 0

    async def extract_document_profile(self, payload: dict) -> LlmResult:
        self.profile_calls += 1
        return LlmResult(
            value={
                "document_kind": "合成资料",
                "title": None,
                "confidence": 0.9,
                "evidence_locations": [payload["overview_blocks"][0]["location"]],
            },
            configured_model="wave",
            actual_model="wave",
            mock=False,
        )

    async def extract_numeric_candidates(self, payload: dict) -> LlmResult:
        self.numeric_calls += 1
        return LlmResult(
            value={
                "items": [
                    {
                        "candidate_index": item["candidate_index"],
                        "semantic_key": "lease_term",
                        "display_name": "期限",
                        "value_type": "DURATION",
                        "decision": "IGNORE",
                        "reason_code": "SYNTHETIC",
                        "confidence": 0.9,
                    }
                    for item in payload["numeric_candidates"]
                ]
            },
            configured_model="wave",
            actual_model="wave",
            mock=False,
        )

    async def extract_text_facts(self, payload: dict) -> LlmResult:
        self.text_calls += 1
        return LlmResult(
            value={"items": []},
            configured_model="wave",
            actual_model="wave",
            mock=False,
        )


@pytest.mark.asyncio
async def test_wave_controller_profiles_once_and_completes_reduce() -> None:
    llm = WaveLlm()
    result, _ = await extract_documents_with_wave_map_reduce(
        settings=Settings(_env_file=None, LLM_ENABLED=False),
        documents=[document()],
        llm=llm,  # type: ignore[arg-type]
    )
    assert llm.profile_calls == 1
    assert llm.numeric_calls == llm.text_calls
    assert result["file_a"]["chunk_count"] >= 1
    assert result["file_a"]["first_wave_success_rate"] == 1


@pytest.mark.asyncio
async def test_wave_controller_fuses_when_first_wave_is_below_ninety_percent() -> None:
    blocks = [
        DocumentBlock(
            block_id=f"long_{index}",
            type="PARAGRAPH",
            order=index,
            raw_text=(f"第{index}条：") + ("合成业务内容。" * 220),
            normalized_text=(f"第{index}条：") + ("合成业务内容。" * 220),
            location=DocumentLocation(paragraph_index=index),
        )
        for index in range(8)
    ]
    doc = document().model_copy(update={"blocks": blocks})

    class FailingText(WaveLlm):
        async def extract_text_facts(self, payload: dict) -> LlmResult:
            self.text_calls += 1
            raise LlmClientError("LLM_INVALID_JSON", "synthetic format failure")

    with pytest.raises(WorkflowError, match="90%"):
        await extract_documents_with_wave_map_reduce(
            settings=Settings(
                _env_file=None,
                LLM_ENABLED=False,
                LLM_EXTRACTION_PAYLOAD_MAX_CHARS=4000,
            ),
            documents=[doc],
            llm=FailingText(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_v2_invalid_json_is_not_retried_on_the_same_payload() -> None:
    doc = document()
    payload = build_numeric_candidate_payload(doc, [doc.blocks[0]], batch_id="batch_json")
    bodies: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append({"size": len(request.content)})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "not-json"}}
                ]
            },
        )

    settings = Settings(
        _env_file=None,
        LLM_ENABLED=True,
        LLM_BASE_URL="https://llm.example.com",
        LLM_API_KEY="synthetic-key",
    )
    client = OpenAIContractLlmClient(
        settings,
        transport=httpx.MockTransport(handler),
        sleeper=lambda _delay: _no_sleep(),
    )
    with pytest.raises(LlmClientError) as raised:
        await client.extract_numeric_candidates(payload)
    assert raised.value.code == "LLM_INVALID_JSON"
    assert len(bodies) == 1


async def _no_sleep() -> None:
    return None
