import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.adapters.llm.base import LlmResult
from app.adapters.llm.openai_client import LlmClientError, OpenAIContractLlmClient
from app.adapters.llm.schemas import FactCandidate, NumericCandidateExtraction, TextFactExtraction
from app.comparison.models import DiffItem, DiffSegment, DiffSide
from app.core.config import Settings
from app.core.errors import WorkflowError
from app.documents.models import (
    DocumentBlock,
    DocumentLocation,
    ParsedDocument,
    ParsedTable,
    TableCell,
    TableRow,
)
from app.draft_review.checkpoints import (
    ExtractionCheckpoint,
    InMemoryExtractionCheckpointStore,
)
from app.draft_review.extraction import (
    DOCUMENT_EXTRACTION_CHECKPOINT_VERSION,
    _checkpoint_payload_digest,
    _document_checkpoint_identity_values,
    _failure_details,
    _payload_digest,
    _split_text_structure_unit,
    _validate_fact_identity_set,
    extract_documents_with_independent_map_reduce,
    extract_documents_with_wave_map_reduce,
    numeric_recovery_blocks,
    text_recovery_blocks,
)
from app.draft_review.facts import (
    EvidenceValidationError,
    build_document_overview_payload,
    build_numeric_candidate_payload,
    build_template_text_candidates,
    build_text_fact_payload,
    expand_numeric_candidate_response,
    expand_text_fact_response,
    match_quote_to_source,
    numeric_candidate_indexes,
    plan_numeric_document_batches,
    plan_simplified_document_batches,
    plan_text_candidate_batches,
    plan_text_document_batches,
    rehydrate_fact_evidence,
    rehydrate_numeric_fact_evidence,
    split_table_text_unit,
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
    assert payload["requirements"]["required_decision_count"] == 1
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


def test_numeric_index_diagnostics_are_safe_and_reach_failure_details() -> None:
    error = LlmClientError(
        "LLM_EXTRACTION_EVIDENCE_INVALID",
        "safe failure",
        failure_code="NUMERIC_CANDIDATE_UNCLASSIFIED",
        validation_summary={
            "expected_count": 1,
            "returned_count": 1,
            "missing_index_count": 1,
            "duplicate_index_count": 0,
            "invalid_index_count": 1,
        },
    )

    details = _failure_details(
        {
            "plan": {
                "document_id": "file_a",
                "chain": "numeric",
                "depth": 0,
                "unit_ids": ["unit_a"],
                "batch_id": "batch_safe",
            },
            "error": error,
        }
    )

    assert details["expected_count"] == 1
    assert details["returned_count"] == 1
    assert details["missing_index_count"] == 1
    assert details["duplicate_index_count"] == 0
    assert details["invalid_index_count"] == 1
    assert "safe failure" not in str(details)


def test_text_quote_is_exact_and_has_more_controls_saturation() -> None:
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
            ],
            "has_more": False,
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
                ],
                "has_more": False,
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
        ],
        "has_more": False,
    }
    assert len(expand_text_fact_response(payload, saturated)) == 12
    saturated["has_more"] = True
    with pytest.raises(EvidenceValidationError, match="saturation"):
        expand_text_fact_response(payload, saturated)
    with pytest.raises(ValidationError):
        TextFactExtraction.model_validate({"items": []})


def test_failed_table_row_can_split_to_column_units_without_losing_location() -> None:
    cells = [
        TableCell(
            raw_text=value,
            normalized_text=value,
            location=DocumentLocation(table_index=2, row=4, column=column),
        )
        for column, value in enumerate(("字段", "甲方", "甲方", "备注"))
    ]
    table = ParsedTable(table_index=2, rows=[TableRow(row=4, cells=cells)])
    block = DocumentBlock(
        block_id="table_row",
        type="TABLE",
        order=0,
        raw_text="\t".join(cell.raw_text for cell in cells),
        normalized_text="\t".join(cell.raw_text for cell in cells),
        location=DocumentLocation(table_index=2, row=4),
        table=table,
    )

    children = split_table_text_unit(block)

    assert [child.location.column for child in children] == [0, 1, 2, 3]
    assert [child.raw_text for child in children] == ["字段", "甲方", "甲方", "备注"]
    assert all(child.location.table_index == 2 for child in children)


def test_reduce_rejects_conflicting_fact_identity() -> None:
    facts = [
        FactCandidate(
            field_key="rate",
            display_name="利率",
            value_type="NUMBER",
            raw_value=value,
            source_file_id="file_a",
            evidence_text=value,
            location=DocumentLocation(paragraph_index=0),
            confidence=0.9,
        )
        for value in ("6", "7")
    ]

    with pytest.raises(EvidenceValidationError) as raised:
        _validate_fact_identity_set(facts)

    assert raised.value.code == "FACT_IDENTITY_CONFLICT"


def test_candidate_fact_evidence_is_rehydrated_from_full_document_location() -> None:
    fact = FactCandidate(
        field_key="party",
        display_name="主体",
        value_type="ENTITY",
        raw_value="甲方",
        source_file_id="file_a",
        evidence_text="甲方",
        location=DocumentLocation(paragraph_index=0),
        confidence=0.9,
    )

    rehydrated = rehydrate_fact_evidence(document(), [fact])

    assert rehydrated[0].evidence_text == "甲方"


def test_numeric_rehydration_allows_repeated_short_value_at_program_location() -> None:
    doc = document().model_copy(
        update={
            "blocks": [
                DocumentBlock(
                    block_id="numeric",
                    type="PARAGRAPH",
                    order=0,
                    raw_text="编号1和1",
                    normalized_text="编号1和1",
                    location=DocumentLocation(paragraph_index=0),
                )
            ]
        }
    )
    fact = FactCandidate(
        field_key="number",
        display_name="编号",
        value_type="NUMBER",
        raw_value="1",
        source_file_id="file_a",
        evidence_text="编号1和1",
        location=DocumentLocation(paragraph_index=0),
        confidence=0.9,
    )

    rehydrated = rehydrate_numeric_fact_evidence(doc, [fact])

    assert rehydrated[0].raw_value == "1"
    assert rehydrated[0].evidence_text == "编号1和1"


@pytest.mark.asyncio
async def test_independent_reduce_recovers_ambiguous_table_row_by_columns() -> None:
    cells = [
        TableCell(
            raw_text=value,
            normalized_text=value,
            location=DocumentLocation(table_index=0, row=0, column=column),
        )
        for column, value in enumerate(("字段", "甲方", "甲方"))
    ]
    table = ParsedTable(table_index=0, rows=[TableRow(row=0, cells=cells)])
    doc = ParsedDocument(
        file_id="table_file",
        role="REFERENCE",
        file_name="table.docx",
        sha256="c" * 64,
        page_count=None,
        parser_name="fixture",
        blocks=[
            DocumentBlock(
                block_id="table",
                type="TABLE",
                order=0,
                raw_text="\t".join(cell.raw_text for cell in cells),
                normalized_text="\t".join(cell.raw_text for cell in cells),
                location=DocumentLocation(table_index=0),
                table=table,
            )
        ],
    )

    class TableRowRetryLlm(WaveLlm):
        def __init__(self) -> None:
            super().__init__()
            self.row_failed = False

        async def extract_text_facts(self, payload: dict) -> LlmResult:
            self.text_calls += 1
            if not self.row_failed and "\t" in payload["units"][0]["text"]:
                self.row_failed = True
                raise LlmClientError(
                    "LLM_EXTRACTION_EVIDENCE_INVALID",
                    "synthetic evidence failure",
                    failure_code="FACT_QUOTE_NOT_GROUNDED",
                )
            return LlmResult(
                value={"items": [], "has_more": False},
                configured_model="table-retry",
                actual_model="table-retry",
                mock=False,
            )

    llm = TableRowRetryLlm()
    result, _ = await extract_documents_with_independent_map_reduce(
        settings=Settings(_env_file=None, LLM_ENABLED=False),
        documents=[doc],
        llm=llm,  # type: ignore[arg-type]
    )

    assert llm.text_calls == 4
    assert result["table_file"]["text_batch_count"] == 3
    assert result["table_file"]["recovery_count"] == 1


