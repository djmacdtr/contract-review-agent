import asyncio
import json
from typing import Any

import httpx
import pytest

from app.adapters.llm import openai_client as openai_client_module
from app.adapters.llm.openai_client import (
    LlmClientError,
    OpenAIContractLlmClient,
    _numeric_candidate_response_schema,
    _numeric_candidate_response_summary,
    _numeric_max_output_tokens,
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


def test_mapping_schema_validation_exposes_only_safe_subcode() -> None:
    with pytest.raises(LlmClientError) as raised:
        openai_client_module._validate_mapping({})

    assert raised.value.code == "LLM_SCHEMA_INVALID"
    assert raised.value.failure_code == "LLM_RESPONSE_SCHEMA_INVALID"
    assert raised.value.validation_summary
    assert "模型事实映射" not in str(raised.value.validation_summary)


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


def test_completion_body_can_disable_model_thinking_without_changing_format() -> None:
    body = completion_body(
        model="model-a",
        system="return JSON",
        payload={"safe": True},
        schema=DocumentFactExtraction,
        max_tokens=2048,
        response_format="json_schema",
        disable_thinking=True,
    )

    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["response_format"]["type"] == "json_schema"


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


async def test_review_compact_response_rehydrates_program_owned_identity() -> None:
    payload = review_payload()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["max_tokens"] == 8192
        schema = body["response_format"]["json_schema"]["schema"]
        assert schema["title"] == "CompactFactReview"
        return httpx.Response(
            200,
            json={
                "model": "reviewer",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "decisions": [
                                        {
                                            "fact_index": 1,
                                            "decision": "ACCEPT",
                                            "confidence": 0.9,
                                            "reason_code": "EVIDENCE_MATCHED",
                                        },
                                        {
                                            "fact_index": 2,
                                            "decision": "REJECT",
                                            "confidence": 0.8,
                                            "reason_code": "VALUE_MISMATCH",
                                        },
                                    ],
                                    "confidence": 0.85,
                                    "evidence_complete": True,
                                }
                            )
                        }
                    }
                ],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_NATIVE_STRUCTURED_OUTPUT=True),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    result = await client.review_facts(payload)

    assert result.value["decisions"][0]["source_file_id"] == "fil_reference"
    assert result.value["decisions"][0]["location"]["paragraph_index"] == 1
    assert result.value["decisions"][1]["field_key"] == "term"
    assert result.value["decisions"][1]["decision"] == "REJECT"


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
            return httpx.Response(
                200, json={"data": [{"id": "GLM-5.3-Flash"}]}
            )
        return httpx.Response(
            200,
            json={
                "model": "GLM-5.3-Flash",
                "choices": [{"message": {"content": extraction_content()}}],
            },
        )

    monkeypatch.setattr(openai_client_module.httpx, "AsyncClient", recording_async_client)
    client = OpenAIContractLlmClient(
        settings(), transport=httpx.MockTransport(handler), sleeper=no_sleep
    )

    assert await client.probe_models() == ["GLM-5.3-Flash"]
    await client.extract_facts(extraction_payload())

    assert [options["trust_env"] for options in client_options] == [False, False]


async def test_probe_models_and_valid_fenced_json() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-key"
        assert "secret-key" not in str(request.url)
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"data": [{"id": "GLM-5.3-Flash"}]}
            )
        request_body = json.loads(request.content)
        assert "CompactDocumentFactExtraction" in request_body["messages"][0]["content"]
        return httpx.Response(
            200,
            json={
                "model": "GLM-5.3-Flash",
                "choices": [{"message": {"content": extraction_content()}}],
            },
        )

    client = OpenAIContractLlmClient(
        settings(), transport=httpx.MockTransport(handler), sleeper=no_sleep
    )
    assert await client.probe_models() == ["GLM-5.3-Flash"]
    result = await client.extract_facts(extraction_payload())
    assert result.value["facts"][0]["field_key"] == "financing_amount"
    assert result.actual_model == "GLM-5.3-Flash"
    assert result.structure_retries == 0


