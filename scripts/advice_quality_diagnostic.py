"""Run one real eight-risk Advice quality canary without mutating task state."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.llm.openai_client import (
    _ADVICE_MAX_OUTPUT_TOKENS,
    LlmClientError,
    OpenAIContractLlmClient,
)
from app.adapters.llm.schemas import AdviceResponse
from app.core.config import Settings
from app.db.models import TaskResult
from app.results.advice import (
    ADVICE_QUALITY_CODES,
    advice_has_dynamic_anchor,
    advice_payload,
    empty_advice_quality_counts,
    extract_dynamic_advice_anchors,
    validate_advice_item,
)
from scripts.draft_review_llm_readiness import CountingTransport
from scripts.retry_failed_draft_report_host import host_database_url

SOURCE_RESULT_TASK_ID = "tsk_01M161GFY6Q7YSP07R877XQM2B"
RISK_BATCH_SIZE = 8
SAFE_CANARY_VALUE_ERRORS = {
    "ADVICE_CANARY_RISK_SET_INVALID",
    "ADVICE_CANARY_NOT_SPECIFIC",
}


def _safe_error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, LlmClientError):
        return {
            "failure_code": exc.failure_code or exc.code,
            "request_attempts": exc.request_attempts,
            "structure_retries": exc.structure_retries,
            "finish_reason": exc.finish_reason,
            "content_chars": exc.content_chars,
            "code_fence": exc.code_fence,
            "json_error_position": exc.json_error_position,
        }
    if isinstance(exc, ValidationError):
        return {"failure_code": "LLM_RESPONSE_SCHEMA_INVALID"}
    if isinstance(exc, ValueError):
        return {
            "failure_code": (
                str(exc)
                if str(exc) in SAFE_CANARY_VALUE_ERRORS
                else "ADVICE_CANARY_VALIDATION_REJECTED"
            )
        }
    return {"failure_code": type(exc).__name__}


def _batch_result(result: dict[str, Any]) -> tuple[dict[str, Any], int]:
    risks = [risk for risk in result.get("risk_items", []) if isinstance(risk, dict)]
    batches = [
        risks[index : index + RISK_BATCH_SIZE]
        for index in range(0, len(risks), RISK_BATCH_SIZE)
    ]
    if not batches:
        raise ValueError("ADVICE_CANARY_NO_RISKS")
    candidates: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
    for index, batch in enumerate(batches):
        subset = {
            **result,
            "risk_items": batch,
            "diff_items": [
                diff
                for diff in result.get("diff_items", [])
                if any(diff.get("diff_id") in risk.get("related_diff_ids", []) for risk in batch)
            ],
        }
        payload = advice_payload(subset)
        payload_chars = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        candidates.append((payload_chars, -index, subset, payload))
    full_batches = [
        candidate
        for candidate in candidates
        if len(candidate[2]["risk_items"]) == RISK_BATCH_SIZE
    ]
    if not full_batches:
        raise ValueError("ADVICE_CANARY_NO_FULL_BATCH")
    _payload_chars, _order, selected, _payload = max(full_batches)
    return selected, len(selected["risk_items"])


def _extract_dynamic_anchors(
    result: dict[str, Any], risk: dict[str, Any]
) -> list[tuple[str, bool]]:
    return extract_dynamic_advice_anchors(result, risk)


def _validate_specificity(result: dict[str, Any], response: AdviceResponse) -> int:
    risks_by_id = {risk["risk_id"]: risk for risk in result.get("risk_items", [])}
    specific_count = 0
    for item in response.risk_advices:
        risk = risks_by_id.get(item.risk_id) or {}
        if advice_has_dynamic_anchor(result, risk, item.analysis_advice):
            specific_count += 1
    return specific_count


def _validate_response_quality(
    result: dict[str, Any],
    response: AdviceResponse,
    *,
    require_dynamic_anchor: bool = True,
) -> tuple[AdviceResponse, int, dict[str, int], int, int]:
    """Apply the same item-level quality gates as production Advice merging."""

    counts = empty_advice_quality_counts()
    seen_risk_ids: set[str] = set()
    seen_advice_texts: set[str] = set()
    normalized_items = []
    normalized_count = 0
    not_specific_count = 0
    for item in response.risk_advices:
        outcome = validate_advice_item(
            result,
            item,
            seen_risk_ids=seen_risk_ids,
            seen_advice_texts=seen_advice_texts,
            require_dynamic_anchor=require_dynamic_anchor,
        )
        seen_risk_ids.add(item.risk_id)
        if outcome.reason_code in ADVICE_QUALITY_CODES:
            counts[outcome.reason_code] += 1
        if outcome.reason_code == "NOT_SPECIFIC":
            not_specific_count += 1
        if outcome.normalized_multi_sentence:
            normalized_count += 1
        if outcome.accepted:
            seen_advice_texts.add(outcome.normalized_advice)
            normalized_items.append(
                item.model_copy(update={"analysis_advice": outcome.normalized_advice})
            )
    normalized_response = response.model_copy(update={"risk_advices": normalized_items})
    return normalized_response, len(normalized_items), counts, normalized_count, not_specific_count


def _canary_status(
    *,
    accepted_count: int,
    risk_count: int,
    quality_counts: dict[str, int],
    not_specific_count: int,
) -> str:
    if accepted_count == risk_count:
        return "SUCCEEDED"
    if (
        accepted_count > 0
        and not_specific_count == risk_count - accepted_count
        and not any(quality_counts.values())
    ):
        return "RECOVERABLE"
    return "FAILED"


async def run(output: Path, *, source_task_id: str = SOURCE_RESULT_TASK_ID) -> dict[str, Any]:
    base = Settings()
    engine = create_async_engine(host_database_url(base.DATABASE_URL), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    transport = CountingTransport()
    report: dict[str, Any] = {
        "status": "FAILED",
        "failure_stage": "CANARY_SETUP",
        "failure_code": "CANARY_SETUP_ERROR",
    }
    try:
        async with session_factory() as session:
            stored = await session.get(TaskResult, source_task_id)
        if stored is None or not isinstance(stored.result, dict):
            report = {
                "status": "FAILED",
                "failure_stage": "CANARY_SETUP",
                "failure_code": "SOURCE_RESULT_NOT_FOUND",
            }
        else:
            selected, risk_count = _batch_result(stored.result)
            payload = advice_payload(selected)
            settings = base.model_copy(
                update={
                    "LLM_ENABLED": True,
                    "LLM_MAX_CONCURRENCY": 1,
                    "LLM_HTTP_RETRY_ATTEMPTS": 0,
                    "LLM_STRUCTURE_RETRY_ATTEMPTS": 0,
                    "LLM_RESPONSE_FORMAT": "json_schema",
                    "LLM_NATIVE_STRUCTURED_OUTPUT": True,
                }
            )
            client = OpenAIContractLlmClient(
                settings,
                transport=transport,
                advice_response_format_override="json_object",
            )
            try:
                generated = await client.generate_advice(payload)
                response = AdviceResponse.model_validate(generated.value)
                returned_ids = [item.risk_id for item in response.risk_advices]
                expected_ids = [risk["risk_id"] for risk in selected["risk_items"]]
                if set(returned_ids) != set(expected_ids) or len(set(returned_ids)) != risk_count:
                    raise ValueError("ADVICE_CANARY_RISK_SET_INVALID")
                (
                    normalized_response,
                    accepted_count,
                    quality_counts,
                    normalized_count,
                    not_specific_count,
                ) = _validate_response_quality(selected, response)
                canary_status = _canary_status(
                    accepted_count=accepted_count,
                    risk_count=risk_count,
                    quality_counts=quality_counts,
                    not_specific_count=not_specific_count,
                )
                if canary_status == "RECOVERABLE":
                    report = {
                        "status": "RECOVERABLE",
                        "failure_stage": "ADVICE_CANARY",
                        "source_task_id": source_task_id,
                        "risk_count": risk_count,
                        "returned_count": len(returned_ids),
                        "unique_count": len(set(returned_ids)),
                        "accepted_count": accepted_count,
                        "quality_counts": quality_counts,
                        "multi_sentence_normalized_count": normalized_count,
                        "not_specific_count": not_specific_count,
                        "specific_count": accepted_count,
                        "failure_code": "ADVICE_CANARY_PARTIAL_NOT_SPECIFIC",
                        "canary_allowed": True,
                        "configured_model": generated.configured_model,
                        "actual_model": generated.actual_model,
                        "response_format": generated.response_format,
                        "max_output_tokens": _ADVICE_MAX_OUTPUT_TOKENS,
                        "request_attempts": generated.request_attempts,
                        "structure_retries": generated.structure_retries,
                        "finish_reason": generated.finish_reason,
                        "response_metadata": generated.response_metadata,
                    }
                elif canary_status == "FAILED":
                    report = {
                        "status": "FAILED",
                        "failure_stage": "ADVICE_CANARY",
                        "source_task_id": source_task_id,
                        "risk_count": risk_count,
                        "accepted_count": accepted_count,
                        "quality_counts": quality_counts,
                        "multi_sentence_normalized_count": normalized_count,
                        "not_specific_count": not_specific_count,
                        "failure_code": "ADVICE_CANARY_QUALITY_REJECTED",
                    }
                else:
                    report = {
                        "status": "SUCCEEDED",
                        "source_task_id": source_task_id,
                        "risk_count": risk_count,
                        "returned_count": len(returned_ids),
                        "unique_count": len(set(returned_ids)),
                        "accepted_count": accepted_count,
                        "quality_counts": quality_counts,
                        "multi_sentence_normalized_count": normalized_count,
                        "not_specific_count": not_specific_count,
                        "specific_count": accepted_count,
                        "configured_model": generated.configured_model,
                        "actual_model": generated.actual_model,
                        "response_format": generated.response_format,
                        "max_output_tokens": _ADVICE_MAX_OUTPUT_TOKENS,
                        "request_attempts": generated.request_attempts,
                        "structure_retries": generated.structure_retries,
                        "finish_reason": generated.finish_reason,
                        "response_metadata": generated.response_metadata,
                    }
            except (LlmClientError, ValidationError, ValueError, TypeError) as exc:
                report = {
                    "status": "FAILED",
                    "failure_stage": "ADVICE_CANARY",
                    "source_task_id": source_task_id,
                    "risk_count": risk_count,
                    **_safe_error(exc),
                }
    finally:
        report = {
            **report,
            "llm_http_calls": transport.http_calls,
            "llm_status_counts": dict(sorted(transport.statuses.items())),
            "llm_call_metadata": transport.safe_call_metadata,
        }
        await transport.close_all()
        await engine.dispose()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-task-id", default=SOURCE_RESULT_TASK_ID)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args.output, source_task_id=args.source_task_id))
    except Exception:
        report = {
            "status": "FAILED",
            "failure_stage": "CANARY_SETUP",
            "failure_code": "CANARY_SETUP_ERROR",
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("status") in {"SUCCEEDED", "RECOVERABLE"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
