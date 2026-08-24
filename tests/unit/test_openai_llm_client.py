import json

import httpx
import pytest

from app.adapters.llm.openai_client import LlmClientError, OpenAIContractLlmClient
from app.core.config import Settings


def settings(**overrides):
    values = {
        "LLM_ENABLED": True,
        "LLM_BASE_URL": "https://llm.example.com/v1",
        "LLM_API_KEY": "secret-key",
        "LLM_TIMEOUT_SECONDS": 0.01,
        "LLM_STRUCTURE_RETRY_ATTEMPTS": 1,
        **overrides,
    }
    return Settings(_env_file=None, **values)


def extraction_content() -> str:
    return json.dumps(
        {
            "profile": {
                "file_id": "fil_reference",
                "document_kind": "项目方案确认函",
                "title": "项目方案确认函",
                "confidence": 0.9,
                "evidence_locations": [{"paragraph_index": 0}],
            },
            "facts": [
                {
                    "field_key": "financing_amount",
                    "display_name": "融资金额",
                    "value_type": "MONEY",
                    "raw_value": "1000万元",
                    "normalized_hint": "10000000",
                    "source_file_id": "fil_reference",
                    "evidence_text": "融资金额为1000万元",
                    "location": {"paragraph_index": 1},
                    "confidence": 0.9,
                }
            ],
            "missing_field_keys": [],
        },
        ensure_ascii=False,
    )


async def no_sleep(_delay: float) -> None:
    return None


async def test_probe_models_and_valid_fenced_json() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-key"
        assert "secret-key" not in str(request.url)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "GLM-5.2"}]})
        request_body = json.loads(request.content)
        assert "DocumentFactExtraction" in request_body["messages"][0]["content"]
        return httpx.Response(
            200,
            json={
                "model": "GLM-5.2-actual",
                "choices": [{"message": {"content": f"```json\n{extraction_content()}\n```"}}],
            },
        )

    client = OpenAIContractLlmClient(
        settings(), transport=httpx.MockTransport(handler), sleeper=no_sleep
    )
    assert await client.probe_models() == ["GLM-5.2"]
    result = await client.extract_facts({"file_id": "fil_reference", "blocks": []})
    assert result.value["facts"][0]["field_key"] == "financing_amount"
    assert result.actual_model == "GLM-5.2-actual"
    assert result.structure_retries == 0


async def test_invalid_json_and_schema_are_retried_then_succeed() -> None:
    responses = iter(
        [
            "not-json",
            json.dumps({"profile": {}, "facts": []}),
            extraction_content(),
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"model": "m", "choices": [{"message": {"content": next(responses)}}]},
        )

    client = OpenAIContractLlmClient(
        settings(LLM_STRUCTURE_RETRY_ATTEMPTS=2),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )
    result = await client.extract_facts({})
    assert result.structure_retries == 2
    assert result.request_attempts == 3


@pytest.mark.parametrize(
    ("status", "code", "calls"),
    [
        (401, "LLM_AUTH_FAILED", 1),
        (403, "LLM_AUTH_FAILED", 1),
        (404, "LLM_ENDPOINT_NOT_FOUND", 1),
        (429, "LLM_RATE_LIMITED", 3),
        (502, "LLM_UPSTREAM_ERROR", 3),
    ],
)
async def test_safe_http_error_mapping(status: int, code: str, calls: int) -> None:
    seen = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        return httpx.Response(status, text="sensitive upstream response")

    client = OpenAIContractLlmClient(
        settings(), transport=httpx.MockTransport(handler), sleeper=no_sleep
    )
    with pytest.raises(LlmClientError) as caught:
        await client.extract_facts({"contract": "sensitive"})
    assert caught.value.code == code
    assert "sensitive" not in caught.value.safe_message
    assert seen == calls


async def test_timeout_is_retried_and_safely_mapped() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("contains upstream details", request=request)

    client = OpenAIContractLlmClient(
        settings(), transport=httpx.MockTransport(handler), sleeper=no_sleep
    )
    with pytest.raises(LlmClientError) as caught:
        await client.extract_facts({})
    assert caught.value.code == "LLM_TIMEOUT"
    assert calls == 3