async def test_compact_extraction_preserves_safe_evidence_failure_code() -> None:
    invalid = json.loads(extraction_content())
    invalid["facts"][0]["raw_value"] = "2000万元"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "extractor",
                "choices":[{"message": {"content": json.dumps(invalid, ensure_ascii=False)}}],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_STRUCTURE_RETRY_ATTEMPTS=0),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    with pytest.raises(LlmClientError) as error:
        await client.extract_facts(extraction_payload())

    assert error.value.code == "LLM_EXTRACTION_EVIDENCE_INVALID"
    assert error.value.failure_code == "FACT_VALUE_NOT_GROUNDED"


async def test_compact_extraction_schema_failure_has_safe_code() -> None:
    requests: list[dict[str, Any]] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(_request.content))
        return httpx.Response(
            200,
            json={
                "model": "extractor",
                "choices":[{"message": {"content": '{"profile": {}, "facts": []}'}}],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_STRUCTURE_RETRY_ATTEMPTS=0),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    with pytest.raises(LlmClientError) as error:
        await client.extract_facts(extraction_payload())

    assert error.value.failure_code == "LLM_RESPONSE_SCHEMA_INVALID"
    assert len(requests) == 1


async def test_compact_schema_summary_is_safe_and_normalized() -> None:
    invalid = json.loads(extraction_content())
    invalid["facts"][0]["value_type"] = "NOT_A_VALUE"
    invalid["facts"][0]["raw_value"] = "SECRET_FACT_VALUE"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "extractor",
                "choices":[{"message": {"content": json.dumps(invalid)}}],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_STRUCTURE_RETRY_ATTEMPTS=0),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    with pytest.raises(LlmClientError) as error:
        await client.extract_facts(extraction_payload())

    summary = error.value.validation_summary
    assert summary is not None
    assert summary["error_count"] >= 1
    assert {
        "path": "facts.*.value_type",
        "error_type": "literal_error",
        "count": 1,
    } in summary["items"]
    safe_text = json.dumps(summary, ensure_ascii=False) + (error.value.correction_message or "")
    assert "NOT_A_VALUE" not in safe_text
    assert "SECRET_FACT_VALUE" not in safe_text
    assert "msg" not in safe_text
    assert "input" not in safe_text
    assert "ctx" not in safe_text


async def test_compact_schema_summary_drives_one_safe_correction() -> None:
    invalid = json.loads(extraction_content())
    invalid["facts"][0]["value_type"] = "NOT_A_VALUE"
    responses = iter([json.dumps(invalid), extraction_content()])
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "extractor",
                "choices":[{"message": {"content": next(responses)}}],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_STRUCTURE_RETRY_ATTEMPTS=1),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    result = await client.extract_facts(extraction_payload())

    assert result.structure_retries == 1
    assert len(requests) == 2
    correction = requests[1]["messages"][-1]["content"]
    assert "facts.*.value_type" in correction
    assert "literal_error" in correction
    assert '"count":1' in correction
    assert "NOT_A_VALUE" not in correction
    assert result.value["facts"][0]["field_key"] == "financing_amount"


async def test_invalid_json_gets_one_structure_retry_then_succeeds() -> None:
    responses = iter(
        [
            "not-json",
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
    assert result.structure_retries == 1
    assert result.request_attempts == 2


async def test_compact_extraction_does_not_retry_json_more_than_once() -> None:
    responses = iter(["not-json", "still-not-json", extraction_content()])
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "extractor",
                "choices": [{"message": {"content": next(responses)}}],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_STRUCTURE_RETRY_ATTEMPTS=5),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    with pytest.raises(LlmClientError) as error:
        await client.extract_facts(extraction_payload())

    assert error.value.code == "LLM_INVALID_JSON"
    assert error.value.request_attempts == 2
    assert error.value.structure_retries == 1
    assert len(requests) == 2


async def test_plan_semantics_uses_bounded_internal_schema() -> None:
    fact_id = "fact_000000000000000000000000"

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "CompactSemanticPlanResponse" in body["messages"][0]["content"]
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
                                            "fact_ids": [fact_id],
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
        {
            "file_id": "fil_reference",
            "documents": [
                {
                    "file_id": "fil_reference",
                    "facts": [
                        {
                            "fact_id": fact_id,
                            "source_file_id": "fil_reference",
                            "location": {"paragraph_index": 1},
                        }
                    ],
                }
            ],
            "mappings": {},
        }
    )

    assert result.value["file_id"] == "fil_reference"
    assert result.value["validation_specs"][0]["validation_id"] == "amount_positive"


