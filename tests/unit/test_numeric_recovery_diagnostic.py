from __future__ import annotations

import pytest

from app.adapters.llm.base import LlmResult
from app.adapters.llm.openai_client import LlmClientError
from app.documents.models import DocumentBlock, DocumentLocation, ParsedDocument
from app.draft_review.facts import plan_numeric_document_batches
from scripts.numeric_recovery_diagnostic import (
    run_numeric_probe,
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
        "failure_code": "LLM_OUTPUT_TRUNCATED",
    }
