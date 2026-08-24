import pytest

from app.adapters.llm.schemas import (
    DocumentFactExtraction,
    FactCandidate,
    SemanticConcept,
    ValidationSpec,
)
from app.documents.models import DocumentBlock, DocumentLocation, ParsedDocument
from app.draft_review.facts import (
    EvidenceValidationError,
    build_fact_matrix,
    chunk_document,
    fact_matrix_result_items,
    merge_chunk_extractions,
    normalize_fact,
    validate_extraction_evidence,
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


def test_not_mentioned_only_requires_review_when_consensus_plan_requires_it() -> None:
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
    assert risks == [] and passed == []
    assert reviews[0]["reason_code"] == "FACT_MISSING"


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
