from __future__ import annotations

import pytest

from app.adapters.llm.base import LlmResult
from app.core.config import Settings
from app.documents.models import (
    DocumentBlock,
    DocumentLocation,
    ParsedDocument,
    ParsedTable,
    TableCell,
    TableRow,
)
from app.draft_review.extraction import extract_documents_with_independent_map_reduce
from app.draft_review.facts import (
    EvidenceValidationError,
    build_text_fact_payload,
    filter_text_fact_evidence,
    plan_text_document_batches,
    split_table_text_unit,
)
from scripts.text_grounding_diagnostic import (
    _decorate_initial_plan,
    _make_child_plan,
    reconstruct_text_batch,
    safe_task_details,
)


def paragraph_document() -> ParsedDocument:
    return ParsedDocument(
        file_id="file_text",
        role="REFERENCE",
        file_name="text.docx",
        sha256="a" * 64,
        page_count=None,
        parser_name="fixture",
        blocks=[
            DocumentBlock(
                block_id="p0",
                type="PARAGRAPH",
                order=0,
                raw_text="保证人为甲方，项目名称为测试项目。",
                normalized_text="保证人为甲方，项目名称为测试项目。",
                location=DocumentLocation(paragraph_index=0),
            )
        ],
    )


def table_document() -> ParsedDocument:
    cells = [
        TableCell(
            raw_text=value,
            normalized_text=value,
            location=DocumentLocation(table_index=0, row=4, column=index),
        )
        for index, value in enumerate(("字段", "甲方", "项目一", "备注"))
    ]
    row = TableRow(row=4, cells=cells)
    block = DocumentBlock(
        block_id="table",
        type="TABLE",
        order=0,
        raw_text="\t".join(cell.raw_text for cell in cells),
        normalized_text="\t".join(cell.raw_text for cell in cells),
        location=DocumentLocation(table_index=0, row=4),
        table=ParsedTable(table_index=0, rows=[row]),
    )
    return ParsedDocument(
        file_id="file_text",
        role="REFERENCE",
        file_name="table.docx",
        sha256="b" * 64,
        page_count=None,
        parser_name="fixture",
        blocks=[block],
    )


def test_text_evidence_filter_keeps_grounded_candidates_and_discards_one() -> None:
    document = paragraph_document()
    payload = build_text_fact_payload(document, document.blocks, batch_id="batch_text")
    unit_id = payload["units"][0]["unit_id"]

    facts, discarded = filter_text_fact_evidence(
        document,
        payload,
        {
            "items": [
                {
                    "unit_id": unit_id,
                    "semantic_key": "guarantor",
                    "display_name": "保证人",
                    "value_type": "ENTITY",
                    "quote": "乙方",
                    "confidence": 0.9,
                },
                {
                    "unit_id": unit_id,
                    "semantic_key": "guarantor",
                    "display_name": "保证人",
                    "value_type": "ENTITY",
                    "quote": "甲方",
                    "confidence": 0.9,
                },
            ],
            "has_more": False,
        },
    )

    assert [fact.field_key for fact in facts] == ["guarantor"]
    assert discarded == {"FACT_QUOTE_NOT_GROUNDED": 1}
    assert facts[0].raw_value == "甲方"


def test_text_evidence_filter_all_invalid_candidates_returns_empty() -> None:
    document = paragraph_document()
    payload = build_text_fact_payload(document, document.blocks, batch_id="batch_text")
    unit_id = payload["units"][0]["unit_id"]

    facts, discarded = filter_text_fact_evidence(
        document,
        payload,
        {
            "items": [
                {
                    "unit_id": unit_id,
                    "semantic_key": "invented_party",
                    "display_name": "虚构主体",
                    "value_type": "ENTITY",
                    "quote": "乙方",
                    "confidence": 0.9,
                }
            ],
            "has_more": False,
        },
    )

    assert facts == []
    assert discarded == {"FACT_QUOTE_NOT_GROUNDED": 1}


def test_text_evidence_filter_keeps_document_identity_strict() -> None:
    document = paragraph_document()
    payload = build_text_fact_payload(document, document.blocks, batch_id="batch_text")
    payload["file_id"] = "different_file"

    with pytest.raises(EvidenceValidationError) as caught:
        filter_text_fact_evidence(
            document,
            payload,
            {"items": [], "has_more": False},
        )

    assert caught.value.code == "FACT_SOURCE_FILE_MISMATCH"


def test_text_evidence_filter_discards_missing_quote_without_raising() -> None:
    document = paragraph_document()
    payload = build_text_fact_payload(document, document.blocks, batch_id="batch_text")
    unit_id = payload["units"][0]["unit_id"]

    facts, discarded = filter_text_fact_evidence(
        document,
        payload,
        {
            "items": [
                {
                    "unit_id": unit_id,
                    "semantic_key": "guarantor",
                    "display_name": "保证人",
                    "value_type": "ENTITY",
                    "quote": None,
                    "confidence": 0.9,
                }
            ],
            "has_more": False,
        },
    )

    assert facts == []
    assert discarded == {"FACT_QUOTE_NOT_GROUNDED": 1}


