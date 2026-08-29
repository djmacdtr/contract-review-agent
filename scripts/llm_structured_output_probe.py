"""Probe gateway JSON modes with synthetic data only and safe output."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ValidationError

from app.adapters.llm.openai_client import _endpoint, completion_body
from app.adapters.llm.schemas import NumericCandidateExtraction, TextFactExtraction
from app.core.config import Settings


class SyntheticProbeResponse(BaseModel):
    marker: Literal["synthetic"]
    ok: bool


PRODUCTION_PROBE_CASES: dict[str, tuple[type[BaseModel], dict[str, Any], str]] = {
    "numeric_candidate": (
        NumericCandidateExtraction,
        {
            "file_id": "synthetic-file",
            "batch_id": "batch_synthetic",
            "numeric_candidates": [
                {
                    "candidate_index": 1,
                    "raw_value": "12",
                    "candidate_kind": "NUMBER",
                    "location": {"paragraph_index": 0},
                    "span": {"start": 0, "end": 2},
                }
            ],
        },
        '{"items":[{"candidate_index":1,"semantic_key":"synthetic_value",'
        '"display_name":"合成数值","value_type":"NUMBER","decision":"FACT",'
        '"reason_code":"SYNTHETIC","confidence":0.9}]}',
    ),
    "text_fact": (
        TextFactExtraction,
        {
            "file_id": "synthetic-file",
            "batch_id": "batch_synthetic",
            "units": [
                {
                    "unit_id": "unit_0123456789abcdef",
                    "type": "PARAGRAPH",
                    "text": "合成保证人：甲方",
                    "location": {"paragraph_index": 0},
                }
            ],
        },
        '{"items":[{"unit_id":"unit_0123456789abcdef","semantic_key":"synthetic_party",'
        '"display_name":"合成主体","value_type":"ENTITY","quote":"甲方",'
        '"confidence":0.9}],"has_more":false}',
    ),
}


def _safe_error(error: BaseException) -> str:
    return type(error).__name__


async def probe_mode(
    settings: Settings,
    mode: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    payload = {"marker": "synthetic", "request": "probe"}
    body = completion_body(
        model=settings.LLM_EXTRACTION_MODEL,
        system="只返回 marker 为 synthetic 且 ok 为 true 的 JSON 对象。",
        payload=payload,
        schema=SyntheticProbeResponse,
        max_tokens=64,
        response_format=mode,
    )
    result: dict[str, Any] = {
        "mode": mode,
        "status_code": None,
        "finish_reason": None,
        "json_valid": False,
        "schema_valid": False,
        "error_code": None,
    }
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=settings.LLM_TIMEOUT_SECONDS,
            trust_env=False,
        ) as client:
            response = await client.post(
                _endpoint(settings.LLM_BASE_URL, "chat/completions"),
                headers={
                    "Authorization": f"Bearer {settings.LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            result["status_code"] = response.status_code
            if response.status_code >= 400:
                result["error_code"] = f"HTTP_{response.status_code}"
                return result
            outer = response.json()
            choice = outer["choices"][0]
            result["finish_reason"] = choice.get("finish_reason")
            content = choice["message"]["content"]
            value = json.loads(content)
            result["json_valid"] = isinstance(value, dict)
            parsed = SyntheticProbeResponse.model_validate(value)
            result["schema_valid"] = parsed.ok and parsed.marker == "synthetic"
            if not result["schema_valid"]:
                result["error_code"] = "PROBE_SCHEMA_NOT_ENFORCED"
    except json.JSONDecodeError:
        result["error_code"] = "INVALID_JSON"
    except ValidationError:
        result["error_code"] = "PROBE_SCHEMA_NOT_ENFORCED"
    except Exception as exc:
        result["error_code"] = _safe_error(exc)
    return result


async def run_probe(settings: Settings) -> dict[str, Any]:
    if not settings.llm_configured:
        raise RuntimeError("LLM gateway is not configured")
    schema_result = await probe_mode(settings, "json_schema")
    object_result = await probe_mode(settings, "json_object")
    if schema_result["schema_valid"] and schema_result["finish_reason"] == "stop":
        selected = "json_schema"
    elif object_result["schema_valid"] and object_result["finish_reason"] == "stop":
        selected = "json_object"
    else:
        selected = "prompt_only"
    return {
        "json_schema": schema_result,
        "json_object": object_result,
        "selected_response_format": selected,
    }


def _json_boundary(content: Any) -> str:
    if not isinstance(content, str):
        return "non_string"
    stripped = content.strip()
    if not stripped:
        return "empty"
    if stripped.startswith("{") and stripped.endswith("}"):
        return "object"
    if stripped.startswith("[") and stripped.endswith("]"):
        return "array"
    return "other"


def _safe_decode_error(exc: json.JSONDecodeError, content: str) -> dict[str, Any]:
    length = max(len(content), 1)
    return {
        "decode_error_category": "json_decode_error",
        "decode_error_position_ratio": round(min(max(exc.pos / length, 0), 1), 6),
    }


async def _probe_production_case(
    settings: Settings,
    *,
    mode: str,
    case_name: str,
    schema: type[BaseModel],
    payload: dict[str, Any],
    expected_content: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    body = completion_body(
        model=settings.LLM_EXTRACTION_MODEL,
        system="这是完全合成的结构化输出能力探测，只返回合成样例 JSON。",
        payload=payload,
        schema=schema,
        max_tokens=256,
        response_format=mode,
    )
    result: dict[str, Any] = {
        "case": case_name,
        "mode": mode,
        "status_code": None,
        "finish_reason": None,
        "content_chars": 0,
        "reasoning_content_chars": 0,
        "content_empty": True,
        "content_has_code_fence": False,
        "json_boundary": "empty",
        "json_valid": False,
        "schema_valid": False,
        "error_code": None,
        "decode_error_category": None,
        "decode_error_position_ratio": None,
        "schema_summary": {
            "schema_name": schema.__name__,
            "top_level": "object",
            "required_field_count": len(schema.model_json_schema().get("required", [])),
        },
    }
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=settings.LLM_TIMEOUT_SECONDS,
            trust_env=False,
        ) as client:
            response = await client.post(
                _endpoint(settings.LLM_BASE_URL, "chat/completions"),
                headers={
                    "Authorization": f"Bearer {settings.LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        result["status_code"] = response.status_code
        if response.status_code >= 400:
            result["error_code"] = f"HTTP_{response.status_code}"
            return result
        outer = response.json()
        choice = outer["choices"][0]
        result["finish_reason"] = choice.get("finish_reason")
        message = choice["message"]
        content = message.get("content")
        reasoning = message.get("reasoning_content")
        result["content_chars"] = len(content) if isinstance(content, str) else 0
        result["reasoning_content_chars"] = len(reasoning) if isinstance(reasoning, str) else 0
        result["content_empty"] = not isinstance(content, str) or not content.strip()
        result["content_has_code_fence"] = isinstance(content, str) and "```" in content
        result["json_boundary"] = _json_boundary(content)
        if not isinstance(content, str):
            result["error_code"] = "CONTENT_NOT_STRING"
            return result
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            result.update(_safe_decode_error(exc, content))
            result["error_code"] = "INVALID_JSON"
            return result
        result["json_valid"] = isinstance(value, dict)
        schema.model_validate(value)
        result["schema_valid"] = True
        parsed = schema.model_validate(value)
        if case_name == "numeric_candidate":
            indices = [item.candidate_index for item in parsed.items]
            if indices != [1]:
                result["error_code"] = "PROBE_CANDIDATE_COVERAGE_INVALID"
                result["schema_valid"] = False
        else:
            if not parsed.items or any(
                item.unit_id != "unit_0123456789abcdef" or item.quote not in "合成保证人：甲方"
                for item in parsed.items
            ):
                result["error_code"] = "PROBE_EVIDENCE_SHAPE_INVALID"
                result["schema_valid"] = False
    except ValidationError:
        result["error_code"] = "PROBE_SCHEMA_NOT_ENFORCED"
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        result["error_code"] = "RESPONSE_ENVELOPE_INVALID"
    except Exception as exc:
        result["error_code"] = _safe_error(exc)
    return result


async def run_production_probe(
    settings: Settings,
    *,
    attempts: int = 3,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Probe both production extraction schemas three times per JSON mode.

    No result body is returned.  A mode is eligible only when both schemas pass
    every attempt with a stopping finish reason and valid JSON/schema data.
    """

    if not settings.llm_configured:
        raise RuntimeError("LLM gateway is not configured")
    modes: dict[str, dict[str, Any]] = {}
    for mode in ("json_schema", "json_object"):
        cases: dict[str, list[dict[str, Any]]] = {}
        for case_name, (schema, payload, expected) in PRODUCTION_PROBE_CASES.items():
            case_results: list[dict[str, Any]] = []
            for _ in range(attempts):
                case_results.append(
                    await _probe_production_case(
                        settings,
                        mode=mode,
                        case_name=case_name,
                        schema=schema,
                        payload=payload,
                        expected_content=expected,
                        transport=transport,
                    )
                )
            cases[case_name] = case_results
        modes[mode] = {
            "cases": cases,
            "all_passed": all(
                item["status_code"] == 200
                and item["finish_reason"] == "stop"
                and item["json_valid"]
                and item["schema_valid"]
                and not item["content_has_code_fence"]
                for values in cases.values()
                for item in values
            ),
            "attempt_count": attempts * len(cases),
        }
    if modes["json_schema"]["all_passed"]:
        selected = "json_schema"
    elif modes["json_object"]["all_passed"]:
        selected = "json_object"
    else:
        selected = "prompt_only"
    return {
        "json_schema": modes["json_schema"],
        "json_object": modes["json_object"],
        "selected_response_format": selected,
        "production_gate_passed": selected != "prompt_only",
        "total_http_calls": sum(item["attempt_count"] for item in modes.values()),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe structured JSON output safely")
    return parser.parse_args()


if __name__ == "__main__":
    parse_args()
    print(
        json.dumps(
            asyncio.run(run_production_probe(Settings())),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
