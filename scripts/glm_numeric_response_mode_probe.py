"""Probe GLM numeric structured-response modes with one synthetic input each."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from app.adapters.llm.openai_client import (
    LlmClientError,
    OpenAIContractLlmClient,
    _numeric_candidate_response_summary,
)
from app.core.config import Settings
from app.draft_review.facts import expand_numeric_candidate_response
from scripts.draft_review_llm_readiness import CountingTransport

MODES = ("prompt_only", "json_object", "json_schema")


def numeric_probe_payload() -> dict[str, Any]:
    return {
        "file_id": "synthetic_numeric_mode_probe",
        "role": "TARGET",
        "batch_id": "batch_synthetic_numeric_mode_probe",
        "units": [
            {
                "unit_id": "unit_synthetic_numeric_mode",
                "type": "PARAGRAPH",
                "text": "租赁期限为12个月。",
                "location": {"paragraph_index": 0},
            }
        ],
        "numeric_candidates": [
            {
                "candidate_index": 1,
                "raw_value": "12个月",
                "value_type": "DURATION",
                "location": {"paragraph_index": 0},
            }
        ],
        "requirements": {
            "max_items": 24,
            "required_decision_count": 1,
            "each_candidate_exactly_once": True,
        },
    }


def safe_error(exc: BaseException, payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "FAILED",
        "error_code": exc.code if isinstance(exc, LlmClientError) else type(exc).__name__,
        "failure_code": getattr(exc, "failure_code", None),
        "request_attempts": int(getattr(exc, "request_attempts", 0) or 0),
        "structure_retries": int(getattr(exc, "structure_retries", 0) or 0),
        "finish_reason": getattr(exc, "finish_reason", None),
    }
    summary = getattr(exc, "validation_summary", None)
    if not isinstance(summary, dict):
        summary = _numeric_candidate_response_summary({}, payload)
    for key in (
        "expected_count",
        "returned_count",
        "missing_index_count",
        "duplicate_index_count",
        "invalid_index_count",
    ):
        result[key] = int(summary.get(key, 0) or 0)
    return result


async def probe_mode(base: Settings, mode: str) -> dict[str, Any]:
    settings = base.model_copy(
        update={
            "LLM_ENABLED": True,
            "LLM_RESPONSE_FORMAT": mode,
            "LLM_NATIVE_STRUCTURED_OUTPUT": mode == "json_schema",
            "LLM_HTTP_RETRY_ATTEMPTS": 0,
            "LLM_STRUCTURE_RETRY_ATTEMPTS": 0,
            "LLM_MAX_CONCURRENCY": 1,
        }
    )
    payload = numeric_probe_payload()
    transport = CountingTransport()
    started = time.monotonic()
    try:
        client = OpenAIContractLlmClient(settings, transport=transport)
        result = await client.extract_numeric_candidates(
            payload, allow_structure_correction=False
        )
        facts, classified = expand_numeric_candidate_response(payload, result.value)
        summary = _numeric_candidate_response_summary(result.value, payload)
        return {
            "mode": mode,
            "status": "SUCCEEDED",
            "actual_model": result.actual_model,
            "finish_reason": result.finish_reason,
            "request_attempts": result.request_attempts,
            "structure_retries": result.structure_retries,
            "http_calls": transport.http_calls,
            "status_counts": dict(sorted(transport.statuses.items())),
            "fact_count": len(facts),
            "classified_count": len(classified),
            **summary,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        return {
            "mode": mode,
            "http_calls": transport.http_calls,
            "status_counts": dict(sorted(transport.statuses.items())),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            **safe_error(exc, payload),
        }
    finally:
        await transport.close_all()


async def run() -> dict[str, Any]:
    base = Settings()
    if not base.llm_configured:
        return {"status": "BLOCKED", "reason_code": "LLM_NOT_CONFIGURED"}
    probes = [await probe_mode(base, mode) for mode in MODES]
    return {
        "status": (
            "SUCCEEDED"
            if all(item["status"] == "SUCCEEDED" for item in probes)
            else "FAILED"
        ),
        "probe_count": len(probes),
        "modes": probes,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "SUCCEEDED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