@pytest.mark.parametrize(
    ("status", "code", "calls"),
    [
        (401, "LLM_AUTH_FAILED", 1),
        (403, "LLM_AUTH_FAILED", 1),
        (404, "LLM_ENDPOINT_NOT_FOUND", 1),
        (429, "LLM_RATE_LIMITED", 5),
        (500, "LLM_UPSTREAM_ERROR", 5),
        (502, "LLM_UPSTREAM_ERROR", 5),
        (503, "LLM_UPSTREAM_ERROR", 5),
        (504, "LLM_UPSTREAM_ERROR", 5),
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
    assert calls == 2


async def test_http_retry_can_be_disabled_for_protocol_probe() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="sensitive upstream response")

    client = OpenAIContractLlmClient(
        settings(LLM_HTTP_RETRY_ATTEMPTS=0),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    with pytest.raises(LlmClientError) as caught:
        await client.extract_facts({})

    assert caught.value.code == "LLM_UPSTREAM_ERROR"
    assert caught.value.request_attempts == 1
    assert calls == 1


async def test_fact_batch_uses_compact_schema_without_program_owned_identity() -> None:
    payload = {
        "file_id": "fil_synthetic",
        "batch_id": "batch_synthetic",
        "units": [
            {
                "unit_id": "unit_1",
                "type": "PARAGRAPH",
                "text": "融资金额为100万元",
                "location": {"paragraph_index": 0},
            }
        ],
        "numeric_candidates": [
            {
                "candidate_index": 1,
                "raw_value": "100万元",
                "candidate_kind": "MONEY",
                "location": {"paragraph_index": 0},
            }
        ],
    }
    response = {
        "facts": [
            {
                "field_key": "amount",
                "display_name": "金额",
                "value_type": "MONEY",
                "raw_value": "100万元",
                "location": {"paragraph_index": 0},
                "confidence": 0.95,
                "candidate_indices": [1],
            }
        ],
        "numeric_candidate_decisions": [
            {"candidate_index": 1, "decision": "FACT", "reason_code": "EXTRACTED"}
        ],
    }
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "extractor",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(response, ensure_ascii=False)},
                    }
                ],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_RESPONSE_FORMAT="json_schema"),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    result = await client.extract_fact_batch(payload)

    assert result.value == response
    body = requests[0]
    assert body["response_format"]["type"] == "json_schema"
    assert "evidence_text" not in json.dumps(result.value)
    assert "source_file_id" not in json.dumps(result.value)


async def test_text_fact_prompt_requires_empty_json_object_when_no_fact_is_grounded() -> None:
    payload = {
        "file_id": "fil_synthetic",
        "batch_id": "batch_synthetic_text",
        "units": [
            {
                "unit_id": "unit_1",
                "type": "PARAGRAPH",
                "text": "本段不包含可可靠识别的非数值事实。",
                "location": {"paragraph_index": 0},
            }
        ],
        "readonly_context": [],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "{\"items\":[],\"has_more\":false}" in body["messages"][0]["content"]
        assert body["response_format"]["json_schema"]["schema"]["required"] == [
            "items",
            "has_more",
        ]
        return httpx.Response(
            200,
            json={
                "model": "extractor",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"items": [], "has_more": false}'},
                    }
                ],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_RESPONSE_FORMAT="json_schema"),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    result = await client.extract_text_facts(payload)

    assert result.value == {"items": [], "has_more": False}
async def test_text_schema_uses_payload_fact_limit() -> None:
    payload = {
        "file_id": "fil_synthetic",
        "batch_id": "batch_synthetic_text",
        "units": [],
        "readonly_context": [],
        "requirements": {"max_items": 6},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"]["json_schema"]["schema"]["properties"]["items"][
            "maxItems"
        ] == 6
        assert "has_more" in body["response_format"]["json_schema"]["schema"]["required"]
        item_properties = body["response_format"]["json_schema"]["schema"]["$defs"][
            "TextFactItem"
        ]["properties"]
        assert set(item_properties) == {
            "unit_id",
            "semantic_key",
            "display_name",
            "value_type",
            "quote",
            "confidence",
        }
        assert "requirements.max_items" in body["messages"][0]["content"]
        return httpx.Response(
            200,
            json={
                "model": "extractor",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"items": [], "has_more": false}'},
                    }
                ],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_RESPONSE_FORMAT="json_schema"),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    result = await client.extract_text_facts(payload)

    assert result.value == {"items": [], "has_more": False}