@pytest.mark.asyncio
async def test_independent_reduce_reuses_completed_table_children_before_parent_retry() -> None:
    cells = [
        TableCell(
            raw_text=value,
            normalized_text=value,
            location=DocumentLocation(table_index=0, row=0, column=column),
        )
        for column, value in enumerate(("字段", "甲方", "甲方"))
    ]
    table = ParsedTable(table_index=0, rows=[TableRow(row=0, cells=cells)])
    doc = ParsedDocument(
        file_id="checkpoint_table_file",
        role="REFERENCE",
        file_name="checkpoint-table.docx",
        sha256="d" * 64,
        page_count=None,
        parser_name="fixture",
        blocks=[
            DocumentBlock(
                block_id="checkpoint_table",
                type="TABLE",
                order=0,
                raw_text="\t".join(cell.raw_text for cell in cells),
                normalized_text="\t".join(cell.raw_text for cell in cells),
                location=DocumentLocation(table_index=0),
                table=table,
            )
        ],
    )
    store = InMemoryExtractionCheckpointStore()

    class TableRowRetryLlm(WaveLlm):
        def __init__(self) -> None:
            super().__init__()
            self.row_failed = False

        async def extract_text_facts(self, payload: dict) -> LlmResult:
            self.text_calls += 1
            if not self.row_failed and "\t" in payload["units"][0]["text"]:
                self.row_failed = True
                raise LlmClientError(
                    "LLM_EXTRACTION_EVIDENCE_INVALID",
                    "synthetic evidence failure",
                    failure_code="FACT_QUOTE_NOT_GROUNDED",
                )
            return LlmResult(
                value={"items": [], "has_more": False},
                configured_model="table-retry",
                actual_model="table-retry",
                mock=False,
            )

    first = TableRowRetryLlm()
    settings = Settings(_env_file=None, LLM_ENABLED=False)

    await extract_documents_with_independent_map_reduce(
        settings=settings,
        documents=[doc],
        llm=first,  # type: ignore[arg-type]
        checkpoint_store=store,
        task_id="checkpoint_source",
    )
    assert first.text_calls == 4

    class NoParentRetry(TableRowRetryLlm):
        async def extract_text_facts(self, payload: dict) -> LlmResult:
            raise AssertionError("completed table children should be reused")

    retry = NoParentRetry()
    result, _ = await extract_documents_with_independent_map_reduce(
        settings=settings,
        documents=[doc],
        llm=retry,  # type: ignore[arg-type]
        checkpoint_store=store,
        task_id="checkpoint_retry",
        source_task_id="checkpoint_source",
    )

    assert retry.text_calls == 0
    assert result["checkpoint_table_file"]["text_batch_count"] == 0
    assert result["checkpoint_table_file"]["document_checkpoint_reused"] is True


def test_text_evidence_keeps_specific_codes_and_only_normalizes_format() -> None:
    doc = document().model_copy(
        update={
            "blocks": [
                DocumentBlock(
                    block_id="format",
                    type="PARAGRAPH",
                    order=0,
                    raw_text="主体：ＡＢ\u200b<br>保证人",
                    normalized_text="主体：ＡＢ 保证人",
                    location=DocumentLocation(paragraph_index=4),
                )
            ]
        }
    )
    payload = build_text_fact_payload(doc, doc.blocks, batch_id="batch_format")
    unit_id = payload["units"][0]["unit_id"]
    facts = expand_text_fact_response(
        payload,
        {
            "items": [
                {
                    "unit_id": unit_id,
                    "semantic_key": "party",
                    "display_name": "主体",
                    "value_type": "ENTITY",
                    "quote": "AB",
                    "confidence": 0.8,
                }
            ],
            "has_more": False,
        },
    )
    assert facts[0].raw_value == "ＡＢ"
    assert facts[0].evidence_text == "ＡＢ"
    assert match_quote_to_source("租\n赁\u200b期限", "租 赁 期限") == "租\n赁\u200b期限"
    assert match_quote_to_source("甲方和甲方", "甲 方") is None
    assert match_quote_to_source("甲方公司", "甲方") == "甲方"
    assert match_quote_to_source("甲方公司", "甲方主体") is None

    with pytest.raises(EvidenceValidationError) as unknown:
        expand_text_fact_response(
            payload,
            {
                "items": [
                    {
                        "unit_id": "unit_aaaaaaaa",
                        "semantic_key": "party",
                        "display_name": "主体",
                        "value_type": "ENTITY",
                        "quote": "ＡＢ",
                        "confidence": 0.8,
                    }
                ],
                "has_more": False,
            },
        )
    assert unknown.value.code == "FACT_UNIT_NOT_FOUND"

    with pytest.raises(EvidenceValidationError) as rewrite:
        expand_text_fact_response(
            payload,
            {
                "items": [
                    {
                        "unit_id": unit_id,
                        "semantic_key": "party",
                        "display_name": "主体",
                        "value_type": "ENTITY",
                        "quote": "ＡＢ主体",
                        "confidence": 0.8,
                    }
                ],
                "has_more": False,
            },
        )
    assert rewrite.value.code == "FACT_QUOTE_NOT_GROUNDED"


