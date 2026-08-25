import json

import pytest

from app.adapters.llm.schemas import (
    DocumentFactExtraction,
    FactCandidate,
    FactReview,
    SemanticConcept,
    SemanticPlanResponse,
    ValidationSpec,
)
from app.documents.models import (
    DocumentBlock,
    DocumentLocation,
    ParsedDocument,
    ParsedTable,
    TableCell,
    TableRow,
)
from app.draft_review.facts import (
    EvidenceValidationError,
    accepted_fact_refs,
    build_fact_index,
    build_fact_matrix,
    build_fact_review_batches,
    chunk_document,
    fact_conflict_diff_items,
    fact_matrix_result_items,
    merge_chunk_extractions,
    merge_fact_review_batches,
    normalize_fact,
    stable_fact_id,
    validate_extraction_evidence,
    validate_semantic_plan,
)


def parsed(file_id: str, text: str) -> ParsedDocument:
    return ParsedDocument(
        file_id=file_id,
        role="REFERENCE",
        file_name="reference.docx",
        sha256="a" * 64,
        page_count=None,
        parser_name="python-docx",
        blocks=[
            DocumentBlock(
                block_id=f"{file_id}_p000000",
                type="PARAGRAPH",
                order=0,
                raw_text=text,
                normalized_text=text,
                location=DocumentLocation(paragraph_index=0),
            )
        ],
    )


def extraction(
    file_id: str,
    amount: str,
    *,
    missing: list[str] | None = None,
    field_key: str = "financing_amount",
):
    return DocumentFactExtraction.model_validate(
        {
            "profile": {
                "file_id": file_id,
                "document_kind": "项目资料",
                "title": None,
                "confidence": 0.9,
                "evidence_locations": [{"paragraph_index": 0}],
            },
            "facts": [
                {
                    "field_key": field_key,
                    "display_name": "融资金额",
                    "value_type": "MONEY",
                    "raw_value": amount,
                    "normalized_hint": None,
                    "source_file_id": file_id,
                    "evidence_text": f"融资金额为{amount}",
                    "location": {"paragraph_index": 0},
                    "confidence": 0.9,
                }
            ],
            "missing_field_keys": missing or [],
        }
    )


def test_document_chunks_respect_boundaries_without_losing_large_blocks() -> None:
    document = parsed("fil_a", "a" * 1200)
    document.blocks.append(
        DocumentBlock(
            block_id="second",
            type="PARAGRAPH",
            order=1,
            raw_text="b" * 900,
            normalized_text="b" * 900,
            location=DocumentLocation(paragraph_index=1),
        )
    )
    chunks = chunk_document(document, 1500)
    assert [len(chunk) for chunk in chunks] == [1, 1]


def test_evidence_must_exist_at_declared_location() -> None:
    document = parsed("fil_a", "融资金额为1000万元")
    valid = extraction("fil_a", "1000万元")
    validate_extraction_evidence(document, valid)

    invalid = extraction("fil_a", "2000万元")
    with pytest.raises(EvidenceValidationError):
        validate_extraction_evidence(document, invalid)


def _accepted_review(extracted: DocumentFactExtraction, **overrides: object) -> FactReview:
    fact = extracted.facts[0]
    return FactReview.model_validate(
        {
            "file_id": extracted.profile.file_id,
            "decisions": [
                {
                    "field_key": fact.field_key,
                    "source_file_id": fact.source_file_id,
                    "location": fact.location.model_dump(mode="json", exclude_none=True),
                    "decision": "ACCEPT",
                    "evidence_text": fact.evidence_text,
                    "confidence": 0.95,
                    "reason_code": "GROUNDED",
                }
            ],
            "confidence": 0.95,
            "evidence_complete": True,
            **overrides,
        }
    )


def test_fact_id_is_stable_and_duplicate_chunks_merge() -> None:
    first = extraction("fil_a", "1000万元")
    duplicate = first.facts[0].model_copy(update={"confidence": 0.99})
    first.facts.append(duplicate)

    index = build_fact_index({"fil_a": first})
    fact_id = stable_fact_id(first.facts[0])

    assert stable_fact_id(first.facts[0].model_copy()) == fact_id
    assert stable_fact_id(first.facts[0].model_copy(update={"raw_value": "2000万元"})) != fact_id
    assert list(index) == [(fact_id, "fil_a")]
    assert index[(fact_id, "fil_a")].fact.confidence == 0.99


