from scripts.expanded_fact_canary import is_recoverable_text_canary


def test_multi_unit_invalid_json_is_recoverable_for_canary() -> None:
    assert is_recoverable_text_canary("LLM_INVALID_JSON", 16)
    assert not is_recoverable_text_canary("LLM_INVALID_JSON", 1)