async def test_text_response_override_is_local_and_keeps_numeric_schema() -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if body["messages"][1]["content"].find("numeric_candidates") >= 0:
            content = json.dumps(
                {
                    "items": [
                        {
                            "candidate_index": 1,
                            "semantic_key": "amount",
                            "display_name": "金额",
                            "value_type": "MONEY",
                            "decision": "IGNORE",
                            "reason_code": "NO_FACT",
                            "confidence": 0.0,
                        }
                    ]
                }
            )
        else:
            content = '{"items": [], "has_more": false}'
        return httpx.Response(
            200,
            json={
                "model": "extractor",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": content},
                    }
                ],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_RESPONSE_FORMAT="json_schema"),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
        text_response_format_override="json_object",
        text_model_override="Qwen3.8-Flash-Next",
    )

    await client.extract_text_facts(
        {
            "file_id": "fil_synthetic",
            "batch_id": "batch_text",
            "units": [],
            "readonly_context": [],
        }
    )
    await client.extract_numeric_candidates(
        {
            "file_id": "fil_synthetic",
            "batch_id": "batch_numeric",
            "units": [
                {"text": "金额为100元", "location": {"paragraph_index": 0}}
            ],
            "numeric_candidates": [
                {
                    "candidate_index": 1,
                    "candidate_id": "numeric_0000000000000000",
                    "raw_value": "100元",
                    "candidate_kind": "MONEY",
                    "location": {"paragraph_index": 0},
                    "span": {"start": 3, "end": 7},
                }
            ],
        }
    )

    assert requests[0]["response_format"] == {"type": "json_object"}
    assert requests[0]["model"] == "Qwen3.8-Flash-Next"
    assert requests[1]["response_format"]["type"] == "json_schema"
    assert requests[1]["model"] == "GLM-5.3-Flash"


async def test_invalid_text_json_exposes_only_safe_response_metadata() -> None:
    content = "```json\n{\"items\": [\n"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "extractor",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": content},
                    }
                ],
            },
        )

    client = OpenAIContractLlmClient(
        settings(
            LLM_RESPONSE_FORMAT="json_schema",
            LLM_HTTP_RETRY_ATTEMPTS=0,
            LLM_STRUCTURE_RETRY_ATTEMPTS=0,
        ),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
        text_response_format_override="json_object",
    )

    with pytest.raises(LlmClientError) as caught:
        await client.extract_text_facts(
            {
                "file_id": "fil_synthetic",
                "batch_id": "batch_text",
                "units": [],
                "readonly_context": [],
            },
            allow_structure_correction=False,
        )

    error = caught.value
    assert error.code == "LLM_INVALID_JSON"
    assert error.finish_reason == "stop"
    assert error.content_chars == len(content)
    assert error.code_fence is True
    assert isinstance(error.json_error_position, int)
    assert "items" not in str(error)
    assert "```" not in str(error)


def test_numeric_schema_requires_exact_candidate_count() -> None:
    payload = {
        "numeric_candidates": [{"candidate_index": index} for index in range(1, 7)],
    }

    schema = _numeric_candidate_response_schema(payload)

    assert schema["properties"]["items"]["minItems"] == 6
    assert schema["properties"]["items"]["maxItems"] == 6
    item_schema = schema["$defs"]["NumericCandidateItem"]
    assert "candidate_index" in item_schema["required"]
    assert item_schema["properties"]["candidate_index"] == {
        "type": "integer",
        "enum": [1, 2, 3, 4, 5, 6],
    }
    assert set(item_schema["properties"]) == {
        "candidate_index",
        "semantic_key",
        "display_name",
        "value_type",
        "decision",
        "reason_code",
        "confidence",
    }
    assert set(item_schema["required"]) == set(item_schema["properties"])


def test_numeric_schema_rejects_empty_candidate_batches() -> None:
    with pytest.raises(ValueError, match="at least one candidate"):
        _numeric_candidate_response_schema({"numeric_candidates": []})