@pytest.mark.parametrize(
    ("review", "expected"),
    [
        ("accepted", True),
        ("rejected", False),
        ("incomplete", False),
        ("low_confidence", False),
        ("wrong_identity", False),
    ],
)
def test_only_fully_verified_accept_facts_enter_semantic_plan(
    review: str, expected: bool
) -> None:
    extracted = extraction("fil_a", "1000万元")
    value: FactReview
    if review == "accepted":
        value = _accepted_review(extracted)
    elif review == "rejected":
        value = _accepted_review(extracted)
        value.decisions[0].decision = "REJECT"
    elif review == "incomplete":
        value = _accepted_review(extracted, evidence_complete=False)
    elif review == "low_confidence":
        value = _accepted_review(extracted, confidence=0.5)
    else:
        value = _accepted_review(extracted)
        value.decisions[0].source_file_id = "fil_other"
    refs = accepted_fact_refs(extracted, value, 0.8)
    assert bool(refs) is expected


def test_cross_document_conflict_diff_keeps_both_source_locations() -> None:
    target = extraction("fil_target", "1000万元", field_key="contract_amount")
    reference = extraction("fil_reference", "1200万元", field_key="approved_amount")
    matrix = build_fact_matrix(
        {"fil_target": target, "fil_reference": reference},
        target_file_id="fil_target",
        reference_file_ids=["fil_reference"],
        mapping_records=[
            {
                "target_fact_id": "target_fact_000001",
                "source_file_id": "fil_reference",
                "reference_field_key": "approved_amount",
                "reference_location": {"paragraph_index": 0},
                "status": "ACCEPT",
            }
        ],
    )
    diffs = fact_conflict_diff_items(matrix, target_file_id="fil_target")

    assert len(diffs) == 1
    assert diffs[0].baseline is not None and diffs[0].baseline.file_id == "fil_reference"
    assert diffs[0].target is not None and diffs[0].target.file_id == "fil_target"
    assert diffs[0].baseline.location.paragraph_index == 0
    assert diffs[0].target.location.paragraph_index == 0


def test_semantic_plan_validates_cross_file_evidence_and_rejects_old_ast() -> None:
    target = extraction("fil_target", "1000万元", field_key="contract_amount")
    reference = extraction("fil_reference", "1200万元", field_key="approved_amount")
    target_document = parsed("fil_target", "融资金额为1000万元")
    reference_document = parsed("fil_reference", "融资金额为1200万元")
    index = build_fact_index({"fil_target": target, "fil_reference": reference})
    target_ref = (stable_fact_id(target.facts[0]), "fil_target")
    reference_ref = (stable_fact_id(reference.facts[0]), "fil_reference")
    accepted = {target_ref, reference_ref}
    plan = SemanticPlanResponse.model_validate(
        {
            "file_id": "fil_target",
            "semantic_concepts": [
                {
                    "concept_id": "approved_amount",
                    "display_name": "金额",
                    "value_type": "MONEY",
                    "fact_refs": [
                        {"fact_id": target_ref[0], "source_file_id": target_ref[1]},
                        {"fact_id": reference_ref[0], "source_file_id": reference_ref[1]},
                    ],
                    "evidence_refs": [
                        {
                            "source_file_id": "fil_target",
                            "location": {"paragraph_index": 0},
                        },
                        {
                            "source_file_id": "fil_reference",
                            "location": {"paragraph_index": 0},
                        },
                    ],
                    "confidence": 0.9,
                }
            ],
            "validation_specs": [
                {
                    "validation_id": "target_amount_positive",
                    "display_name": "目标金额为正",
                    "expression": {
                        "op": "greater_than",
                        "left": {
                            "op": "fact",
                            "fact_id": target_ref[0],
                            "source_file_id": target_ref[1],
                        },
                        "right": {"op": "literal", "value": "0"},
                    },
                    "evidence_refs": [
                        {
                            "source_file_id": "fil_target",
                            "location": {"paragraph_index": 0},
                        }
                    ],
                    "confidence": 0.9,
                }
            ],
        }
    )
    validate_semantic_plan(
        primary_file_id="fil_target",
        documents_by_file={
            "fil_target": target_document,
            "fil_reference": reference_document,
        },
        plan=plan,
        fact_index=index,
        accepted_refs=accepted,
    )

    old_ast = plan.model_copy(
        update={
            "validation_specs": [
                plan.validation_specs[0].model_copy(
                    update={
                        "expression": {
                            "op": "greater_than",
                            "left": {"op": "fact", "concept_id": "amount"},
                            "right": {"op": "literal", "value": "0"},
                        }
                    }
                )
            ]
        }
    )
    with pytest.raises(EvidenceValidationError, match="qualified"):
        validate_semantic_plan(
            primary_file_id="fil_target",
            documents_by_file={
                "fil_target": target_document,
                "fil_reference": reference_document,
            },
            plan=old_ast,
            fact_index=index,
            accepted_refs=accepted,
        )


