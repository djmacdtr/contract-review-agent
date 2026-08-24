import pytest

from app.adapters.llm.schemas import FactCandidate, ValidationSpec
from app.documents.models import DocumentLocation
from app.draft_review.numeric_rules import (
    NumericAstError,
    evaluate_ast,
    evaluate_validation_spec,
    validate_ast,
)


def fact(field_key: str, value: str) -> FactCandidate:
    return FactCandidate(
        field_key=field_key,
        display_name=field_key,
        value_type="MONEY",
        raw_value=value,
        source_file_id="fil_a",
        evidence_text=value,
        location=DocumentLocation(paragraph_index=0),
        confidence=0.95,
    )


def test_decimal_ast_supports_arithmetic_and_tolerance() -> None:
    expression = {
        "op": "equals",
        "left": {
            "op": "multiply",
            "args": [{"op": "fact", "concept_id": "unit_price"}, {"op": "literal", "value": "2"}],
        },
        "right": {"op": "fact", "concept_id": "total"},
        "tolerance": "0.01",
    }
    validate_ast(expression)
    assert evaluate_ast(expression, {"unit_price": 1_000_000, "total": 2_000_000}) is True


def test_invalid_ast_and_missing_fact_are_review_required() -> None:
    with pytest.raises(NumericAstError):
        validate_ast({"op": "execute", "code": "__import__('os')"})
    spec = ValidationSpec(
        validation_id="amount_check",
        display_name="金额校验",
        expression={
            "op": "equals",
            "left": {"op": "fact", "concept_id": "missing"},
            "right": {"op": "literal", "value": "1"},
        },
        confidence=0.9,
    )
    result = evaluate_validation_spec(spec, [fact("known", "1")])
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["reason_code"] == "NUMERIC_RULE_UNCERTAIN"


def test_conflicting_duplicate_inputs_are_review_required() -> None:
    spec = ValidationSpec(
        validation_id="amount_check",
        display_name="金额校验",
        expression={
            "op": "equals",
            "left": {"op": "fact", "concept_id": "amount"},
            "right": {"op": "literal", "value": "1"},
        },
        confidence=0.9,
    )
    result = evaluate_validation_spec(spec, [fact("amount", "1"), fact("amount", "2")])
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["reason_code"] == "NUMERIC_RULE_AMBIGUOUS_INPUT"