@pytest.mark.asyncio
async def test_empty_numeric_candidates_short_circuit_without_http() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    client = OpenAIContractLlmClient(
        settings(LLM_RESPONSE_FORMAT="json_schema"),
        transport=httpx.MockTransport(handler),
        sleeper=lambda _delay: asyncio.sleep(0),
    )

    result = await client.extract_numeric_candidates({"numeric_candidates": []})

    assert result.value == {"items": []}
    assert result.request_attempts == 0
    assert result.structure_retries == 0
    assert result.mock is True
    assert calls == 0


def test_numeric_output_tokens_scale_with_candidate_count() -> None:
    assert _numeric_max_output_tokens({"numeric_candidates": []}) == 2048
    assert _numeric_max_output_tokens({"numeric_candidates": [{}]}) == 2048
    assert _numeric_max_output_tokens({"numeric_candidates": [{} for _ in range(12)]}) == 3584
    assert _numeric_max_output_tokens({"numeric_candidates": [{} for _ in range(24)]}) == 6656


@pytest.mark.asyncio
async def test_mapping_operations_use_expanded_budget_only_for_mapping() -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if body["messages"][0]["content"].startswith("你是跨文件合同事实映射器"):
            content = json.dumps(
                {
                    "reference_file_id": "fil_reference",
                    "mappings": [],
                    "missing_requirements": [],
                }
            )
        elif body["messages"][0]["content"].startswith("你是独立的跨文件事实映射评审器"):
            content = json.dumps(
                {
                    "reference_file_id": "fil_reference",
                    "decisions": [],
                    "missing_requirement_decisions": [],
                    "confidence": 0.0,
                    "evidence_complete": False,
                }
            )
        else:
            content = json.dumps(
                {
                    "overall_advice": "请核对本项差异的具体业务依据。",
                    "priority_actions": [],
                    "manual_review_focus": [],
                    "limitations": [],
                    "evidence_refs": [],
                    "risk_advices": [
                        {
                            "risk_id": "risk_1",
                            "analysis_advice": "请核对金额差异的审批依据。",
                        }
                    ],
                }
            )
        return httpx.Response(
            200,
            json={
                "model": "GLM-5.3-Flash",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": content},
                    }
                ],
            },
        )

    client = OpenAIContractLlmClient(
        settings(
            LLM_EXTRACTION_MODEL="GLM-5.3-Flash",
            LLM_REVIEW_MODEL="GLM-5.3-Flash",
            LLM_RESPONSE_FORMAT="json_schema",
        ),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
        advice_response_format_override="json_object",
    )

    await client.map_facts({"reference_file_id": "fil_reference"})
    await client.review_mappings({"reference_file_id": "fil_reference"})
    await client.generate_advice({"risk_items": [{"risk_id": "risk_1"}]})

    assert len(requests) == 3
    assert [request["max_tokens"] for request in requests] == [12288, 12288, 8192]
    assert [request["model"] for request in requests] == [
        "GLM-5.3-Flash",
        "GLM-5.3-Flash",
        "GLM-5.3-Flash",
    ]
    assert [request["response_format"]["type"] for request in requests] == [
        "json_schema",
        "json_schema",
        "json_object",
    ]
    assert [request.get("chat_template_kwargs") for request in requests] == [
        {"enable_thinking": False},
        {"enable_thinking": False},
        {"enable_thinking": False},
    ]
    assert openai_client_module._PROFILE_MAX_OUTPUT_TOKENS == 2048
    assert openai_client_module._TEXT_MAX_OUTPUT_TOKENS == 8192
    assert openai_client_module._ADVICE_MAX_OUTPUT_TOKENS == 8192


def test_numeric_response_summary_contains_only_safe_index_counts() -> None:
    payload = {"numeric_candidates": [{"candidate_index": 1}]}

    summary = _numeric_candidate_response_summary(
        {"items": [{"candidate_index": 2}]}, payload
    )

    assert summary == {
        "expected_count": 1,
        "returned_count": 1,
        "missing_index_count": 1,
        "duplicate_index_count": 0,
        "invalid_index_count": 1,
    }


