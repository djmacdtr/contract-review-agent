from __future__ import annotations

import pytest

from app.adapters.llm.base import LlmResult
from app.adapters.llm.openai_client import LlmClientError
from app.core.config import Settings
from app.documents.models import DocumentBlock, DocumentLocation, ParsedDocument
from app.draft_review.extraction import _checkpoint_batch_id
from app.draft_review.facts import NUMERIC_EXTRACTION_VERSION, plan_numeric_document_batches
from scripts.numeric_recovery_diagnostic import (
    prepare_numeric_plans,
    reconstruct_exact_numeric_plan,
    run_exact_numeric_canary,
    run_numeric_probe,
    safe_llm_error_metadata,
    safe_task_details,
)


def diagnostic_document() -> ParsedDocument:
    blocks = [
        DocumentBlock(
            block_id=f"numeric_{index}",
            type="PARAGRAPH",
            order=index,
            raw_text=f"租赁金额为{index + 1}万元。",
            normalized_text=f"租赁金额为{index + 1}万元。",
            location=DocumentLocation(paragraph_index=index),
        )
        for index in range(6)
    ]
    return ParsedDocument(
        file_id="diagnostic_file",
        role="TARGET",
        file_name="diagnostic.docx",
        sha256="d" * 64,
        page_count=None,
        parser_name="fixture",
        blocks=blocks,
    )


class TruncatesUntilSingleton:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def extract_numeric_candidates(
        self, payload: dict, *, allow_structure_correction: bool = True
    ) -> LlmResult:
        self.payloads.append(payload)
        if len(payload["units"]) > 1:
            raise LlmClientError(
                "LLM_OUTPUT_TRUNCATED",
                "diagnostic truncation",
                finish_reason="length",
            )
        return LlmResult(
            value={
                "items": [
                    {
                        "candidate_index": 1,
                        "semantic_key": "amount",
                        "display_name": "金额",
                        "value_type": "MONEY",
                        "decision": "IGNORE",
                        "reason_code": "DIAGNOSTIC",
                        "confidence": 0.9,
                    }
                ]
            },
            configured_model="diagnostic",
            actual_model="diagnostic",
            mock=True,
        )


@pytest.mark.asyncio
async def test_diagnostic_uses_only_6_to_3_to_1_and_new_payloads() -> None:
    document = diagnostic_document()
    plan = plan_numeric_document_batches(document, max_numeric_units=6)[0]
    client = TruncatesUntilSingleton()

    result = await run_numeric_probe(client, document, plan)  # type: ignore[arg-type]

    assert result == {
        "status": "SUCCEEDED",
        "llm_calls": 3,
        "attempted_unit_counts": [6, 3, 1],
        "failure_code": None,
    }
    assert len({payload["batch_id"] for payload in client.payloads}) == 3


def test_diagnostic_task_details_keep_only_safe_fields() -> None:
    details = safe_task_details(
        {
            "failure_stage": "FACT_EXTRACTION",
            "chain": "numeric",
            "file_id": "fil_safe",
            "batch_depth": 1,
            "unit_count": 10,
            "batch_id": "batch_safe",
            "numeric_candidate_count": 1,
            "failure_code": "LLM_OUTPUT_TRUNCATED",
            "message": "合同正文和 https://secret.invalid/key",
            "api_key": "secret",
        }
    )

    assert details == {
        "failure_stage": "FACT_EXTRACTION",
        "chain": "numeric",
        "file_id": "fil_safe",
        "batch_depth": 1,
        "unit_count": 10,
        "batch_id": "batch_safe",
        "numeric_candidate_count": 1,
        "failure_code": "LLM_OUTPUT_TRUNCATED",
    }


def test_diagnostic_llm_metadata_excludes_response_content() -> None:
    error = LlmClientError(
        "LLM_OUTPUT_TRUNCATED",
        "safe failure",
        finish_reason="length",
        content_chars=12,
        reasoning_content_chars=345,
        usage={"prompt_tokens": 10, "completion_tokens": 2048, "raw": 1},
        max_tokens=2048,
    )

    assert safe_llm_error_metadata(error) == {
        "finish_reason": "length",
        "content_chars": 12,
        "reasoning_content_chars": 345,
        "usage": {"prompt_tokens": 10, "completion_tokens": 2048},
        "max_tokens": 2048,
    }
    assert "safe failure" not in str(safe_llm_error_metadata(error))


def test_reconstructs_only_exact_historical_singleton_batch_id() -> None:
    document = diagnostic_document()
    plans = prepare_numeric_plans(document, Settings(_env_file=None))
    requested_batch_id = _checkpoint_batch_id(
        document,
        [document.blocks[0]],
        NUMERIC_EXTRACTION_VERSION,
    )

    matches = reconstruct_exact_numeric_plan(document, plans, requested_batch_id)

    assert len(matches) == 1
    assert matches[0]["batch_id"] == requested_batch_id
    assert len(matches[0]["blocks"]) == 1
    assert len(matches[0]["payload"]["units"]) == 1


@pytest.mark.asyncio
async def test_empty_numeric_canary_is_skipped_without_llm_call() -> None:
    source = diagnostic_document()
    document = source.model_copy(
        update={
            "blocks": [
                *source.blocks,
                source.blocks[0].model_copy(
                    update={
                        "block_id": "non_numeric",
                        "order": 6,
                        "raw_text": "项目说明。",
                        "normalized_text": "项目说明。",
                        "location": DocumentLocation(paragraph_index=6),
                    }
                ),
            ]
        }
    )
    plans = prepare_numeric_plans(
        document,
        Settings(_env_file=None, LLM_EXTRACTION_MAX_NUMERIC_UNITS=6),
        include_empty=True,
    )
    empty_plan = next(plan for plan in plans if plan["numeric_candidate_count"] == 0)

    class NoCallClient:
        async def extract_numeric_candidates(self, payload: dict, **kwargs) -> LlmResult:
            raise AssertionError("empty numeric canary must not call the LLM")

    result = await run_exact_numeric_canary(NoCallClient(), document, empty_plan)  # type: ignore[arg-type]

    assert result["status"] == "SKIPPED_EMPTY"
    assert result["llm_calls"] == 0
