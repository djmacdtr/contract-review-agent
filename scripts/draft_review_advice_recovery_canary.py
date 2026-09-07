"""Run the bounded Advice-only recovery canary against one stored result.

The source result is loaded read-only.  This script never creates a task and never
invokes OCR, extraction, mapping, or page recovery.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.llm.openai_client import LlmClientError, OpenAIContractLlmClient
from app.core.config import Settings
from app.core.reliability import RecoveryBudget
from app.db.models import TaskResult
from app.results.advice import ADVICE_QUALITY_CODES, fallback_analysis_advice
from app.results.advice_batches import ADVICE_DIAGNOSTIC_CODES, generate_advice_in_batches
from scripts.draft_review_llm_readiness import CountingTransport
from scripts.retry_failed_draft_report_host import host_database_url

SOURCE_RESULT_TASK_ID = "tsk_01M1XBQ4G6VJB235GXZJC3JQR6"
EXPECTED_FALLBACK_RISK_IDS = tuple(
    f"risk_diff_{index:06d}" for index in range(14, 20)
)


def _identify_fallback_risk_ids(result: dict[str, Any]) -> list[str]:
    """Identify old deterministic Advice by recomputing the fallback function."""

    identified: list[str] = []
    for risk in result.get("risk_items", []):
        if not isinstance(risk, dict) or not risk.get("risk_id"):
            continue
        existing = str(risk.get("analysis_advice") or "").strip()
        deterministic = fallback_analysis_advice(result, risk).strip()
        if existing and existing == deterministic:
            identified.append(str(risk["risk_id"]))
    return identified


def _select_fallback_result(
    result: dict[str, Any],
    *,
    expected_risk_ids: tuple[str, ...] = EXPECTED_FALLBACK_RISK_IDS,
) -> tuple[dict[str, Any], list[str]]:
    fallback_ids = _identify_fallback_risk_ids(result)
    if tuple(fallback_ids) != expected_risk_ids:
        raise ValueError("ADVICE_CANARY_FALLBACK_RISK_SET_INVALID")
    selected = copy.deepcopy(result)
    selected["risk_items"] = [
        risk
        for risk in selected.get("risk_items", [])
        if str(risk.get("risk_id")) in fallback_ids
    ]
    for risk in selected["risk_items"]:
        risk["analysis_advice"] = None
    selected["advice"] = {}
    return selected, fallback_ids


def _safe_error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, LlmClientError):
        return {
            "failure_code": exc.failure_code or exc.code,
            "request_attempts": exc.request_attempts,
            "structure_retries": exc.structure_retries,
            "finish_reason": exc.finish_reason,
            "content_chars": exc.content_chars,
            "reasoning_content_chars": exc.reasoning_content_chars,
        }
    return {"failure_code": type(exc).__name__}


async def run(
    output: Path,
    *,
    source_task_id: str = SOURCE_RESULT_TASK_ID,
    expected_risk_ids: tuple[str, ...] = EXPECTED_FALLBACK_RISK_IDS,
) -> dict[str, Any]:
    base = Settings()
    engine = create_async_engine(host_database_url(base.DATABASE_URL), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    transport = CountingTransport()
    report: dict[str, Any]
    try:
        async with session_factory() as session:
            stored = await session.get(TaskResult, source_task_id)
        if stored is None or not isinstance(stored.result, dict):
            report = {
                "status": "FAILED",
                "failure_stage": "CANARY_SETUP",
                "failure_code": "SOURCE_RESULT_NOT_FOUND",
                "source_task_id": source_task_id,
            }
        else:
            try:
                selected, fallback_ids = _select_fallback_result(
                    stored.result,
                    expected_risk_ids=expected_risk_ids,
                )
                settings = base.model_copy(
                    update={
                        "LLM_ENABLED": True,
                        "LLM_MAX_CONCURRENCY": 2,
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
                stats = await generate_advice_in_batches(
                    selected,
                    client,
                    batch_size=len(fallback_ids),
                    recovery_batch_size=1,
                    max_concurrency=2,
                    recovery_budget=RecoveryBudget(600),
                    require_dynamic_anchor=True,
                )
                coverage = stats.as_dict()
                quality_counts = {
                    code: int(coverage.get("quality_rejections", {}).get(code, 0))
                    for code in ADVICE_QUALITY_CODES
                }
                diagnostic_counts = {
                    code: int(coverage.get("quality_rejections", {}).get(code, 0))
                    for code in ADVICE_DIAGNOSTIC_CODES
                }
                returned_ids = [
                    str(item.get("risk_id"))
                    for item in selected.get("advice", {}).get("risk_advices", [])
                    if isinstance(item, dict)
                ]
                success = (
                    coverage.get("accepted_count") == len(expected_risk_ids)
                    and coverage.get("fallback_count") == 0
                    and set(returned_ids) == set(expected_risk_ids)
                    and len(returned_ids) == len(set(returned_ids)) == len(expected_risk_ids)
                    and not any(quality_counts.values())
                    and not any(diagnostic_counts.values())
                )
                report = {
                    "status": "SUCCEEDED" if success else "FAILED",
                    "failure_stage": None if success else "ADVICE_CANARY",
                    "failure_code": None if success else "ADVICE_CANARY_QUALITY_GATE",
                    "source_task_id": source_task_id,
                    "target_fallback_risk_ids": list(fallback_ids),
                    "returned_risk_ids": returned_ids,
                    "accepted": coverage.get("accepted_count", 0),
                    "fallback": coverage.get("fallback_count", 0),
                    "quality_counts": quality_counts,
                    "diagnostic_counts": diagnostic_counts,
                    "coverage": coverage,
                    "llm_http_calls": transport.http_calls,
                    "http_statuses": dict(transport.statuses),
                    "safe_call_metadata": list(transport.safe_call_metadata),
                    "ocr_calls": 0,
                    "writes_database": False,
                }
            except Exception as exc:  # noqa: BLE001 - safe diagnostic boundary
                report = {
                    "status": "FAILED",
                    "failure_stage": "ADVICE_CANARY",
                    "source_task_id": source_task_id,
                    **_safe_error(exc),
                    "llm_http_calls": transport.http_calls,
                    "http_statuses": dict(transport.statuses),
                    "ocr_calls": 0,
                    "writes_database": False,
                }
    finally:
        await transport.close_all()
        await engine.dispose()

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-task-id", default=SOURCE_RESULT_TASK_ID)
    parser.add_argument(
        "--expected-risk-ids",
        help="Comma-separated exact fallback risk IDs; defaults to the historical six-risk set.",
    )
    args = parser.parse_args()
    expected_risk_ids = (
        tuple(item.strip() for item in args.expected_risk_ids.split(",") if item.strip())
        if args.expected_risk_ids
        else EXPECTED_FALLBACK_RISK_IDS
    )
    report = asyncio.run(
        run(
            args.output,
            source_task_id=args.source_task_id,
            expected_risk_ids=expected_risk_ids,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
