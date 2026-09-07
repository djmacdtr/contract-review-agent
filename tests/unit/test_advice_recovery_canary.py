from __future__ import annotations

import pytest

from app.results.advice import fallback_analysis_advice
from scripts.draft_review_advice_recovery_canary import (
    EXPECTED_FALLBACK_RISK_IDS,
    _identify_fallback_risk_ids,
    _select_fallback_result,
)
from tests.unit.test_advice_batches import advice_result


def test_canary_identifies_exact_fallback_risk_set_without_external_calls() -> None:
    result = advice_result(6)
    for index, risk in enumerate(result["risk_items"], start=14):
        risk["risk_id"] = f"risk_diff_{index:06d}"
        risk["analysis_advice"] = fallback_analysis_advice(result, risk)

    selected, fallback_ids = _select_fallback_result(result)

    assert tuple(fallback_ids) == EXPECTED_FALLBACK_RISK_IDS
    assert _identify_fallback_risk_ids(result) == list(EXPECTED_FALLBACK_RISK_IDS)
    assert [risk["risk_id"] for risk in selected["risk_items"]] == list(
        EXPECTED_FALLBACK_RISK_IDS
    )
    assert all(risk.get("analysis_advice") is None for risk in selected["risk_items"])
    assert all(risk.get("analysis_advice") for risk in result["risk_items"])


def test_canary_rejects_a_non_exact_fallback_risk_set() -> None:
    result = advice_result(6)
    for risk in result["risk_items"]:
        risk["analysis_advice"] = fallback_analysis_advice(result, risk)

    with pytest.raises(ValueError, match="FALLBACK_RISK_SET_INVALID"):
        _select_fallback_result(result)
