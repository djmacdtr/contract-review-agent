import json
from typing import Any

import httpx
import pytest

from app.adapters.llm import openai_client as openai_client_module
from app.adapters.llm.openai_client import (
    LlmClientError,
    OpenAIContractLlmClient,
    completion_body,
    review_response_schema,
)
from app.adapters.llm.schemas import DocumentFactExtraction, FactReview
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
                    "location": {"paragraph_index": 1},
                    "confidence": 0.9,
                }
            ],
        },
        ensure_ascii=False,
    )


def extraction_payload() -> dict[str, Any]:
    return {
        "file_id": "fil_reference",
        "blocks": [
            {"text": "项目方案确认函", "location": {"paragraph_index": 0}},
            {"text": "融资金额为1000万元", "location": {"paragraph_index": 1}},
        ],
        "evidence_blocks": [
            {"text": "项目方案确认函", "location": {"paragraph_index": 0}},
            {"text": "融资金额为1000万元", "location": {"paragraph_index": 1}},
        ],
    }


def review_payload() -> dict[str, Any]:
    facts = []
    for field_key, paragraph_index in (("amount", 1), ("term", 2)):
        facts.append(
            {
                "field_key": field_key,
                "display_name": field_key,
                "value_type": "TEXT",
                "raw_value": f"value-{paragraph_index}",
                "normalized_hint": None,
                "source_file_id": "fil_reference",
                "evidence_text": f"evidence-{paragraph_index}",
                "location": {"paragraph_index": paragraph_index},
                "confidence": 0.9,
            }
        )
    return {
        "file_id": "fil_reference",
        "role": "REFERENCE",
        "blocks": [],
        "facts": facts,
        "semantic_concepts": [],
        "validation_specs": [],
    }


def review_content(payload: dict[str, Any]) -> str:
    return json.dumps(
        {
            "file_id": payload["file_id"],
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
                for fact in payload["facts"]
            ],
            "semantic_concepts": [],
            "validation_specs": [],
            "confidence": 0.9,
            "evidence_complete": True,
        },
        ensure_ascii=False,
    )


async def no_sleep(_delay: float) -> None:
    return None


@pytest.mark.parametrize(
    ("response_format", "expected"),
    [
        ("prompt_only", None),
        ("json_object", {"type": "json_object"}),
        ("json_schema", "json_schema"),
    ],
)
def test_completion_body_uses_zero_temperature_and_requested_response_format(
    response_format: str, expected: object
) -> None:
    body = completion_body(
        model="model-a",
        system="return JSON",
        payload={"safe": True},
        schema=DocumentFactExtraction,
        max_tokens=123,
        response_format=response_format,
    )

    assert body["temperature"] == 0
    assert body["stream"] is False
    assert body["max_tokens"] == 123
    if expected is None:
        assert "response_format" not in body
    elif expected == "json_schema":
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True
    else:
        assert body["response_format"] == expected


def test_fact_review_schema_and_dynamic_request_require_all_decisions() -> None:
    payload = review_payload()
    assert FactReview.model_json_schema()["properties"]["decisions"]["minItems"] == 1

    schema = review_response_schema(payload)

    assert schema["properties"]["decisions"]["minItems"] == 2
    assert schema["properties"]["decisions"]["maxItems"] == 2


async def test_review_retries_once_with_missing_candidate_identities() -> None:
    payload = review_payload()
    bodies: list[dict[str, Any]] = []
    contents = iter(
        [
            json.dumps(
                {
                    "file_id": payload["file_id"],
                    "decisions": [],
                    "semantic_concepts": [],
                    "validation_specs": [],
                    "confidence": 0.0,
                    "evidence_complete": False,
                }
            ),
            review_content(payload),
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"model": "reviewer", "choices": [{"message": {"content": next(contents)}}]},
        )

    client = OpenAIContractLlmClient(
        settings(
            LLM_REVIEW_MODEL="reviewer",
            LLM_NATIVE_STRUCTURED_OUTPUT=True,
            LLM_STRUCTURE_RETRY_ATTEMPTS=3,
        ),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    result = await client.review_facts(payload)

    assert result.structure_retries == 1
    assert result.request_attempts == 2
    assert len(result.value["decisions"]) == 2
    assert len(bodies) == 2
    for body in bodies:
        decisions = body["response_format"]["json_schema"]["schema"]["properties"][
            "decisions"
        ]
        assert decisions["minItems"] == decisions["maxItems"] == 2
    correction = bodies[1]["messages"][-1]["content"]
    assert "missing_candidate_identities" in correction
    assert "amount" in correction and "term" in correction


async def test_real_http_clients_do_not_read_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_options: list[dict[str, Any]] = []
    original_async_client = httpx.AsyncClient

    def recording_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        client_options.append(kwargs.copy())
        return original_async_client(*args, **kwargs)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "GLM-5.2"}]})
        return httpx.Response(
            200,
            json={
                "model": "GLM-5.2-actual",
                "choices": [{"message": {"content": extraction_content()}}],
            },
        )

    monkeypatch.setattr(openai_client_module.httpx, "AsyncClient", recording_async_client)
    client = OpenAIContractLlmClient(
        settings(), transport=httpx.MockTransport(handler), sleeper=no_sleep
    )

    assert await client.probe_models() == ["GLM-5.2"]
    await client.extract_facts(extraction_payload())

    assert [options["trust_env"] for options in client_options] == [False, False]


async def test_probe_models_and_valid_fenced_json() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-key"
        assert "secret-key" not in str(request.url)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "GLM-5.2"}]})
        request_body = json.loads(request.content)
        assert "CompactDocumentFactExtraction" in request_body["messages"][0]["content"]
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
    result = await client.extract_facts(extraction_payload())
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
    result = await client.extract_facts(extraction_payload())
    assert result.structure_retries == 2
    assert result.request_attempts == 3


async def test_plan_semantics_uses_bounded_internal_schema() -> None:
    fact_id = "fact_000000000000000000000000"

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "SemanticPlanResponse" in body["messages"][0]["content"]
        return httpx.Response(
            200,
            json={
                "model": "extractor",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "file_id": "fil_reference",
                                    "semantic_concepts": [
                                        {
                                            "concept_id": "financing_amount",
                                            "display_name": "融资金额",
                                            "value_type": "MONEY",
                                            "aliases": [],
                                            "fact_refs": [
                                                {
                                                    "fact_id": fact_id,
                                                    "source_file_id": "fil_reference",
                                                }
                                            ],
                                            "evidence_refs": [
                                                {
                                                    "source_file_id": "fil_reference",
                                                    "location": {"paragraph_index": 1},
                                                }
                                            ],
                                            "confidence": 0.9,
                                        }
                                    ],
                                    "validation_specs": [
                                        {
                                            "validation_id": "amount_positive",
                                            "display_name": "金额为正",
                                            "expression": {
                                                "op": "greater_than",
                                                "left": {
                                                    "op": "fact",
                                                    "fact_id": fact_id,
                                                    "source_file_id": "fil_reference",
                                                },
                                                "right": {"op": "literal", "value": "0"},
                                            },
                                            "evidence_refs": [
                                                {
                                                    "source_file_id": "fil_reference",
                                                    "location": {"paragraph_index": 1},
                                                }
                                            ],
                                            "confidence": 0.9,
                                        }
                                    ],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_EXTRACTION_MODEL="extractor"),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )
    result = await client.plan_semantics(
        {"file_id": "fil_reference", "documents": [], "mappings": {}}
    )

    assert result.value["file_id"] == "fil_reference"
    assert result.value["validation_specs"][0]["validation_id"] == "amount_positive"


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