@pytest.mark.parametrize(
    ("value_type", "raw", "expected"),
    [
        ("MONEY", "1,000万元", "MONEY:1E+7"),
        ("PERCENTAGE", "5.20%", "PERCENTAGE:5.2%"),
        ("DURATION", "2年", "DURATION:24M"),
        ("DATE", "2026年8月21日", "DATE:2026-08-21"),
    ],
)
def test_deterministic_fact_normalization(value_type: str, raw: str, expected: str) -> None:
    fact = FactCandidate(
        field_key="test_field",
        display_name="测试",
        value_type=value_type,
        raw_value=raw,
        source_file_id="fil_a",
        evidence_text=raw,
        location=DocumentLocation(paragraph_index=0),
        confidence=1,
    )
    assert normalize_fact(fact) == expected


def test_matrix_conflict_becomes_risk_while_not_mentioned_is_not_a_risk() -> None:
    matrix = build_fact_matrix(
        {
            "fil_a": extraction("fil_a", "1000万元"),
            "fil_b": extraction("fil_b", "2000万元"),
        }
    )
    assert matrix[0]["status"] == "CONFLICT"
    risks, reviews, passed = fact_matrix_result_items(matrix)
    assert risks[0]["change_type"] == "SOURCE_CONFLICT"
    assert reviews == [] and passed == []

    matrix = build_fact_matrix({"fil_a": extraction("fil_a", "1000万元")})
    assert matrix[0]["status"] == "MISSING"
    risks, reviews, _passed = fact_matrix_result_items(matrix)
    assert risks == [] and reviews == []


def test_matrix_can_be_limited_to_consensus_candidates() -> None:
    first = extraction("fil_a", "1000万元")
    second = extraction("fil_b", "2000万元")
    first_fact = first.facts[0]
    matrix = build_fact_matrix(
        {"fil_a": first, "fil_b": second},
        consensus_fields={
            (first_fact.field_key, first_fact.source_file_id, (None, 0, None, None, None))
        },
    )
    assert matrix[0]["status"] == "UNCERTAIN"
    assert len(matrix[0]["candidates"]) == 2
    assert matrix[0]["reference_results"][0]["status"] == "UNCERTAIN"


def test_target_centric_mapping_accepts_different_field_keys_and_ignores_reference_only() -> None:
    target = extraction("fil_target", "1000万元", field_key="contract_amount")
    reference = extraction("fil_reference", "10000000元", field_key="approved_limit")
    reference.facts.append(
        reference.facts[0].model_copy(
            update={"field_key": "reference_only_count", "raw_value": "5"}
        )
    )
    matrix = build_fact_matrix(
        {"fil_target": target, "fil_reference": reference},
        target_file_id="fil_target",
        reference_file_ids=["fil_reference"],
        mapping_records=[
            {
                "target_fact_id": "target_fact_000001",
                "source_file_id": "fil_reference",
                "reference_field_key": "approved_limit",
                "reference_location": {"paragraph_index": 0},
                "status": "ACCEPT",
            }
        ],
    )
    assert len(matrix) == 1
    assert matrix[0]["field_key"] == "contract_amount"
    assert matrix[0]["status"] == "CONSISTENT"


def test_currency_or_mapping_uncertainty_never_becomes_conflict() -> None:
    target = extraction("fil_target", "人民币100元")
    reference = extraction("fil_reference", "100美元")
    matrix = build_fact_matrix(
        {"fil_target": target, "fil_reference": reference},
        target_file_id="fil_target",
        reference_file_ids=["fil_reference"],
        mapping_records=[
            {
                "target_fact_id": "target_fact_000001",
                "source_file_id": "fil_reference",
                "reference_field_key": "financing_amount",
                "reference_location": {"paragraph_index": 0},
                "status": "ACCEPT",
            }
        ],
    )
    assert matrix[0]["status"] == "UNCERTAIN"
    risks, reviews, _passed = fact_matrix_result_items(matrix)
    assert risks == [] and reviews[0]["reason_code"] == "FACT_UNCERTAIN"