def test_template_candidates_are_delta_scoped_deduplicated_and_table_aware() -> None:
    doc = document().model_copy(
        update={
            "blocks": [
                *document().blocks,
                DocumentBlock(
                    block_id="table0",
                    type="TABLE",
                    order=2,
                    raw_text="项目\t金额\n租赁物\t100万元",
                    normalized_text="项目 金额 租赁物 100万元",
                    location=DocumentLocation(table_index=0),
                    table=ParsedTable(
                        table_index=0,
                        rows=[
                            TableRow(
                                row=0,
                                cells=[
                                    TableCell(
                                        raw_text="项目",
                                        normalized_text="项目",
                                        location=DocumentLocation(
                                            table_index=0, row=0, column=0
                                        ),
                                    ),
                                    TableCell(
                                        raw_text="金额",
                                        normalized_text="金额",
                                        location=DocumentLocation(
                                            table_index=0, row=0, column=1
                                        ),
                                    ),
                                ],
                            ),
                            TableRow(
                                row=1,
                                cells=[
                                    TableCell(
                                        raw_text="租赁物",
                                        normalized_text="租赁物",
                                        location=DocumentLocation(
                                            table_index=0, row=1, column=0
                                        ),
                                    ),
                                    TableCell(
                                        raw_text="100万元",
                                        normalized_text="100万元",
                                        location=DocumentLocation(
                                            table_index=0, row=1, column=1
                                        ),
                                    ),
                                ],
                            ),
                        ],
                    ),
                ),
            ]
        }
    )
    paragraph_location = doc.blocks[0].location
    table_location = DocumentLocation(table_index=0, row=1, column=1)

    def diff(diff_id: str, location: DocumentLocation, text: str, diff_type: str) -> DiffItem:
        return DiffItem(
            diff_id=diff_id,
            diff_type=diff_type,
            title=diff_id,
            baseline=DiffSide(file_id="template", location=location, text="待填"),
            target=DiffSide(file_id=doc.file_id, location=location, text=text),
            segments=[DiffSegment(operation="INSERT", text=text)],
            confidence=1,
        )

    duplicate = diff("duplicate", paragraph_location, "甲方", "MODIFIED")
    review = SimpleNamespace(
        diff_items=[
            diff("paragraph", paragraph_location, "甲方", "MODIFIED"),
            diff("table", table_location, "100万元", "TABLE_CELL_CHANGED"),
            DiffItem(
                diff_id="deleted",
                diff_type="DELETED",
                title="deleted",
                baseline=DiffSide(
                    file_id="template", location=paragraph_location, text="删除"
                ),
                target=None,
                confidence=1,
            ),
        ],
        diagnostics=SimpleNamespace(
            filtered_diff_items=[SimpleNamespace(diff=duplicate)]
        ),
    )

    candidates = build_template_text_candidates(review, doc)
    assert len(candidates) == 2
    assert {candidate.block.raw_text for candidate in candidates} == {"甲方", "100万元"}
    table_candidate = next(
        candidate for candidate in candidates if candidate.block.raw_text == "100万元"
    )
    assert table_candidate.block.location == table_location
    assert table_candidate.context_units
    plans = plan_text_candidate_batches(doc, candidates, max_candidates=1)
    assert len(plans) == 2
    assert all(plan["extraction_version"] == "text-v4" for plan in plans)
    assert all(len(plan["blocks"]) == 1 for plan in plans)
    payload = next(
        plan["payload"] for plan in plans if plan["blocks"][0].raw_text == "100万元"
    )
    facts = expand_text_fact_response(
        payload,
        {
            "items": [
                {
                    "unit_id": payload["units"][0]["unit_id"],
                    "semantic_key": "lease_amount",
                    "display_name": "金额",
                    "value_type": "TEXT",
                    "quote": "100万元",
                    "confidence": 0.9,
                }
            ],
            "has_more": False,
        },
    )
    assert facts[0].location == table_location
    assert facts[0].evidence_text == "100万元"


