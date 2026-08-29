"""Probe DRAFT_REVIEW LLM concurrency and critical schemas with synthetic data only."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from app.adapters.llm.openai_client import LlmClientError, OpenAIContractLlmClient
from app.core.config import Settings


class CountingTransport(httpx.AsyncBaseTransport):
    """Count safe transport outcomes without retaining requests or responses."""

    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport(retries=0)
        self.http_calls = 0
        self.statuses: Counter[int] = Counter()
        self.safe_call_metadata: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.http_calls += 1
        response = await self.inner.handle_async_request(request)
        try:
            content = await response.aread()
            self.statuses[response.status_code] += 1
            call: dict[str, Any] = {"http_status": response.status_code}
            try:
                request_body = json.loads(request.content)
                if isinstance(request_body, dict):
                    call["configured_model"] = request_body.get("model")
                    response_format = request_body.get("response_format")
                    call["response_format"] = (
                        response_format.get("type")
                        if isinstance(response_format, dict)
                        else "prompt_only"
                    )
            except (TypeError, json.JSONDecodeError):
                call["response_format"] = "unknown"
            try:
                response_body = json.loads(content)
                if isinstance(response_body, dict):
                    call["actual_model"] = response_body.get("model")
                    choices = response_body.get("choices")
                    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                        call["finish_reason"] = choices[0].get("finish_reason")
            except (TypeError, json.JSONDecodeError):
                call["finish_reason"] = None
            self.safe_call_metadata.append(call)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=content,
                request=request,
                extensions=response.extensions,
            )
        finally:
            await response.aclose()

    async def aclose(self) -> None:
        # The client creates a short-lived AsyncClient per logical call.  Keep
        # the shared probe transport alive across the complete wave.
        return None

    async def close_all(self) -> None:
        await self.inner.aclose()


def _safe_error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, LlmClientError):
        return {
            "error_code": exc.code,
            "failure_code": exc.failure_code,
            "request_attempts": exc.request_attempts,
            "structure_retries": exc.structure_retries,
            "finish_reason": exc.finish_reason,
        }
    if isinstance(exc, TimeoutError):
        return {"error_code": "TIMEOUT"}
    return {"error_code": type(exc).__name__}


def _profile_payload(index: int) -> dict[str, Any]:
    return {
        "file_id": f"synthetic_probe_{index}",
        "role": "REFERENCE",
        "overview_blocks": [
            {
                "unit_id": f"unit_synthetic_{index}",
                "type": "PARAGRAPH",
                "text": f"结构化并发探测样本 {index}，仅用于识别测试资料用途。",
                "location": {"paragraph_index": 0},
            }
        ],
        "extraction_requirements": {
            "return_only_document_overview": True,
            "document_kind_is_open_ended": True,
            "identity_is_program_owned": True,
        },
    }


def _mapping_payload() -> dict[str, Any]:
    return {
        "reference_file_id": "synthetic_reference",
        "reference_profile": {
            "document_kind": "测试辅助资料",
            "title": "结构化映射探针",
        },
        "target_facts": [
            {
                "target_fact_id": "target_fact_000001",
                "field_key": "test_metric",
                "concept_id": "test_metric",
                "display_name": "测试指标",
                "value_type": "NUMBER",
                "raw_value": "10",
                "normalized_hint": "10",
                "location": {"paragraph_index": 0},
                "confidence": 0.98,
            },
            {
                "target_fact_id": "target_fact_000002",
                "field_key": "test_window",
                "concept_id": "test_window",
                "display_name": "测试周期",
                "value_type": "DURATION",
                "raw_value": "5日",
                "normalized_hint": "5 days",
                "location": {"paragraph_index": 1},
                "confidence": 0.98,
            },
        ],
        "reference_facts": [
            {
                "field_key": "test_metric",
                "concept_id": "test_metric",
                "display_name": "测试指标",
                "value_type": "NUMBER",
                "raw_value": "10",
                "normalized_hint": "10",
                "source_file_id": "synthetic_reference",
                "evidence_text": "测试指标为10。",
                "location": {"paragraph_index": 0},
                "confidence": 0.98,
            },
            {
                "field_key": "test_window",
                "concept_id": "test_window",
                "display_name": "测试周期",
                "value_type": "DURATION",
                "raw_value": "5日",
                "normalized_hint": "5 days",
                "source_file_id": "synthetic_reference",
                "evidence_text": "测试周期为5日。",
                "location": {"paragraph_index": 1},
                "confidence": 0.98,
            },
        ],
    }


def _advice_payload() -> dict[str, Any]:
    return {
        "files": [
            {"file_id": "synthetic_left", "file_name": "测试资料甲", "role": "TEMPLATE"},
            {"file_id": "synthetic_right", "file_name": "测试资料乙", "role": "TARGET"},
        ],
        "risk_items": [
            {
                "risk_id": "risk_synthetic_1",
                "risk_type": "MODIFICATION",
                "title": "测试指标差异",
                "description": "测试资料甲记载10，测试资料乙记载12。",
                "related_diff_ids": ["diff_synthetic_1"],
                "source_evidence": [],
            },
            {
                "risk_id": "risk_synthetic_2",
                "risk_type": "MODIFICATION",
                "title": "测试周期差异",
                "description": "测试资料甲记载5日，测试资料乙记载7日。",
                "related_diff_ids": ["diff_synthetic_2"],
                "source_evidence": [],
            },
        ],
        "diff_items": [
            {
                "diff_id": "diff_synthetic_1",
                "diff_type": "TEXT_MODIFIED",
                "title": "测试指标",
                "baseline": {"file_id": "synthetic_left", "text": "测试指标为10。"},
                "target": {"file_id": "synthetic_right", "text": "测试指标为12。"},
                "segments": [],
            },
            {
                "diff_id": "diff_synthetic_2",
                "diff_type": "TEXT_MODIFIED",
                "title": "测试周期",
                "baseline": {"file_id": "synthetic_left", "text": "测试周期为5日。"},
                "target": {"file_id": "synthetic_right", "text": "测试周期为7日。"},
                "segments": [],
            },
        ],
        "related_facts": [],
    }


async def _run_profile_wave(settings: Settings, width: int, offset: int) -> dict[str, Any]:
    transport = CountingTransport()
    client = OpenAIContractLlmClient(
        settings.model_copy(update={"LLM_MAX_CONCURRENCY": width}),
        transport=transport,
    )
    started = time.monotonic()

    async def call(index: int) -> dict[str, Any]:
        try:
            result = await client.extract_document_profile(_profile_payload(index))
            return {
                "status": "SUCCEEDED",
                "request_attempts": result.request_attempts,
                "structure_retries": result.structure_retries,
                "finish_reason": result.finish_reason,
            }
        except BaseException as exc:
            return {"status": "FAILED", **_safe_error(exc)}

    try:
        calls = await asyncio.gather(*(call(offset + index) for index in range(width)))
    finally:
        await transport.close_all()
    clean = all(
        item.get("status") == "SUCCEEDED"
        and item.get("request_attempts") == 1
        and item.get("structure_retries") == 0
        and item.get("finish_reason") != "length"
        for item in calls
    ) and not any(code == 429 or code >= 500 for code in transport.statuses)
    return {
        "width": width,
        "status": "SUCCEEDED" if clean else "FAILED",
        "logical_calls": width,
        "http_calls": transport.http_calls,
        "status_counts": dict(sorted(transport.statuses.items())),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "calls": calls,
    }


async def _run_schema_probe(
    settings: Settings, operation: str, payload: dict[str, Any]
) -> dict[str, Any]:
    transport = CountingTransport()
    client = OpenAIContractLlmClient(settings, transport=transport)
    method = {
        "FACT_MAPPING": client.map_facts,
        "RISK_ADVICE": client.generate_advice,
    }[operation]
    started = time.monotonic()
    try:
        result = await method(payload)
        if operation == "FACT_MAPPING":
            returned = len(result.value.get("mappings", []))
            valid = returned == 2 and result.value.get("reference_file_id") == (
                "synthetic_reference"
            )
        else:
            ids = {
                item.get("risk_id")
                for item in result.value.get("risk_advices", [])
                if isinstance(item, dict)
            }
            returned = len(ids)
            valid = ids == {"risk_synthetic_1", "risk_synthetic_2"}
        call = {
            "status": "SUCCEEDED" if valid else "FAILED",
            "returned_items": returned,
            "actual_model": result.actual_model,
            "request_attempts": result.request_attempts,
            "structure_retries": result.structure_retries,
            "finish_reason": result.finish_reason,
        }
    except BaseException as exc:
        call = {"status": "FAILED", **_safe_error(exc)}
    finally:
        await transport.close_all()
    return {
        "operation": operation,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "http_calls": transport.http_calls,
        "status_counts": dict(sorted(transport.statuses.items())),
        **call,
    }


async def run() -> dict[str, Any]:
    settings = Settings().model_copy(
        update={
            "LLM_ENABLED": True,
            "LLM_RESPONSE_FORMAT": "json_schema",
            "LLM_NATIVE_STRUCTURED_OUTPUT": True,
            "LLM_STRUCTURE_RETRY_ATTEMPTS": 0,
        }
    )
    if not settings.llm_configured:
        return {"status": "BLOCKED", "reason_code": "LLM_NOT_CONFIGURED"}
    transport = CountingTransport()
    client = OpenAIContractLlmClient(settings, transport=transport)
    try:
        models = await client.probe_models()
    except BaseException as exc:
        await transport.close_all()
        return {
            "status": "BLOCKED",
            "contains_contract_text": False,
            "model_list_status": "FAILED",
            "model_list_error": _safe_error(exc),
        }
    finally:
        await transport.close_all()
    selected = settings.LLM_MAX_CONCURRENCY
    if "GLM-5.3-Flash" not in models:
        return {
            "status": "BLOCKED",
            "contains_contract_text": False,
            "model_list_status": "FAILED",
            "reason_code": "GLM_5_3_FLASH_NOT_LISTED",
            "available_model_count": len(models),
        }
    schema_probes: list[dict[str, Any]] = []
    if selected:
        selected_settings = settings.model_copy(update={"LLM_MAX_CONCURRENCY": selected})
        schema_probes = [
            await _run_schema_probe(selected_settings, "FACT_MAPPING", _mapping_payload()),
            await _run_schema_probe(selected_settings, "RISK_ADVICE", _advice_payload()),
        ]
    ready = bool(selected) and all(
        item.get("status") == "SUCCEEDED"
        and item.get("actual_model") == "GLM-5.3-Flash"
        and item.get("finish_reason") == "stop"
        and item.get("request_attempts") == 1
        and item.get("structure_retries") == 0
        for item in schema_probes
    )
    return {
        "status": "SUCCEEDED" if ready else "BLOCKED",
        "contains_contract_text": False,
        "model_list_status": "SUCCEEDED",
        "model_list_contains_glm_5_3_flash": True,
        "available_model_count": len(models),
        "selected_concurrency": selected,
        "profile_logical_calls": 0,
        "profile_http_calls": 0,
        "schema_logical_calls": len(schema_probes),
        "schema_http_calls": sum(int(item["http_calls"]) for item in schema_probes),
        "schema_probes": schema_probes,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    report = asyncio.run(run())
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    raise SystemExit(0 if report["status"] == "SUCCEEDED" else 2)