def test_reliably_required_but_missing_source_becomes_deletion_risk() -> None:
    target = extraction("fil_target", "1000万元")
    matrix = build_fact_matrix(
        {"fil_target": target, "fil_reference": extraction("fil_reference", "5")},
        target_file_id="fil_target",
        reference_file_ids=["fil_reference"],
        mapping_records=[],
        required_missing={("target_fact_000001", "fil_reference")},
    )
    assert matrix[0]["status"] == "MISSING"
    risks, reviews, passed = fact_matrix_result_items(matrix)
    assert reviews == [] and passed == []
    assert risks[0]["risk_type"] == "DELETION_OR_MISSING"
    assert risks[0]["change_type"] == "REQUIRED_SOURCE_MISSING"


def test_required_missing_source_prevents_pass_for_same_fact() -> None:
    target = extraction("fil_target", "1000万元")
    matching = extraction("fil_matching", "10000000元")
    matrix = build_fact_matrix(
        {
            "fil_target": target,
            "fil_matching": matching,
            "fil_missing": extraction("fil_missing", "5", field_key="other_fact"),
        },
        target_file_id="fil_target",
        reference_file_ids=["fil_matching", "fil_missing"],
        mapping_records=[
            {
                "target_fact_id": "target_fact_000001",
                "source_file_id": "fil_matching",
                "reference_field_key": "financing_amount",
                "reference_location": {"paragraph_index": 0},
                "status": "ACCEPT",
            }
        ],
        required_missing={("target_fact_000001", "fil_missing")},
    )

    risks, reviews, passed = fact_matrix_result_items(matrix)
    assert risks[0]["change_type"] == "REQUIRED_SOURCE_MISSING"
    assert reviews == []
    assert passed == []


def test_chunk_merge_preserves_concepts_and_validation_specs() -> None:
    document = parsed("fil_a", "融资金额为1000万元")
    first = extraction("fil_a", "1000万元")
    first.semantic_concepts = [
        SemanticConcept.model_validate(
            {
            "concept_id": "financing_amount",
            "display_name": "融资金额",
            "value_type": "MONEY",
            "aliases": [],
            "evidence_locations": [{"paragraph_index": 0}],
            "confidence": 0.9,
            }
        )
    ]
    first.validation_specs = [
        ValidationSpec.model_validate(
            {
            "validation_id": "amount_positive",
            "display_name": "融资金额为正数",
            "expression": {
                "op": "greater_than",
                "left": {"op": "fact", "concept_id": "financing_amount"},
                "right": {"op": "literal", "value": "0"},
            },
            "evidence_locations": [{"paragraph_index": 0}],
            "confidence": 0.9,
            }
        )
    ]
    merged = merge_chunk_extractions(document, [first])
    assert [item.concept_id for item in merged.semantic_concepts] == ["financing_amount"]
    assert [item.validation_id for item in merged.validation_specs] == ["amount_positive"]


