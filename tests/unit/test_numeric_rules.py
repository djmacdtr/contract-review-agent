import pytest

from app.adapters.llm.schemas import FactCandidate, ValidationSpec
from app.documents.models import DocumentLocation
from app.draft_review.facts import stable_fact_id
from app.draft_review.numeric_rules import (
    NumericAstError,
    evaluate_ast,
    evaluate_validation_spec,
    referenced_fact_refs,
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


def test_ast_depth_is_bounded() -> None:
    expression: dict[str, object] = {"op": "literal", "value": "1"}
    for _ in range(7):
        expression = {
            "op": "add",
            "args": [expression, {"op": "literal", "value": "1"}],
        }
    with pytest.raises(NumericAstError, match="maximum depth"):
        validate_ast(expression)


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


def test_qualified_ast_does_not_mix_same_named_facts_across_files() -> None:
    target = fact("amount", "100")
    reference = target.model_copy(
        update={
            "source_file_id": "fil_b",
            "raw_value": "200",
            "evidence_text": "金额200",
        }
    )
    target_ref = (stable_fact_id(target), target.source_file_id)
    reference_ref = (stable_fact_id(reference), reference.source_file_id)
    spec = ValidationSpec(
        validation_id="target_amount_positive",
        display_name="目标金额为正",
        expression={
            "op": "greater_than",
            "left": {
                "op": "fact",
                "fact_id": target_ref[0],
                "source_file_id": target_ref[1],
            },
            "right": {"op": "literal", "value": "0"},
        },
        evidence_locations=[],
        confidence=0.9,
    )
    result = evaluate_validation_spec(
        spec,
        {target_ref: target, reference_ref: reference},
    )

    assert referenced_fact_refs(spec.expression) == {target_ref}
    assert result["status"] == "PASSED"
    assert result["source_evidence"][0]["file_id"] == "fil_a"


def test_old_concept_ast_is_not_a_qualified_reference() -> None:
    with pytest.raises(NumericAstError, match="qualified"):
        referenced_fact_refs(
            {"op": "fact", "concept_id": "amount"}
        )
