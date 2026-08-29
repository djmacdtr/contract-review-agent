import json

import httpx
import pytest

from app.core.config import Settings
from scripts.llm_structured_output_probe import run_production_probe


def probe_settings() -> Settings:
    return Settings(
        _env_file=None,
        LLM_ENABLED=True,
        LLM_BASE_URL="https://llm.example.com",
        LLM_API_KEY="synthetic-key",
    )


def response_for(request: httpx.Request, *, invalid_text: bool = False) -> httpx.Response:
    body = json.loads(request.content)
    schema = body.get("response_format", {}).get("json_schema", {}).get("name")
    if schema is None:
        schema = (
            "NumericCandidateExtraction"
            if "NumericCandidateExtraction" in body["messages"][0]["content"]
            else "TextFactExtraction"
        )
    if invalid_text:
        content = "not-json"
    elif schema == "NumericCandidateExtraction":
        content = json.dumps(
            {
                "items": [
                    {
                        "candidate_index": 1,
                        "semantic_key": "synthetic_value",
                        "display_name": "合成数值",
                        "value_type": "NUMBER",
                        "decision": "FACT",
                        "reason_code": "SYNTHETIC",
                        "confidence": 0.9,
                    }
                ],
            },
            ensure_ascii=False,
        )
    else:
        content = json.dumps(
            {
                "items": [
                    {
                        "unit_id": "unit_0123456789abcdef",
                        "semantic_key": "synthetic_party",
                        "display_name": "合成主体",
                        "value_type": "ENTITY",
                        "quote": "甲方",
                        "confidence": 0.9,
                    }
                ],
                "has_more": False,
            },
            ensure_ascii=False,
        )
    return httpx.Response(
        200,
        json={
            "model": "synthetic",
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
        },
    )


@pytest.mark.asyncio
async def test_complex_probe_requires_both_schemas_three_of_three_and_is_safe() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response_for(request)

    result = await run_production_probe(
        probe_settings(),
        transport=httpx.MockTransport(handler),
    )
    assert result["production_gate_passed"] is True
    assert result["selected_response_format"] == "json_schema"
    assert result["total_http_calls"] == 12
    assert all(
        len(values) == 3
        for mode in ("json_schema", "json_object")
        for values in result[mode]["cases"].values()
    )
    assert all(
        item["content_chars"] > 0
        for item in result["json_schema"]["cases"]["numeric_candidate"]
    )
    assert "synthetic-key" not in json.dumps(result)
    assert all(request.headers["authorization"] == "Bearer synthetic-key" for request in requests)


@pytest.mark.asyncio
async def test_complex_probe_falls_back_without_enabling_production_schema() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response_for(request, invalid_text=True)

    result = await run_production_probe(
        probe_settings(),
        transport=httpx.MockTransport(handler),
    )
    assert calls == 12
    assert result["production_gate_passed"] is False
    assert result["selected_response_format"] == "prompt_only"
    assert all(
        item["error_code"] == "INVALID_JSON"
        for mode in ("json_schema", "json_object")
        for values in result[mode]["cases"].values()
        for item in values
    )