def test_review_batches_use_only_evidence_blocks_and_neighbors() -> None:
    document = parsed("fil_a", "前文")
    document.blocks.extend(
        DocumentBlock(
            block_id=f"fil_a_p{index:06d}",
            type="PARAGRAPH",
            order=index,
            raw_text=text,
            normalized_text=text,
            location=DocumentLocation(paragraph_index=index),
        )
        for index, text in enumerate(
            ["", "融资金额为1000万元", "中间说明", "租赁期限24个月", "后文"],
            start=1,
        )
    )
    extracted = extraction("fil_a", "1000万元")
    extracted.facts[0].location = DocumentLocation(paragraph_index=2)
    extracted.facts[0].evidence_text = "融资金额为1000万元"
    extracted.facts.append(
        FactCandidate(
            field_key="lease_term",
            display_name="租赁期限",
            value_type="DURATION",
            raw_value="24个月",
            source_file_id="fil_a",
            evidence_text="租赁期限24个月",
            location=DocumentLocation(paragraph_index=4),
            confidence=0.9,
        )
    )
    full = build_fact_review_batches(
        document, extracted, max_chars=100000, context_blocks=1
    )[0]
    first_only = extracted.model_copy(update={"facts": [extracted.facts[0]]})
    second_only = extracted.model_copy(update={"facts": [extracted.facts[1]]})
    single_sizes = [
        len(
            json.dumps(
                build_fact_review_batches(
                    document, item, max_chars=100000, context_blocks=1
                )[0],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        for item in (first_only, second_only)
    ]
    full_size = len(json.dumps(full, ensure_ascii=False, separators=(",", ":")))
    batches = build_fact_review_batches(
        document,
        extracted,
        max_chars=max(single_sizes) + 1,
        context_blocks=1,
    )

    assert full_size > max(single_sizes)
    assert len(batches) == 2
    assert [block["location"]["paragraph_index"] for block in batches[0]["blocks"]] == [
        1,
        2,
        3,
    ]
    assert [block["location"]["paragraph_index"] for block in batches[1]["blocks"]] == [
        3,
        4,
        5,
    ]


def test_review_batch_resolves_table_cell_to_parent_table_block() -> None:
    location = DocumentLocation(table_index=0, row=0, column=0)
    table = ParsedTable(
        table_index=0,
        rows=[
            TableRow(
                row=0,
                cells=[
                    TableCell(
                        raw_text="融资金额1000万元",
                        normalized_text="融资金额1000万元",
                        location=location,
                    )
                ],
            )
        ],
    )
    document = ParsedDocument(
        file_id="fil_table",
        role="REFERENCE",
        file_name="table.docx",
        sha256="a" * 64,
        page_count=None,
        parser_name="python-docx",
        blocks=[
            DocumentBlock(
                block_id="before",
                type="PARAGRAPH",
                order=0,
                raw_text="前文",
                normalized_text="前文",
                location=DocumentLocation(paragraph_index=0),
            ),
            DocumentBlock(
                block_id="table",
                type="TABLE",
                order=1,
                raw_text="融资金额1000万元",
                normalized_text="融资金额1000万元",
                location=DocumentLocation(table_index=0),
                table=table,
            ),
            DocumentBlock(
                block_id="after",
                type="PARAGRAPH",
                order=2,
                raw_text="后文",
                normalized_text="后文",
                location=DocumentLocation(paragraph_index=1),
            ),
        ],
    )
    extracted = DocumentFactExtraction.model_validate(
        {
            "profile": {
                "file_id": "fil_table",
                "document_kind": "项目资料",
                "confidence": 0.9,
                "evidence_locations": [{"table_index": 0}],
            },
            "facts": [
                {
                    "field_key": "financing_amount",
                    "display_name": "融资金额",
                    "value_type": "MONEY",
                    "raw_value": "1000万元",
                    "source_file_id": "fil_table",
                    "evidence_text": "融资金额1000万元",
                    "location": {"table_index": 0, "row": 0, "column": 0},
                    "confidence": 0.9,
                }
            ],
        }
    )

    batch = build_fact_review_batches(
        document, extracted, max_chars=100000, context_blocks=1
    )[0]

    assert [block["block_id"] for block in batch["blocks"]] == ["before", "table", "after"]


def test_review_batch_merge_requires_exact_decision_coverage() -> None:
    document = parsed("fil_a", "融资金额为1000万元")
    extracted = extraction("fil_a", "1000万元")
    payload = build_fact_review_batches(
        document, extracted, max_chars=100000, context_blocks=0
    )[0]
    fact = payload["facts"][0]
    review = FactReview.model_validate(
        {
            "file_id": "fil_a",
            "decisions": [
                {
                    "field_key": fact["field_key"],
                    "source_file_id": fact["source_file_id"],
                    "location": fact["location"],
                    "decision": "ACCEPT",
                    "evidence_text": fact["evidence_text"],
                    "confidence": 0.9,
                    "reason_code": "EVIDENCE_MATCHED",
                }
            ],
            "semantic_concepts": [],
            "validation_specs": [],
            "confidence": 0.9,
            "evidence_complete": True,
        }
    )

    merged = merge_fact_review_batches(document, extracted, [(payload, review)])

    assert len(merged.decisions) == 1
    assert merged.evidence_complete is True
    with pytest.raises(EvidenceValidationError, match="every candidate fact"):
        merge_fact_review_batches(
            document,
            extracted,
            [(payload, review.model_copy(update={"decisions": []}))],
        )