async def test_numeric_request_includes_required_decision_count_and_dynamic_schema() -> None:
    payload = {
        "file_id": "fil_synthetic",
        "batch_id": "batch_synthetic_numeric",
        "units": [],
        "numeric_candidates": [
            {
                "candidate_index": index,
                "raw_value": str(index),
                "value_type": "NUMBER",
                "location": {"paragraph_index": index - 1},
            }
            for index in range(1, 3)
        ],
        "requirements": {
            "max_items": 24,
            "required_decision_count": 2,
        },
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        schema = body["response_format"]["json_schema"]["schema"]
        assert schema["properties"]["items"]["minItems"] == 2
        assert schema["properties"]["items"]["maxItems"] == 2
        assert '"required_decision_count":2' in body["messages"][1]["content"]
        return httpx.Response(
            200,
            json={
                "model": "extractor",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "items": [
                                        {
                                            "candidate_index": index,
                                            "semantic_key": f"amount_{index}",
                                            "display_name": "金额",
                                            "value_type": "NUMBER",
                                            "decision": "IGNORE",
                                            "reason_code": "NO_MATCH",
                                            "confidence": 0.8,
                                        }
                                        for index in range(1, 3)
                                    ]
                                }
                            )
                        },
                    }
                ],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_RESPONSE_FORMAT="json_schema"),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    result = await client.extract_numeric_candidates(payload)

    assert result.value["items"][0]["candidate_index"] == 1


async def test_text_evidence_subcode_survives_malformed_correction() -> None:
    unit_id = "unit_aaaaaaaa"
    payload = {
        "file_id": "fil_synthetic",
        "batch_id": "batch_synthetic_text",
        "units": [
            {
                "unit_id": unit_id,
                "type": "PARAGRAPH",
                "text": "保证人为甲方。",
                "location": {"paragraph_index": 0},
            }
        ],
        "readonly_context": [],
    }
    invalid_evidence = {
        "items": [
            {
                "unit_id": unit_id,
                "semantic_key": "guarantor",
                "display_name": "保证人",
                "value_type": "ENTITY",
                "quote": "乙方",
                "confidence": 0.9,
            }
        ],
        "has_more": False,
    }
    responses = iter(
        [json.dumps(invalid_evidence, ensure_ascii=False), "无法严格回查。"]
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "extractor",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": next(responses)},
                    }
                ],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_RESPONSE_FORMAT="json_schema"),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    with pytest.raises(LlmClientError) as caught:
        await client.extract_text_facts(payload)

    assert caught.value.code == "LLM_INVALID_JSON"
    assert caught.value.failure_code == "FACT_QUOTE_NOT_GROUNDED"
    assert caught.value.structure_retries == 1


async def test_length_finish_reason_is_not_structure_corrected() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": "extractor",
                "choices": [
                    {"finish_reason": "length", "message": {"content": "{"}}
                ],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_RESPONSE_FORMAT="json_object"),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    with pytest.raises(LlmClientError) as caught:
        await client.extract_fact_batch({"file_id": "fil_synthetic", "units": []})

    assert caught.value.code == "LLM_OUTPUT_TRUNCATED"
    assert caught.value.finish_reason == "length"
    assert caught.value.content_chars == 1
    assert caught.value.reasoning_content_chars == 0
    assert caught.value.max_tokens == 2048
    assert calls == 1


async def test_truncation_diagnostics_include_safe_reasoning_and_usage_metadata() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "extractor",
                "usage": {
                    "prompt_tokens": 17,
                    "completion_tokens": 2048,
                    "total_tokens": 2065,
                    "prompt_tokens_details": {"cached_tokens": 9},
                },
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {
                            "content": "{\"",
                            "reasoning_content": "internal reasoning",
                        },
                    }
                ],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_RESPONSE_FORMAT="json_object"),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    with pytest.raises(LlmClientError) as caught:
        await client.extract_fact_batch({"file_id": "fil_synthetic", "units": []})

    error = caught.value
    assert error.content_chars == 2
    assert error.reasoning_content_chars == len("internal reasoning")
    assert error.usage == {
        "prompt_tokens": 17,
        "completion_tokens": 2048,
        "total_tokens": 2065,
    }
    assert "internal reasoning" not in str(error)


