import pytest
from pydantic import ValidationError

from app.adapters.llm.schemas import DocumentFactExtraction


def valid_payload() -> dict:
    return {
        "profile": {
            "file_id": "fil_target",
            "document_kind": "UNKNOWN",
            "title": None,
            "confidence": 0.4,
            "evidence_locations": [{"paragraph_index": 0}],
        },
        "facts": [
            {
                "field_key": "financing_amount",
                "display_name": "融资金额",
                "value_type": "MONEY",
                "raw_value": "1000万元",
                "normalized_hint": "10000000",
                "source_file_id": "fil_target",
                "evidence_text": "融资金额为1000万元",
                "location": {"paragraph_index": 7},
                "confidence": 0.9,
            }
        ],
        "missing_field_keys": ["interest_rate"],
    }


def test_fact_extraction_accepts_open_unknown_profile_and_evidenced_fact() -> None:
    result = DocumentFactExtraction.model_validate(valid_payload())

    assert result.profile.document_kind == "UNKNOWN"
    assert result.facts[0].location.paragraph_index == 7


@pytest.mark.parametrize("missing_key", ["evidence_text", "location", "source_file_id"])
def test_fact_candidate_without_required_evidence_is_rejected(missing_key: str) -> None:
    payload = valid_payload()
    payload["facts"][0].pop(missing_key)

    with pytest.raises(ValidationError):
        DocumentFactExtraction.model_validate(payload)


def test_llm_schema_rejects_extra_fields_and_duplicate_missing_keys() -> None:
    payload = valid_payload()
    payload["facts"][0]["invented"] = True

    with pytest.raises(ValidationError):
        DocumentFactExtraction.model_validate(payload)

    payload = valid_payload()
    payload["missing_field_keys"] = ["interest_rate", "interest_rate"]
    with pytest.raises(ValidationError):
        DocumentFactExtraction.model_validate(payload)
