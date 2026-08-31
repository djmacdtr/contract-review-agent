"""Run one host-side Advice-only regeneration for a FINAL_COMPARE report.

This is an internal operator entry point.  It reads the successful source
result, creates one private child task, and lets the standard WorkerRunner
route that child to the Advice-only workflow.  It never downloads documents,
re-runs comparison/page work, or calls the public retry endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.adapters.llm.openai_client import OpenAIContractLlmClient
from app.core.config import get_settings
from app.core.enums import TaskStatus
from app.core.ids import new_request_id
from app.db.models import CheckTask, TaskResult
from app.results.advice_batches import generate_advice_in_batches
from app.schemas.results import TaskResultData
from app.services.task_service import TaskService
from app.worker.runner import WorkerRunner
from app.workflows.final_compare_advice_regeneration import (
    FINAL_COMPARE_ADVICE_REGENERATION_VERSION,
    FinalCompareAdviceRegenerationWorkflowExecutor,
)
from app.workflows.router import WorkflowRouter

SOURCE_TASK_ID = "tsk_01M1BBHY5424N69QRDFA8N96VZ"


def host_database_url(value: str) -> str:
    url = make_url(value)
    if url.host == "postgres" and url.port == 5432:
        return url.set(host="127.0.0.1", port=15432).render_as_string(hide_password=False)
    return url.render_as_string(hide_password=False)


class CountingTransport(httpx.AsyncBaseTransport):
    """Count status/finish metadata without retaining response content."""

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self.http_calls = 0
        self.status_counts: dict[str, int] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.http_calls += 1
        response = await self._inner.handle_async_request(request)
        key = str(response.status_code)
        self.status_counts[key] = self.status_counts.get(key, 0) + 1
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def docker_service_action(action: str, service: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "compose", action, service],
        capture_output=True,
        text=True,
        check=False,
    )
    return {"action": action, "service": service, "return_code": completed.returncode}


def docker_worker_running() -> bool:
    completed = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", "contract-review-worker-1"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip().casefold() == "true"


async def read_task(session_factory, task_id: str) -> tuple[CheckTask | None, TaskResult | None]:
    async with session_factory() as session:
        task = await session.get(CheckTask, task_id)
        result = await session.get(TaskResult, task_id)
        return task, result


def safe_task_result(result: dict[str, Any]) -> dict[str, Any]:
    metadata = result.get("metadata") or {}
    advice = metadata.get("advice_coverage") or {}
    return {
        "risk_count": len(result.get("risk_items", [])),
        "diff_count": len(result.get("diff_items", [])),
        "passed_count": len(result.get("passed_checks", [])),
        "page_coverage": {
            "required": sum(
                1
                for diff in result.get("diff_items", [])
                for side in ("baseline", "target")
                if isinstance(diff.get(side), dict) and diff.get(side)
            ),
            "page_counts": {
                str(item.get("file_id")): item.get("page_count")
                for item in result.get("files", [])
                if item.get("file_id")
            },
        },
        "advice_coverage": {
            key: advice.get(key)
            for key in (
                "risk_count",
                "accepted_count",
                "model_count",
                "fallback_count",
                "model_rate",
                "fallback_rate",
                "logical_call_count",
                "batch_count",
                "quality_rejections",
            )
            if key in advice
        },
        "warning_codes": [
            item.get("code")
            for item in result.get("warnings", [])
            if isinstance(item, dict) and item.get("code")
        ],
    }


async def run_advice_canary(
    source_result: dict[str, Any],
    llm: OpenAIContractLlmClient,
) -> tuple[dict[str, Any], Any]:
    """Run the single production-shaped eight-risk canary.

    The first request may omit or reject items.  The coordinator is allowed
    one four-item recovery request, so this is the same two-call gate used by
    the production batch path.
    """

    canary_result = deepcopy(source_result)
    canary_result["risk_items"] = canary_result.get("risk_items", [])[:8]
    for risk in canary_result["risk_items"]:
        risk["analysis_advice"] = None
    stats = await generate_advice_in_batches(
        canary_result,
        llm,
        max_logical_calls=2,
        max_concurrency=1,
        require_dynamic_anchor=True,
    )
    return canary_result, stats


def canary_passed(stats: Any) -> bool:
    def value(name: str, default: Any = None) -> Any:
        if isinstance(stats, dict):
            return stats.get(name, default)
        return getattr(stats, name, default)

    quality_rejections = value("quality_rejections", {})
    if not isinstance(quality_rejections, dict):
        return False
    unsafe_rejections = {
        key: count
        for key, count in quality_rejections.items()
        if key != "NOT_SPECIFIC" and count
    }
    return (
        value("risk_count") == 8
        and value("accepted_count", 0) >= 7
        and value("fallback_count", 0) <= 1
        and not unsafe_rejections
        and not value("failure_codes", {})
    )


def load_reusable_canary(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("source_task_id") != SOURCE_TASK_ID:
        return None
    if payload.get("version") != FINAL_COMPARE_ADVICE_REGENERATION_VERSION:
        return None
    canary = payload.get("canary")
    if not isinstance(canary, dict) or not canary_passed(canary):
        return None
    if payload.get("task_creation_count") != 0 or payload.get("ocr_calls") != 0:
        return None
    if canary.get("max_logical_calls") != 2:
        return None
    return canary


async def run(args: argparse.Namespace) -> dict[str, Any]:
    base = get_settings()
    database_url = host_database_url(base.DATABASE_URL)
    settings = base.model_copy(
        update={
            "DATABASE_URL": database_url,
            "LLM_ENABLED": True,
            "LLM_RESPONSE_FORMAT": "json_schema",
            "LLM_NATIVE_STRUCTURED_OUTPUT": True,
            "LLM_MAX_CONCURRENCY": 2,
            "LLM_HTTP_RETRY_ATTEMPTS": 1,
            "WORKER_MAX_CONCURRENT_TASKS": 1,
            "TEMP_ROOT": str(Path(".real-diagnostic-temp") / "final-compare-advice"),
        }
    )
    engine = create_async_engine(database_url, pool_pre_ping=True, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    report: dict[str, Any] = {
        "script": "regenerate_final_compare_advice_host",
        "source_task_id": SOURCE_TASK_ID,
        "version": FINAL_COMPARE_ADVICE_REGENERATION_VERSION,
        "task_creation_count": 0,
        "ocr_calls": 0,
        "comparison_calls": 0,
    }
    docker_stopped = False
    try:
        source, source_result = await read_task(session_factory, SOURCE_TASK_ID)
        if source is None or source_result is None:
            report.update({"status": "BLOCKED", "failure_code": "SOURCE_NOT_FOUND"})
            return report
        if source.status != TaskStatus.SUCCEEDED or source_result is None:
            report.update({"status": "BLOCKED", "failure_code": "SOURCE_NOT_SUCCEEDED"})
            return report
        try:
            TaskResultData.model_validate(source_result.result)
        except (TypeError, ValueError):
            report.update({"status": "BLOCKED", "failure_code": "SOURCE_RESULT_INVALID"})
            return report

        if docker_worker_running():
            stop = docker_service_action("stop", "worker")
            report["docker_worker_stop"] = stop
            docker_stopped = stop["return_code"] == 0
            if docker_worker_running():
                report.update({"status": "BLOCKED", "failure_code": "DOCKER_WORKER_STILL_RUNNING"})
                return report

        transport = CountingTransport()
        llm = OpenAIContractLlmClient(
            settings,
            transport=transport,
            advice_response_format_override="json_object",
        )
        if args.reuse_canary_report is not None:
            canary = load_reusable_canary(args.reuse_canary_report)
            if canary is None:
                report.update(
                    {
                        "status": "BLOCKED",
                        "failure_code": "ADVICE_CANARY_EVIDENCE_INVALID",
                        "task_creation_count": 0,
                    }
                )
                return report
            report["canary"] = {**canary, "reused": True}
        else:
            _canary_result, canary_stats = await run_advice_canary(source_result.result, llm)
            report["canary"] = {
                "risk_count": canary_stats.risk_count,
                **canary_stats.as_dict(),
                "http_calls": transport.http_calls,
                "status_counts": dict(sorted(transport.status_counts.items())),
                "max_logical_calls": 2,
            }
        if not canary_passed(report["canary"]):
            report.update(
                {
                    "status": "BLOCKED",
                    "failure_code": "ADVICE_CANARY_COVERAGE_INCOMPLETE",
                    "task_creation_count": 0,
                }
            )
            return report
        if args.canary_only:
            report.update(
                {
                    "status": "SUCCEEDED",
                    "task_creation_count": 0,
                    "ocr_calls": 0,
                    "comparison_calls": 0,
                }
            )
            return report

        before_full_http_calls = transport.http_calls
        prepared_result = deepcopy(source_result.result)
        full_started = time.monotonic()
        full_stats = await generate_advice_in_batches(
            prepared_result,
            llm,
            require_dynamic_anchor=True,
        )
        report["full_preflight"] = {
            **full_stats.as_dict(),
            "elapsed_seconds": round(time.monotonic() - full_started, 3),
            "http_calls": transport.http_calls - before_full_http_calls,
        }
        full_model_rate = full_stats.as_dict()["model_rate"]
        if full_model_rate < 0.95:
            report.update(
                {
                    "status": "BLOCKED",
                    "failure_code": "ADVICE_PREPUBLISH_QUALITY_GATE",
                    "task_creation_count": 0,
                }
            )
            return report

        async with session_factory() as session:
            accepted = await TaskService(session, settings).create_final_advice_regeneration(
                SOURCE_TASK_ID,
                new_request_id(),
            )
        report["task_id"] = accepted.task_id
        report["task_creation_count"] = 1
        before_task_http_calls = transport.http_calls

        advice_workflow = FinalCompareAdviceRegenerationWorkflowExecutor(
            settings,
            session_factory=session_factory,
            llm=llm,
            prepared_results={SOURCE_TASK_ID: prepared_result},
        )
        router = WorkflowRouter(
            settings,
            session_factory=session_factory,
            final_compare_advice_regeneration=advice_workflow,
        )
        runner = WorkerRunner(
            settings,
            workflow=router,
            session_factory=session_factory,
        )
        started = time.monotonic()
        claimed = await runner.run_once()
        task_http_calls = transport.http_calls - before_task_http_calls
        task, stored = await read_task(session_factory, accepted.task_id)
        report.update(
            {
                "status": task.status.value if task else "FAILED",
                "worker_claimed": claimed,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "llm_http_calls": transport.http_calls,
                "preflight_http_calls": before_task_http_calls,
                "task_http_calls": task_http_calls,
                "llm_status_counts": dict(sorted(transport.status_counts.items())),
                "ocr_calls": 0,
                "comparison_calls": 0,
                "console_tasks_path": "/console/#/tasks",
                "console_report_path": f"/console/#/tasks/{accepted.task_id}/report",
            }
        )
        if stored is not None:
            report["result"] = safe_task_result(stored.result)
        elif task is not None:
            report["failure_code"] = task.error_code
            report["failure_details"] = task.error_details
        return report
    except Exception as exc:  # noqa: BLE001 - operator output remains safe
        report.update({"status": "FAILED", "failure_code": type(exc).__name__})
        return report
    finally:
        if "transport" in locals():
            await transport.aclose()
        if docker_stopped:
            report["docker_worker_restore"] = docker_service_action("start", "worker")
        await engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tmp/final-compare-advice-regeneration.json"),
    )
    parser.add_argument("--canary-only", action="store_true")
    parser.add_argument(
        "--reuse-canary-report",
        type=Path,
        help="reuse a previously recorded safe Canary result without another LLM call",
    )
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    result = asyncio.run(run(arguments))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