async def test_numeric_singleton_disables_thinking_and_uses_numeric_model_override() -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "model": "Qwen3.8-Flash-Next",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "items": [
                                        {
                                            "candidate_index": 1,
                                            "semantic_key": "amount",
                                            "display_name": "金额",
                                            "value_type": "MONEY",
                                            "decision": "IGNORE",
                                            "reason_code": "NO_MATCH",
                                            "confidence": 0.9,
                                        }
                                    ]
                                }
                            )
                        },
                    }
                ],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_RESPONSE_FORMAT="json_schema"),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
        numeric_model_override="Qwen3.8-Flash-Next",
    )

    result = await client.extract_numeric_candidates(
        {"numeric_candidates": [{"candidate_index": 1}]},
        allow_structure_correction=False,
    )

    assert result.configured_model == "Qwen3.8-Flash-Next"
    assert result.actual_model == "Qwen3.8-Flash-Next"
    assert requests[0]["model"] == "Qwen3.8-Flash-Next"
    assert requests[0]["max_tokens"] == 2048
    assert requests[0]["chat_template_kwargs"] == {"enable_thinking": False}


async def test_numeric_singleton_400_thinking_fallback_uses_8192_without_repeating_schema() -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(400, request=request, json={"error": "unsupported"})
        return httpx.Response(
            200,
            json={
                "model": "extractor",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "items": [
                                        {
                                            "candidate_index": 1,
                                            "semantic_key": "amount",
                                            "display_name": "金额",
                                            "value_type": "MONEY",
                                            "decision": "IGNORE",
                                            "reason_code": "NO_MATCH",
                                            "confidence": 0.9,
                                        }
                                    ]
                                }
                            )
                        },
                    }
                ],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_RESPONSE_FORMAT="json_schema", LLM_HTTP_RETRY_ATTEMPTS=0),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    result = await client.extract_numeric_candidates(
        {"numeric_candidates": [{"candidate_index": 1}]},
        allow_structure_correction=False,
    )

    assert len(requests) == 2
    assert requests[0]["max_tokens"] == 2048
    assert requests[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert requests[1]["max_tokens"] == 8192
    assert "chat_template_kwargs" not in requests[1]
    assert result.request_attempts == 2


async def test_fact_batch_evidence_error_is_corrected_once_without_json_repair() -> None:
    calls = 0
    payload = {
        "file_id": "fil_synthetic",
        "batch_id": "batch_synthetic",
        "units": [
            {
                "unit_id": "unit_1",
                "type": "PARAGRAPH",
                "text": "融资金额为100万元",
                "location": {"paragraph_index": 0},
            }
        ],
        "numeric_candidates": [],
    }
    response = {
        "facts": [
            {
                "field_key": "amount",
                "display_name": "金额",
                "value_type": "MONEY",
                "raw_value": "100万元",
                "location": {"paragraph_index": 99},
                "confidence": 0.95,
            }
        ],
        "numeric_candidate_decisions": [],
    }

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": "extractor",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(response)},
                    }
                ],
            },
        )

    client = OpenAIContractLlmClient(
        settings(LLM_RESPONSE_FORMAT="json_object"),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    with pytest.raises(LlmClientError) as caught:
        await client.extract_fact_batch(payload)

    assert caught.value.code == "LLM_EXTRACTION_EVIDENCE_INVALID"
    assert calls == 2


async def test_upstream_502_uses_bounded_http_retries() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502, text="sensitive gateway body")

    client = OpenAIContractLlmClient(
        settings(), transport=httpx.MockTransport(handler), sleeper=no_sleep
    )

    with pytest.raises(LlmClientError) as caught:
        await client.extract_fact_batch({"file_id": "fil_synthetic", "units": []})

    assert caught.value.code == "LLM_UPSTREAM_ERROR"
    assert caught.value.request_attempts == 5
    assert calls == 5


async def test_all_client_requests_share_global_concurrency_gate() -> None:
    active = 0
    maximum = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json={"data": [{"id": "model-a"}]})

    client = OpenAIContractLlmClient(
        settings(LLM_MAX_CONCURRENCY=2, LLM_TIMEOUT_SECONDS=1),
        transport=httpx.MockTransport(handler),
    )

    await asyncio.gather(*(client.probe_models() for _ in range(6)))

    assert maximum == 2