def test_text_evidence_filter_keeps_schema_and_saturation_strict() -> None:
    document = paragraph_document()
    payload = build_text_fact_payload(document, document.blocks, batch_id="batch_text")
    unit_id = payload["units"][0]["unit_id"]
    saturated = {
        "items": [
            {
                "unit_id": unit_id,
                "semantic_key": f"field_{index}",
                "display_name": "主体",
                "value_type": "ENTITY",
                "quote": "甲方",
                "confidence": 0.9,
            }
            for index in range(12)
        ],
        "has_more": False,
    }

    accepted, discarded = filter_text_fact_evidence(document, payload, saturated)
    assert len(accepted) == 12
    assert discarded == {}


def test_text_diagnostic_reconstructs_a_table_cell_child_batch() -> None:
    document = table_document()
    settings = Settings(_env_file=None)
    initial = plan_text_document_batches(
        document,
        max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
        max_text_units=1,
        max_text_facts=12,
        estimated_output_token_limit=min(
            settings.LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS,
            2000,
        ),
    )
    parent = _decorate_initial_plan(initial[0], planned_batch_count=len(initial))
    cell_block = split_table_text_unit(parent["blocks"][0])[1]
    child = _make_child_plan(
        document,
        parent,
        [cell_block],
        text_fact_limit=12,
    )

    rebuilt = reconstruct_text_batch(document, child["batch_id"], settings)

    assert rebuilt is not None
    assert rebuilt["batch_id"] == child["batch_id"]
    assert rebuilt["depth"] == 1
    assert len(rebuilt["blocks"]) == 1


def test_text_diagnostic_reconstructs_production_balanced_depth_two_batch() -> None:
    source = paragraph_document()
    blocks = [
        DocumentBlock(
            block_id=f"production_{index}",
            type="PARAGRAPH",
            order=index,
            raw_text=f"条款{index}内容。",
            normalized_text=f"条款{index}内容。",
            location=DocumentLocation(paragraph_index=index),
        )
        for index in range(16)
    ]
    document = source.model_copy(update={"blocks": blocks})
    settings = Settings(_env_file=None)
    initial = plan_text_document_batches(
        document,
        max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
        max_text_units=16,
        max_text_facts=12,
        estimated_output_token_limit=min(
            settings.LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS,
            2000,
        ),
    )
    parent = _decorate_initial_plan(initial[0], planned_batch_count=len(initial))
    middle = _make_child_plan(document, parent, blocks[:8], text_fact_limit=12)
    target = _make_child_plan(document, middle, blocks[:4], text_fact_limit=12)

    rebuilt = reconstruct_text_batch(document, target["batch_id"], settings)

    assert rebuilt is not None
    assert rebuilt["batch_id"] == target["batch_id"]
    assert rebuilt["depth"] == 2
    assert [block.block_id for block in rebuilt["blocks"]] == [
        f"production_{index}" for index in range(4)
    ]


def test_text_diagnostic_details_exclude_body_and_credentials() -> None:
    details = safe_task_details(
        {
            "failure_stage": "FACT_EXTRACTION",
            "chain": "text",
            "file_id": "fil_safe",
            "batch_depth": 1,
            "unit_count": 1,
            "batch_id": "batch_safe",
            "failure_code": "FACT_VALUE_NOT_GROUNDED",
            "message": "合同正文 https://secret.invalid/key",
            "api_key": "secret",
        }
    )

    assert details == {
        "failure_stage": "FACT_EXTRACTION",
        "chain": "text",
        "file_id": "fil_safe",
        "batch_depth": 1,
        "unit_count": 1,
        "batch_id": "batch_safe",
        "failure_code": "FACT_VALUE_NOT_GROUNDED",
    }


@pytest.mark.asyncio
async def test_independent_text_chain_filters_one_bad_candidate_without_failing() -> None:
    document = paragraph_document()

    class CandidateLlm:
        async def extract_document_profile(self, payload: dict) -> LlmResult:
            return LlmResult(
                value={
                    "document_kind": "项目资料",
                    "title": None,
                    "confidence": 0.9,
                    "evidence_locations": [payload["overview_blocks"][0]["location"]],
                },
                configured_model="test",
                actual_model="test",
                mock=True,
            )

        async def extract_numeric_candidates(self, payload: dict) -> LlmResult:
            return LlmResult(
                value={"items": []},
                configured_model="test",
                actual_model="test",
                mock=True,
            )

        async def extract_text_facts(
            self, payload: dict, *, allow_structure_correction: bool = True
        ) -> LlmResult:
            unit_id = payload["units"][0]["unit_id"]
            return LlmResult(
                value={
                    "items": [
                        {
                            "unit_id": unit_id,
                            "semantic_key": "guarantor",
                            "display_name": "保证人",
                            "value_type": "ENTITY",
                            "quote": "甲方",
                            "confidence": 0.9,
                        },
                        {
                            "unit_id": unit_id,
                            "semantic_key": "invented_party",
                            "display_name": "虚构主体",
                            "value_type": "ENTITY",
                            "quote": "乙方",
                            "confidence": 0.9,
                        },
                    ],
                    "has_more": False,
                },
                configured_model="test",
                actual_model="test",
                mock=True,
            )

    result, _ = await extract_documents_with_independent_map_reduce(
        settings=Settings(_env_file=None, LLM_ENABLED=False),
        documents=[document],
        llm=CandidateLlm(),  # type: ignore[arg-type]
    )

    assert result[document.file_id]["value"]["facts"]
    assert result[document.file_id]["value"]["facts"][0]["field_key"] == "guarantor"
    assert result[document.file_id]["discarded_fact_count"] == 1