def test_text_response_drops_numeric_categories_owned_by_numeric_chain() -> None:
    doc = document()
    payload = build_text_fact_payload(doc, [doc.blocks[0]], batch_id="batch_numeric_overlap")

    facts = expand_text_fact_response(
        payload,
        {
            "items": [
                {
                    "unit_id": payload["units"][0]["unit_id"],
                    "semantic_key": "lease_term",
                    "display_name": "期限",
                    "value_type": "DURATION",
                    "quote": "12个月",
                    "confidence": 0.9,
                },
                {
                    "unit_id": payload["units"][0]["unit_id"],
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


def test_empty_target_candidate_override_does_not_fall_back_to_full_document() -> None:
    assert plan_text_candidate_batches(document(), []) == []


def test_template_candidate_falls_back_when_insert_segment_is_not_source_grounded() -> None:
    doc = document()
    location = doc.blocks[0].location
    review = SimpleNamespace(
        diff_items=[
            DiffItem(
                diff_id="normalized_insert",
                diff_type="MODIFIED",
                title="normalized insert",
                baseline=DiffSide(file_id="template", location=location, text="旧文本"),
                target=DiffSide(
                    file_id=doc.file_id,
                    location=location,
                    text=doc.blocks[0].raw_text,
                ),
                segments=[DiffSegment(operation="INSERT", text="非连续差异片段")],
                confidence=1,
            )
        ],
        diagnostics=SimpleNamespace(filtered_diff_items=[]),
    )

    candidates = build_template_text_candidates(review, doc)

    assert [candidate.block.raw_text for candidate in candidates] == [
        doc.blocks[0].raw_text
    ]


def test_table_structure_insert_stays_with_deterministic_diff() -> None:
    location = DocumentLocation(table_index=9)
    review = SimpleNamespace(
        diff_items=[
            DiffItem(
                diff_id="expanded_table",
                diff_type="TABLE_STRUCTURE_EXPANDED",
                title="expanded table",
                baseline=DiffSide(file_id="template", location=location, text="old"),
                target=DiffSide(file_id="file_a", location=location, text="new"),
                segments=[
                    DiffSegment(operation="INSERT", text="x" * 201),
                    DiffSegment(operation="INSERT", text="新增字段"),
                ],
                confidence=1,
            )
        ],
        diagnostics=SimpleNamespace(filtered_diff_items=[]),
    )
    candidates = build_template_text_candidates(review, document())
    assert candidates == []


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


def test_numeric_planner_limits_structure_units_without_dropping_candidates() -> None:
    blocks = [
        DocumentBlock(
            block_id=f"numeric_{index}",
            type="PARAGRAPH",
            order=index,
            raw_text=f"租赁金额为{index + 1}万元，租赁期限为{index + 1}个月。",
            normalized_text=f"租赁金额为{index + 1}万元，租赁期限为{index + 1}个月。",
            location=DocumentLocation(paragraph_index=index),
        )
        for index in range(8)
    ]
    doc = document().model_copy(update={"blocks": blocks})
    plans = plan_numeric_document_batches(doc, max_numeric_units=6)

    assert [len(plan["blocks"]) for plan in plans] == [6, 2]
    assert sum(plan["numeric_candidate_count"] for plan in plans) == 16
    assert all(len(plan["blocks"]) <= 6 for plan in plans)


def test_numeric_planner_splits_dense_table_cells_before_candidate_limit() -> None:
    cells = [
        TableCell(
            raw_text=f"金额{index + 1}万元",
            normalized_text=f"金额{index + 1}万元",
            location=DocumentLocation(table_index=3, row=0, column=index),
        )
        for index in range(30)
    ]
    table = ParsedTable(table_index=3, rows=[TableRow(row=0, cells=cells)])
    block = DocumentBlock(
        block_id="dense_numeric_table",
        type="TABLE",
        order=0,
        raw_text="\t".join(cell.raw_text for cell in cells),
        normalized_text="\t".join(cell.normalized_text for cell in cells),
        location=DocumentLocation(table_index=3),
        table=table,
    )
    doc = document().model_copy(update={"blocks": [block]})
    plans = plan_numeric_document_batches(doc, max_numeric_units=6)

    assert all(len(plan["blocks"]) <= 6 for plan in plans)
    assert all(plan["numeric_candidate_count"] <= 24 for plan in plans)
    assert sum(plan["numeric_candidate_count"] for plan in plans) == 30


def test_numeric_planner_filters_empty_batches_and_preserves_checkpoint_count() -> None:
    source = document()
    blocks = [source.blocks[0]] + [
        DocumentBlock(
            block_id=f"non_numeric_{index}",
            type="PARAGRAPH",
            order=index,
            raw_text=f"项目说明{index}。",
            normalized_text=f"项目说明{index}。",
            location=DocumentLocation(paragraph_index=index),
        )
        for index in range(1, 7)
    ]
    doc = source.model_copy(update={"blocks": blocks})

    all_plans = plan_numeric_document_batches(
        doc, max_numeric_units=6, include_empty=True
    )
    plans = plan_numeric_document_batches(doc, max_numeric_units=6)

    assert len(all_plans) == 2
    assert len(plans) == 1
    assert plans[0]["numeric_candidate_count"] > 0
    assert plans[0]["blocks"] == all_plans[0]["blocks"]
    assert plans[0]["checkpoint_planned_batch_count"] == len(all_plans)


def test_numeric_truncation_recovery_is_fixed_6_to_3_to_1() -> None:
    blocks = [
        DocumentBlock(
            block_id=f"recovery_{index}",
            type="PARAGRAPH",
            order=index,
            raw_text=f"金额为{index + 1}万元。",
            normalized_text=f"金额为{index + 1}万元。",
            location=DocumentLocation(paragraph_index=index),
        )
        for index in range(6)
    ]

    first = numeric_recovery_blocks(blocks, "LLM_OUTPUT_TRUNCATED")
    second = numeric_recovery_blocks(first[0], "LLM_OUTPUT_TRUNCATED")

    assert [len(group) for group in first] == [3, 3]
    assert [len(group) for group in second] == [1, 1, 1]
    assert numeric_recovery_blocks(blocks, "LLM_INVALID_JSON") == []


@pytest.mark.asyncio
async def test_numeric_leaf_truncation_splits_candidate_indexes_without_loss() -> None:
    block = DocumentBlock(
        block_id="dense_numeric_leaf",
        type="PARAGRAPH",
        order=0,
        raw_text="租金为1万元，首期租金为2万元，保证金为3万元，金额合计4万元。",
        normalized_text="租金为1万元，首期租金为2万元，保证金为3万元，金额合计4万元。",
        location=DocumentLocation(paragraph_index=0),
    )
    doc = document().model_copy(update={"blocks": [block]})

    class CandidateSplitLlm(WaveLlm):
        def __init__(self) -> None:
            super().__init__()
            self.candidate_counts: list[int] = []
            self.candidate_indexes: list[list[int]] = []

        async def extract_numeric_candidates(self, payload: dict) -> LlmResult:
            self.numeric_calls += 1
            indexes = numeric_candidate_indexes(payload)
            self.candidate_counts.append(len(indexes))
            self.candidate_indexes.append(indexes)
            if len(indexes) > 2:
                raise LlmClientError(
                    "LLM_OUTPUT_TRUNCATED",
                    "synthetic numeric truncation",
                    finish_reason="length",
                )
            return LlmResult(
                value={
                    "items": [
                        {
                            "candidate_index": index,
                            "semantic_key": "amount",
                            "display_name": "金额",
                            "value_type": "MONEY",
                            "decision": "IGNORE",
                            "reason_code": "SYNTHETIC",
                            "confidence": 0.9,
                        }
                        for index in indexes
                    ]
                },
                configured_model="candidate-split",
                actual_model="candidate-split",
                mock=False,
            )

    llm = CandidateSplitLlm()
    await extract_documents_with_independent_map_reduce(
        settings=Settings(
            _env_file=None,
            LLM_ENABLED=False,
            LLM_EXTRACTION_MAX_NUMERIC_UNITS=6,
        ),
        documents=[doc],
        llm=llm,  # type: ignore[arg-type]
    )

    assert llm.candidate_counts == [4, 2, 2]
    assert llm.candidate_indexes == [[1, 2, 3, 4], [1, 2], [3, 4]]
    assert set(llm.candidate_indexes[1]).isdisjoint(llm.candidate_indexes[2])


@pytest.mark.asyncio
async def test_numeric_single_candidate_truncation_is_terminal() -> None:
    llm_calls = 0

    class SingleCandidateTruncationLlm(WaveLlm):
        async def extract_numeric_candidates(self, payload: dict) -> LlmResult:
            nonlocal llm_calls
            llm_calls += 1
            assert len(payload["numeric_candidates"]) == 1
            raise LlmClientError(
                "LLM_OUTPUT_TRUNCATED",
                "synthetic numeric truncation",
                finish_reason="length",
            )

    with pytest.raises(WorkflowError) as caught:
        await extract_documents_with_independent_map_reduce(
            settings=Settings(_env_file=None, LLM_ENABLED=False),
            documents=[document().model_copy(update={"blocks": [document().blocks[0]]})],
            llm=SingleCandidateTruncationLlm(),  # type: ignore[arg-type]
        )

    assert llm_calls == 1
    assert caught.value.details["failure_code"] == "LLM_OUTPUT_TRUNCATED"
    assert caught.value.details["numeric_candidate_count"] == 1


@pytest.mark.parametrize(
    ("count", "expected_sizes"),
    [(16, [8, 8]), (8, [4, 4]), (5, [3, 2])],
)
def test_text_recovery_blocks_bisect_contiguously_without_loss(
    count: int, expected_sizes: list[int]
) -> None:
    blocks = [
        DocumentBlock(
            block_id=f"text_recovery_{index}",
            type="PARAGRAPH",
            order=index,
            raw_text=f"条款{index}",
            normalized_text=f"条款{index}",
            location=DocumentLocation(paragraph_index=index),
        )
        for index in range(count)
    ]

    groups = text_recovery_blocks(blocks)

    assert [len(group) for group in groups] == expected_sizes
    assert [block.block_id for group in groups for block in group] == [
        block.block_id for block in blocks
    ]


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
            value={"items": [], "has_more": False},
            configured_model="wave",
            actual_model="wave",
            mock=False,
        )


class CheckpointLlm(WaveLlm):
    def __init__(self, *, fail_text: bool = False) -> None:
        super().__init__()
        self.fail_text = fail_text

    async def extract_text_facts(self, payload: dict) -> LlmResult:
        self.text_calls += 1
        if self.fail_text:
            raise LlmClientError("LLM_INVALID_JSON", "synthetic text failure")
        return LlmResult(
            value={"items": [], "has_more": False},
            configured_model="checkpoint",
            actual_model="checkpoint",
            mock=False,
        )


@pytest.mark.asyncio
async def test_independent_extraction_never_calls_numeric_for_empty_batch() -> None:
    source = document()
    blocks = [source.blocks[0]] + [
        DocumentBlock(
            block_id=f"empty_numeric_{index}",
            type="PARAGRAPH",
            order=index,
            raw_text=f"项目说明{index}。",
            normalized_text=f"项目说明{index}。",
            location=DocumentLocation(paragraph_index=index),
        )
        for index in range(1, 7)
    ]
    doc = source.model_copy(update={"blocks": blocks})

    class RejectsEmptyNumeric(WaveLlm):
        async def extract_numeric_candidates(self, payload: dict) -> LlmResult:
            assert payload["numeric_candidates"]
            return await super().extract_numeric_candidates(payload)

    llm = RejectsEmptyNumeric()
    result, _ = await extract_documents_with_independent_map_reduce(
        settings=Settings(
            _env_file=None,
            LLM_ENABLED=False,
            LLM_EXTRACTION_MAX_NUMERIC_UNITS=6,
        ),
        documents=[doc],
        llm=llm,  # type: ignore[arg-type]
    )

    assert llm.numeric_calls == 1
    assert result["file_a"]["numeric_batch_count"] == 1


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
async def test_independent_text_failure_does_not_invalidate_numeric_checkpoint() -> None:
    store = InMemoryExtractionCheckpointStore()
    settings = Settings(_env_file=None, LLM_ENABLED=False)
    with pytest.raises(WorkflowError):
        await extract_documents_with_independent_map_reduce(
            settings=settings,
            documents=[document()],
            llm=CheckpointLlm(fail_text=True),  # type: ignore[arg-type]
            checkpoint_store=store,
            task_id="task_source",
        )

    retry = CheckpointLlm()
    result, _ = await extract_documents_with_independent_map_reduce(
        settings=settings,
        documents=[document()],
        llm=retry,  # type: ignore[arg-type]
        checkpoint_store=store,
        task_id="task_retry",
        source_task_id="task_source",
    )
    assert result["file_a"]["numeric_batch_count"] >= 1
    assert retry.profile_calls == 0
    assert retry.numeric_calls == 0
    assert retry.text_calls >= 1

    chained = CheckpointLlm(fail_text=True)
    await extract_documents_with_independent_map_reduce(
        settings=settings,
        documents=[document()],
        llm=chained,  # type: ignore[arg-type]
        checkpoint_store=store,
        task_id="task_chained_retry",
        source_task_id="task_retry",
    )
    assert chained.profile_calls == 0
    assert chained.numeric_calls == 0
    assert chained.text_calls == 0


@pytest.mark.asyncio
async def test_numeric_upstream_failure_does_not_split_or_schedule_remaining_batches() -> None:
    class UpstreamNumeric(WaveLlm):
        async def extract_numeric_candidates(self, payload: dict) -> LlmResult:
            self.numeric_calls += 1
            raise LlmClientError("LLM_UPSTREAM_ERROR", "synthetic upstream failure")

    llm = UpstreamNumeric()
    with pytest.raises(WorkflowError) as caught:
        await extract_documents_with_independent_map_reduce(
            settings=Settings(
                _env_file=None,
                LLM_ENABLED=False,
                LLM_EXTRACTION_MAX_NUMERIC_UNITS=1,
            ),
            documents=[document()],
            llm=llm,  # type: ignore[arg-type]
        )

    # The fixture produces two one-unit numeric batches.  The first transport
    # failure opens the circuit; the second batch is never sent and no text
    # chain is scheduled.
    assert caught.value.details["failure_code"] == "LLM_UPSTREAM_ERROR"
    assert llm.numeric_calls == 1
    assert llm.text_calls == 0


@pytest.mark.asyncio
async def test_independent_checkpoint_recovery_ignores_task_file_ids() -> None:
    source = document()
    retry = source.model_copy(
        update={
            "file_id": "file_retry",
            "blocks": [
                block.model_copy(update={"block_id": f"retry_{block.block_id}"})
                for block in source.blocks
            ],
        }
    )
    store = InMemoryExtractionCheckpointStore()

    class SourceFactLlm(CheckpointLlm):
        async def extract_text_facts(self, payload: dict) -> LlmResult:
            self.text_calls += 1
            items = []
            if "甲方" in payload["units"][0]["text"]:
                items = [
                    {
                        "unit_id": payload["units"][0]["unit_id"],
                        "semantic_key": "guarantor",
                        "display_name": "保证人",
                        "value_type": "ENTITY",
                        "quote": "甲方",
                        "confidence": 0.9,
                    }
                ]
            return LlmResult(
                value={"items": items, "has_more": False},
                configured_model="checkpoint",
                actual_model="checkpoint",
                mock=False,
            )

    source_llm = SourceFactLlm()

    await extract_documents_with_independent_map_reduce(
        settings=Settings(_env_file=None, LLM_ENABLED=False),
        documents=[source],
        llm=source_llm,  # type: ignore[arg-type]
        checkpoint_store=store,
        task_id="task_source_ids",
    )

    retry_llm = CheckpointLlm(fail_text=True)
    result, _ = await extract_documents_with_independent_map_reduce(
        settings=Settings(_env_file=None, LLM_ENABLED=False),
        documents=[retry],
        llm=retry_llm,  # type: ignore[arg-type]
        checkpoint_store=store,
        task_id="task_retry_ids",
        source_task_id="task_source_ids",
    )

    assert retry_llm.profile_calls == 0
    assert retry_llm.numeric_calls == 0
    assert retry_llm.text_calls == 0
    assert result["file_retry"]["value"]["profile"]["file_id"] == "file_retry"
    assert result["file_retry"]["value"]["facts"][0]["source_file_id"] == "file_retry"


def test_checkpoint_digest_excludes_task_and_display_identity() -> None:
    first = {
        "file_id": "file_source",
        "batch_id": "batch_source",
        "units": [
            {
                "unit_id": "unit_source",
                "text": "同一结构",
                "location": {"page": 1, "paragraph_index": 4},
            }
        ],
        "page_count": 46,
    }
    second = {
        "file_id": "file_retry",
        "batch_id": "batch_retry",
        "units": [
            {
                "unit_id": "unit_retry",
                "text": "同一结构",
                "location": {"page": 9, "paragraph_index": 4},
            }
        ],
        "page_count": 52,
    }

    assert _checkpoint_payload_digest(first) == _checkpoint_payload_digest(second)


def test_document_checkpoint_identity_excludes_page_and_file_id() -> None:
    source = document()
    retry = source.model_copy(
        update={
            "file_id": "file_retry",
            "blocks": [
                block.model_copy(
                    update={
                        "block_id": f"retry_{block.block_id}",
                        "location": block.location.model_copy(update={"page": 9}),
                    }
                )
                for block in source.blocks
            ],
        }
    )

    assert _document_checkpoint_identity_values(source, None) == (
        _document_checkpoint_identity_values(retry, None)
    )


def test_text_structure_split_prefers_sentence_and_clause_boundaries() -> None:
    block = document().blocks[0].model_copy(
        update={"raw_text": "第一句。第二句。", "normalized_text": "第一句。第二句。"}
    )

    children = _split_text_structure_unit(block)

    assert [child.raw_text for child in children] == ["第一句。", "第二句。"]


@pytest.mark.asyncio
async def test_document_checkpoint_reuse_skips_all_extraction_for_new_file_ids() -> None:
    source = document()
    retry = source.model_copy(
        update={
            "file_id": "file_retry_snapshot",
            "blocks": [
                block.model_copy(update={"block_id": f"retry_{block.block_id}"})
                for block in source.blocks
            ],
        }
    )
    store = InMemoryExtractionCheckpointStore()

    class SnapshotSourceLlm(WaveLlm):
        async def extract_text_facts(self, payload: dict) -> LlmResult:
            self.text_calls += 1
            items = []
            if "甲方" in payload["units"][0]["text"]:
                items = [
                    {
                        "unit_id": payload["units"][0]["unit_id"],
                        "semantic_key": "guarantor",
                        "display_name": "保证人",
                        "value_type": "ENTITY",
                        "quote": "甲方",
                        "confidence": 0.9,
                    }
                ]
            return LlmResult(
                value={"items": items, "has_more": False},
                configured_model="snapshot-source",
                actual_model="snapshot-source",
                mock=False,
            )

    source_llm = SnapshotSourceLlm()
    settings = Settings(_env_file=None, LLM_ENABLED=False)
    await extract_documents_with_independent_map_reduce(
        settings=settings,
        documents=[source],
        llm=source_llm,  # type: ignore[arg-type]
        checkpoint_store=store,
        task_id="snapshot_source",
    )

    assert any(
        item.extraction_version == DOCUMENT_EXTRACTION_CHECKPOINT_VERSION
        for item in store._records.values()
    )

    retry_llm = CheckpointLlm(fail_text=True)
    result, _ = await extract_documents_with_independent_map_reduce(
        settings=settings,
        documents=[retry],
        llm=retry_llm,  # type: ignore[arg-type]
        checkpoint_store=store,
        task_id="snapshot_retry",
        source_task_id="snapshot_source",
        source_file_ids_by_file_id={"file_retry_snapshot": source.file_id},
    )

    assert retry_llm.profile_calls == 0
    assert retry_llm.numeric_calls == 0
    assert retry_llm.text_calls == 0
    assert result["file_retry_snapshot"]["document_checkpoint_reused"] is True
    assert result["file_retry_snapshot"]["value"]["profile"]["file_id"] == (
        "file_retry_snapshot"
    )
    assert result["file_retry_snapshot"]["value"]["facts"][0]["source_file_id"] == (
        "file_retry_snapshot"
    )


@pytest.mark.asyncio
async def test_failed_reduce_does_not_save_document_checkpoint() -> None:
    store = InMemoryExtractionCheckpointStore()

    with pytest.raises(WorkflowError):
        await extract_documents_with_independent_map_reduce(
            settings=Settings(_env_file=None, LLM_ENABLED=False),
            documents=[document()],
            llm=CheckpointLlm(fail_text=True),  # type: ignore[arg-type]
            checkpoint_store=store,
            task_id="snapshot_failed",
        )

    assert not any(
        item.extraction_version == DOCUMENT_EXTRACTION_CHECKPOINT_VERSION
        for item in store._records.values()
    )


@pytest.mark.asyncio
async def test_document_snapshot_is_not_saved_when_the_extraction_round_fails() -> None:
    reference = document().model_copy(
        update={"file_id": "reference_first", "sha256": "b" * 64}
    )
    target = document().model_copy(
        update={
            "file_id": "target_later",
            "role": "TARGET",
            "sha256": "c" * 64,
            "blocks": [
                block.model_copy(update={"block_id": f"target_{block.block_id}"})
                for block in document().blocks
            ],
        }
    )
    store = InMemoryExtractionCheckpointStore()

    class TargetFailureLlm(WaveLlm):
        async def extract_text_facts(self, payload: dict) -> LlmResult:
            self.text_calls += 1
            if payload["file_id"] == target.file_id:
                raise LlmClientError("LLM_UPSTREAM_ERROR", "synthetic target failure")
            return LlmResult(
                value={"items": [], "has_more": False},
                configured_model="document-snapshot",
                actual_model="document-snapshot",
                mock=False,
            )

    with pytest.raises(WorkflowError):
        await extract_documents_with_independent_map_reduce(
            settings=Settings(_env_file=None, LLM_ENABLED=False),
            documents=[target, reference],
            llm=TargetFailureLlm(),  # type: ignore[arg-type]
            checkpoint_store=store,
            task_id="document_snapshot_partial_failure",
        )

    document_snapshots = [
        item
        for item in store._records.values()
        if item.extraction_version == DOCUMENT_EXTRACTION_CHECKPOINT_VERSION
    ]
    assert document_snapshots == []


@pytest.mark.asyncio
async def test_singleton_truncation_reduces_text_limit_without_same_payload_retry() -> None:
    block = document().blocks[1].model_copy(
        update={"raw_text": "项目名称为测试项目", "normalized_text": "项目名称为测试项目"}
    )
    doc = document().model_copy(update={"blocks": [block]})

    class LimitAwareTruncationLlm(WaveLlm):
        def __init__(self) -> None:
            super().__init__()
            self.limits: list[int] = []
            self.payloads: list[str] = []

        async def extract_text_facts(self, payload: dict) -> LlmResult:
            self.text_calls += 1
            self.limits.append(payload["requirements"]["max_items"])
            self.payloads.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            if payload["requirements"]["max_items"] > 3:
                raise LlmClientError(
                    "LLM_OUTPUT_TRUNCATED",
                    "synthetic truncation",
                    finish_reason="length",
                )
            return LlmResult(
            value={"items": [], "has_more": False},
            configured_model="truncation",
                actual_model="truncation",
                mock=False,
            )

    llm = LimitAwareTruncationLlm()
    result, _ = await extract_documents_with_independent_map_reduce(
        settings=Settings(_env_file=None, LLM_ENABLED=False),
        documents=[doc],
        llm=llm,  # type: ignore[arg-type]
    )

    assert llm.limits == [12, 6, 3]
    assert len(set(llm.payloads)) == len(llm.payloads)
    assert result["file_a"]["text_batch_count"] == 1


@pytest.mark.asyncio
async def test_multi_unit_truncation_bisects_without_exhausting_recovery_budget() -> None:
    blocks = [
        DocumentBlock(
            block_id=f"paragraph_{index}",
            type="PARAGRAPH",
            order=index,
            raw_text=f"测试资料条目甲乙丙丁第{chr(0x4E00 + index)}项",
            normalized_text=f"测试资料条目甲乙丙丁第{chr(0x4E00 + index)}项",
            location=DocumentLocation(paragraph_index=index),
        )
        for index in range(16)
    ]
    doc = document().model_copy(update={"role": "TARGET", "blocks": blocks})

    class SizeAwareTruncationLlm(WaveLlm):
        def __init__(self) -> None:
            super().__init__()
            self.unit_counts: list[int] = []

        async def extract_text_facts(self, payload: dict) -> LlmResult:
            self.text_calls += 1
            unit_count = len(payload["units"])
            self.unit_counts.append(unit_count)
            if unit_count > 4:
                raise LlmClientError(
                    "LLM_OUTPUT_TRUNCATED",
                    "synthetic truncation",
                    finish_reason="length",
                )
            return LlmResult(
                value={"items": [], "has_more": False},
                configured_model="truncation",
                actual_model="truncation",
                mock=False,
            )

    llm = SizeAwareTruncationLlm()
    result, _ = await extract_documents_with_independent_map_reduce(
        settings=Settings(
            _env_file=None,
            LLM_ENABLED=False,
            LLM_EXTRACTION_MAX_TEXT_UNITS=16,
            LLM_EXTRACTION_WAVE_SIZE=6,
        ),
        documents=[doc],
        llm=llm,  # type: ignore[arg-type]
    )

    assert llm.unit_counts == [16, 8, 8, 4, 4, 4, 4]
    assert max(llm.unit_counts) == 16
    assert result["file_a"]["recovery_count"] == 3


@pytest.mark.asyncio
async def test_text_saturation_splits_do_not_consume_error_recovery_budget() -> None:
    blocks = [
        DocumentBlock(
            block_id=f"saturated_{index}",
            type="PARAGRAPH",
            order=index,
            raw_text=f"密集业务资料第{index + 1}项包含可核验文本内容",
            normalized_text=f"密集业务资料第{index + 1}项包含可核验文本内容",
            location=DocumentLocation(paragraph_index=index),
        )
        for index in range(32)
    ]
    doc = document().model_copy(update={"role": "TARGET", "blocks": blocks})

    class SaturationAwareLlm(WaveLlm):
        def __init__(self) -> None:
            super().__init__()
            self.unit_counts: list[int] = []

        async def extract_text_facts(self, payload: dict) -> LlmResult:
            self.text_calls += 1
            unit_count = len(payload["units"])
            self.unit_counts.append(unit_count)
            if unit_count > 4:
                raise LlmClientError(
                    "LLM_EXTRACTION_EVIDENCE_INVALID",
                    "synthetic saturation",
                    failure_code="FACT_BATCH_SATURATED",
                )
            return LlmResult(
                value={"items": [], "has_more": False},
                configured_model="saturation",
                actual_model="saturation",
                mock=False,
            )

    llm = SaturationAwareLlm()
    result, _ = await extract_documents_with_independent_map_reduce(
        settings=Settings(
            _env_file=None,
            LLM_ENABLED=False,
            LLM_EXTRACTION_MAX_TEXT_UNITS=8,
            LLM_EXTRACTION_WAVE_SIZE=8,
        ),
        documents=[doc],
        llm=llm,  # type: ignore[arg-type]
    )

    assert llm.unit_counts.count(8) == 4
    assert llm.unit_counts.count(4) == 8
    assert result["file_a"]["recovery_count"] == 4


@pytest.mark.asyncio
async def test_text_recovery_budget_error_keeps_outer_and_underlying_codes() -> None:
    blocks = [
        DocumentBlock(
            block_id=f"budget_{index}",
            type="PARAGRAPH",
            order=index,
            raw_text=f"条款{chr(0x4E00 + index)}项",
            normalized_text=f"条款{chr(0x4E00 + index)}项",
            location=DocumentLocation(paragraph_index=index),
        )
        for index in range(32)
    ]
    doc = document().model_copy(update={"blocks": blocks})

    class AlwaysInvalidText(WaveLlm):
        async def extract_text_facts(self, payload: dict) -> LlmResult:
            self.text_calls += 1
            raise LlmClientError("LLM_INVALID_JSON", "synthetic invalid JSON")

    with pytest.raises(WorkflowError) as caught:
        await extract_documents_with_independent_map_reduce(
            settings=Settings(
                _env_file=None,
                LLM_ENABLED=False,
                LLM_EXTRACTION_MAX_TEXT_UNITS=16,
                LLM_EXTRACTION_WAVE_SIZE=6,
            ),
            documents=[doc],
            llm=AlwaysInvalidText(),  # type: ignore[arg-type]
        )

    assert caught.value.details["failure_code"] == "TEXT_RECOVERY_BUDGET_EXHAUSTED"
    assert caught.value.details["underlying_failure_code"] == "LLM_INVALID_JSON"
    assert caught.value.details["chain"] == "text"


@pytest.mark.asyncio
async def test_singleton_truncation_at_three_is_safe_terminal_failure() -> None:
    block = document().blocks[1].model_copy(
        update={"raw_text": "项目名称为测试项目", "normalized_text": "项目名称为测试项目"}
    )
    doc = document().model_copy(update={"blocks": [block]})
    limits: list[int] = []

    class AlwaysTruncated(WaveLlm):
        async def extract_text_facts(self, payload: dict) -> LlmResult:
            self.text_calls += 1
            limits.append(payload["requirements"]["max_items"])
            raise LlmClientError(
                "LLM_OUTPUT_TRUNCATED",
                "synthetic truncation",
                finish_reason="length",
            )

    with pytest.raises(WorkflowError) as caught:
        await extract_documents_with_independent_map_reduce(
            settings=Settings(_env_file=None, LLM_ENABLED=False),
            documents=[doc],
            llm=AlwaysTruncated(),  # type: ignore[arg-type]
        )

    assert limits == [12, 6, 3]
    assert 1 not in limits
    assert caught.value.details["failure_code"] == "LLM_OUTPUT_TRUNCATED"


@pytest.mark.asyncio
async def test_table_truncation_splits_cells_before_reducing_fact_limit() -> None:
    cells = [
        TableCell(
            raw_text=value,
            normalized_text=value,
            location=DocumentLocation(table_index=0, row=0, column=column),
        )
        for column, value in enumerate(("字段", "甲方", "备注"))
    ]
    table = ParsedTable(table_index=0, rows=[TableRow(row=0, cells=cells)])
    doc = document().model_copy(
        update={
            "blocks": [
                DocumentBlock(
                    block_id="truncated_table",
                    type="TABLE",
                    order=0,
                    raw_text="\t".join(cell.raw_text for cell in cells),
                    normalized_text="\t".join(cell.raw_text for cell in cells),
                    location=DocumentLocation(table_index=0),
                    table=table,
                )
            ]
        }
    )

    class TableTruncationLlm(WaveLlm):
        def __init__(self) -> None:
            super().__init__()
            self.payloads: list[dict] = []

        async def extract_text_facts(self, payload: dict) -> LlmResult:
            self.text_calls += 1
            self.payloads.append(payload)
            if "\t" in payload["units"][0]["text"]:
                raise LlmClientError(
                    "LLM_OUTPUT_TRUNCATED",
                    "synthetic truncation",
                    finish_reason="length",
                )
            return LlmResult(
                value={"items": [], "has_more": False},
                configured_model="table-truncation",
                actual_model="table-truncation",
                mock=False,
            )

    llm = TableTruncationLlm()
    await extract_documents_with_independent_map_reduce(
        settings=Settings(_env_file=None, LLM_ENABLED=False),
        documents=[doc],
        llm=llm,  # type: ignore[arg-type]
    )

    assert llm.text_calls == 4
    assert "\t" in llm.payloads[0]["units"][0]["text"]
    assert all("\t" not in payload["units"][0]["text"] for payload in llm.payloads[1:])


@pytest.mark.asyncio
async def test_independent_checkpoint_recovery_reads_legacy_source_identity() -> None:
    def with_file_id(document: ParsedDocument, file_id: str) -> ParsedDocument:
        return document.model_copy(
            update={
                "file_id": file_id,
                "blocks": [
                    block.model_copy(
                        update={"block_id": f"{file_id}_{block.block_id}"}
                    )
                    for block in document.blocks
                ],
            }
        )

    source = with_file_id(document(), "file_source")
    retry = with_file_id(document(), "file_retry")
    settings = Settings(
        _env_file=None,
        LLM_ENABLED=False,
        LLM_EXTRACTION_MAX_TEXT_UNITS=1,
    )
    store = InMemoryExtractionCheckpointStore()
    source_task_id = "task_legacy_source"

    profile_payload = build_document_overview_payload(source)
    profile_payload.update(
        {
            "batch_id": stable_batch_id(source.sha256, source.blocks, "profile-v2"),
            "extraction_version": "profile-v2",
        }
    )
    await store.save(
        ExtractionCheckpoint(
            task_id=source_task_id,
            file_sha256=source.sha256,
            batch_id=profile_payload["batch_id"],
            extraction_version="profile-v2",
            payload_digest=_payload_digest(profile_payload),
            status="SUCCEEDED",
            value={
                "profile": {
                    "file_id": source.file_id,
                    "document_kind": "合成资料",
                    "title": None,
                    "confidence": 0.9,
                    "evidence_locations": [{"paragraph_index": 0}],
                },
                "facts": [],
                "missing_field_keys": [],
            },
        )
    )

    for _chain, plans in (
        (
            "numeric",
            plan_numeric_document_batches(
                source,
                max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
                max_numeric_candidates=24,
                estimated_output_token_limit=2000,
            ),
        ),
        (
            "text",
            plan_text_document_batches(
                source,
                max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
                max_text_units=1,
                estimated_output_token_limit=2000,
            ),
        ),
    ):
        for plan in plans:
            plan["payload"].update(
                {
                    "batch_depth": 0,
                    "parent_batch_id": None,
                    "planned_batch_count": len(plans),
                    "extraction_version": plan["extraction_version"],
                }
            )
            await store.save(
                ExtractionCheckpoint(
                    task_id=source_task_id,
                    file_sha256=source.sha256,
                    batch_id=plan["batch_id"],
                    extraction_version=plan["extraction_version"],
                    payload_digest=_payload_digest(plan["payload"]),
                    status="SUCCEEDED",
                    value={"facts": []},
                )
            )

    retry_llm = CheckpointLlm(fail_text=True)
    result, _ = await extract_documents_with_independent_map_reduce(
        settings=settings,
        documents=[retry],
        llm=retry_llm,  # type: ignore[arg-type]
        checkpoint_store=store,
        task_id="task_legacy_retry",
        source_task_id=source_task_id,
        source_file_ids_by_file_id={"file_retry": "file_source"},
    )

    assert retry_llm.profile_calls == 0
    assert retry_llm.numeric_calls == 0
    assert retry_llm.text_calls == 0
    assert result["file_retry"]["value"]["profile"]["file_id"] == "file_retry"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_factory", "expected_code", "expected_depth", "expected_unit_count"),
    [
        (
            lambda: LlmClientError("LLM_INVALID_JSON", "safe failure"),
            "LLM_INVALID_JSON",
            1,
            1,
        ),
        (
            lambda: LlmClientError(
                "LLM_SCHEMA_INVALID",
                "safe failure",
                failure_code="LLM_RESPONSE_SCHEMA_INVALID",
            ),
            "LLM_RESPONSE_SCHEMA_INVALID",
            0,
            2,
        ),
        (
            lambda: LlmClientError(
                "LLM_EXTRACTION_EVIDENCE_INVALID",
                "safe failure",
                failure_code="FACT_QUOTE_NOT_GROUNDED",
            ),
            "FACT_QUOTE_NOT_GROUNDED",
            1,
            1,
        ),
        (
            lambda: LlmClientError(
                "LLM_EXTRACTION_EVIDENCE_INVALID",
                "safe failure",
                failure_code="FACT_VALUE_NOT_GROUNDED",
            ),
            "FACT_VALUE_NOT_GROUNDED",
            1,
            1,
        ),
        (
            lambda: LlmClientError("LLM_TIMEOUT", "safe failure"),
            "LLM_TIMEOUT",
            0,
            2,
        ),
        (
            lambda: LlmClientError("LLM_UPSTREAM_ERROR", "safe failure"),
            "LLM_UPSTREAM_ERROR",
            0,
            2,
        ),
        (
            lambda: EvidenceValidationError(
                "unsafe evidence body", code="FACT_QUOTE_NOT_GROUNDED"
            ),
            "FACT_QUOTE_NOT_GROUNDED",
            1,
            1,
        ),
        (
            lambda: WorkflowError(
                "LLM_EXTRACTION_FAILED",
                "unsafe workflow body",
                details={"failure_code": "LLM_UPSTREAM_ERROR"},
            ),
            "LLM_UPSTREAM_ERROR",
            0,
            2,
        ),
    ],
)
async def test_independent_text_terminal_failure_preserves_safe_context(
    error_factory,
    expected_code: str,
    expected_depth: int,
    expected_unit_count: int,
) -> None:
    class FailingText(CheckpointLlm):
        async def extract_text_facts(self, payload: dict) -> LlmResult:
            self.text_calls += 1
            raise error_factory()

    with pytest.raises(WorkflowError) as caught:
        await extract_documents_with_independent_map_reduce(
            settings=Settings(_env_file=None, LLM_ENABLED=False),
            documents=[document()],
            llm=FailingText(),  # type: ignore[arg-type]
        )

    assert caught.value.code == "DYNAMIC_CHECK_INCOMPLETE"
    assert caught.value.details == {
        "failure_stage": "FACT_EXTRACTION",
        "chain": "text",
        "file": "file_a",
        "file_id": "file_a",
        "batch_depth": expected_depth,
        "unit_count": expected_unit_count,
        "batch_id": caught.value.details["batch_id"],
        "failure_code": expected_code,
    }
    assert caught.value.details["batch_id"].startswith("batch_")
    assert "safe failure" not in str(caught.value.details)
    assert "保证人为甲方" not in str(caught.value.details)


@pytest.mark.asyncio
async def test_independent_checkpoint_failure_preserves_safe_context() -> None:
    class BrokenCheckpointStore:
        async def load(self, batch_id: str, **kwargs):
            if kwargs.get("extraction_version") == "profile-v2":
                return None
            raise EvidenceValidationError(
                "checkpoint evidence is not usable",
                code="FACT_QUOTE_NOT_GROUNDED",
            )

        async def save(self, checkpoint) -> None:
            return None

    with pytest.raises(WorkflowError) as caught:
        await extract_documents_with_independent_map_reduce(
            settings=Settings(_env_file=None, LLM_ENABLED=False),
            documents=[document()],
            llm=CheckpointLlm(),  # type: ignore[arg-type]
            checkpoint_store=BrokenCheckpointStore(),  # type: ignore[arg-type]
            task_id="task_checkpoint_failure",
        )

    assert caught.value.code == "DYNAMIC_CHECK_INCOMPLETE"
    assert caught.value.details["failure_stage"] == "FACT_EXTRACTION"
    assert caught.value.details["chain"] in {"numeric", "text"}
    assert caught.value.details["file"] == "file_a"
    assert caught.value.details["file_id"] == "file_a"
    assert caught.value.details["unit_count"] == 2
    assert caught.value.details["failure_code"] == "FACT_QUOTE_NOT_GROUNDED"
    assert caught.value.details["batch_id"].startswith("batch_")


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
