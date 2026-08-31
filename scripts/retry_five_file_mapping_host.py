"""Run exactly one host-side retry for the failed five-file acceptance task.

This operator entrypoint uses the public retry route once, then executes the
new child with the normal WorkerRunner and DraftReviewWorkflowExecutor.  It
does not retry the child and only writes aggregate, redacted diagnostics.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from collections import Counter
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.document_parser.cached_parser import (
    CachedExternalDocumentParser,
    SqlAlchemyDocumentParseCache,
)
from app.adapters.document_parser.textin_client import TextInDocumentParserClient
from app.adapters.document_parser.textin_parser import TextInDocumentParser
from app.adapters.llm.openai_client import OpenAIContractLlmClient
from app.core.config import Settings
from app.core.enums import TaskStatus
from app.db.models import CheckTask, ExtractionCheckpoint, TaskFile, TaskResult
from app.documents.page_location_cache import SqlAlchemyPageLocationSidecarCache
from app.documents.parsers import ParserRegistry
from app.documents.router import DocumentParsingRouter
from app.draft_review.checkpoints import SqlAlchemyExtractionCheckpointStore
from app.workflows.draft_review import DraftReviewWorkflowExecutor
from app.workflows.router import WorkflowRouter
from scripts.draft_review_five_file_public_acceptance import (
    FILE_ROOT,
    FILE_SPECS,
    QuietHandler,
    TimedWorkerRunner,
    host_database_url,
    runtime_settings,
)
from scripts.draft_review_llm_readiness import CountingTransport

SOURCE_TASK_ID = "tsk_01M16W32545DN9NC65XXEPJG1D"


def _safe_error(details: Any) -> dict[str, Any]:
    if not isinstance(details, dict):
        return {}
    allowed = {
        "failure_stage",
        "chain",
        "file_id",
        "batch_id",
        "batch_depth",
        "unit_count",
        "failure_code",
        "underlying_failure_code",
        "request_attempts",
        "structure_retries",
        "finish_reason",
        "content_chars",
        "reasoning_content_chars",
        "max_tokens",
        "http_status",
        "usage",
        "candidate_pair_count",
        "public_evidence_file_id",
        "public_evidence_location",
        "required_evidence_count",
        "covered_evidence_count",
        "missing_evidence_count",
    }
    return {key: details[key] for key in allowed if key in details}


def _source_file_url(port: int, index: int) -> str:
    relative = FILE_SPECS[index]["path"].relative_to(FILE_ROOT).as_posix()
    return f"http://127.0.0.1:{port}/{quote(relative, safe='/')}"


async def _preflight(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    async with factory() as session:
        source = await session.get(CheckTask, SOURCE_TASK_ID)
        active_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(CheckTask)
                    .where(CheckTask.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
                )
            ).scalar_one()
        )
        child_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(CheckTask)
                    .where(CheckTask.source_task_id == SOURCE_TASK_ID)
                )
            ).scalar_one()
        )
        file_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(TaskFile)
                    .where(TaskFile.task_id == SOURCE_TASK_ID)
                )
            ).scalar_one()
        )
    return {
        "source_status": source.status.value if source is not None else None,
        "active_task_count": active_count,
        "existing_child_count": child_count,
        "source_file_count": file_count,
        "passed": (
            source is not None
            and source.status == TaskStatus.FAILED
            and active_count == 0
            and child_count == 0
            and file_count == len(FILE_SPECS)
        ),
    }


async def _task_snapshot(
    factory: async_sessionmaker[AsyncSession], task_id: str
) -> tuple[Any, list[TaskFile], TaskResult | None, int]:
    async with factory() as session:
        task = await session.get(CheckTask, task_id)
        files = list(
            (
                await session.execute(
                    select(TaskFile)
                    .where(TaskFile.task_id == task_id)
                    .order_by(TaskFile.sort_order)
                )
            )
            .scalars()
            .all()
        )
        result = await session.get(TaskResult, task_id)
        checkpoint_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ExtractionCheckpoint)
                    .where(
                        ExtractionCheckpoint.task_id == task_id,
                        ExtractionCheckpoint.status == "SUCCEEDED",
                    )
                )
            ).scalar_one()
        )
    return task, files, result, checkpoint_count


def _result_summary(result: dict[str, Any]) -> dict[str, Any]:
    metadata = result.get("metadata", {})
    runs = metadata.get("model_runs", [])
    purpose_counts: Counter[str] = Counter()
    request_attempts = 0
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict):
            continue
        purpose = run.get("purpose")
        if isinstance(purpose, str):
            purpose_counts[purpose] += 1
        request_attempts += int(run.get("request_attempts", 0) or 0)
    advice = metadata.get("advice_coverage", {})
    return {
        "workflow_version": metadata.get("workflow_version"),
        "rules_version": metadata.get("rules_version"),
        "file_count": len(result.get("files", [])),
        "diff_count": len(result.get("diff_items", [])),
        "risk_count": len(result.get("risk_items", [])),
        "passed_count": len(result.get("passed_checks", [])),
        "fact_matrix_count": len(result.get("fact_matrix", [])),
        "model_run_purpose_counts": dict(sorted(purpose_counts.items())),
        "model_run_request_attempts": request_attempts,
        "advice_coverage": {
            key: advice.get(key)
            for key in (
                "risk_count",
                "model_count",
                "fallback_count",
                "model_rate",
                "fallback_rate",
            )
            if isinstance(advice, dict) and key in advice
        },
        "nonempty_advice_count": sum(
            bool(str(item.get("analysis_advice") or "").strip())
            for item in result.get("risk_items", [])
            if isinstance(item, dict)
        ),
    }


async def run(api_base_url: str, output: Path) -> dict[str, Any]:
    base_settings = runtime_settings(Settings())
    engine = create_async_engine(host_database_url(base_settings.DATABASE_URL), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    server: ThreadingHTTPServer | None = None
    server_thread: threading.Thread | None = None
    llm_transport: CountingTransport | None = None
    ocr_transport: CountingTransport | None = None
    runner_task: asyncio.Task[bool] | None = None
    started = time.monotonic()
    task_id: str | None = None
    try:
        preflight = await _preflight(factory)
        if not preflight["passed"]:
            report = {"status": "BLOCKED", "preflight": preflight}
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            return report

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            partial(QuietHandler, directory=str(FILE_ROOT)),
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        port = int(server.server_address[1])
        async with factory() as session, session.begin():
            source_files = list(
                (
                    await session.execute(
                        select(TaskFile)
                        .where(TaskFile.task_id == SOURCE_TASK_ID)
                        .order_by(TaskFile.sort_order)
                    )
                )
                .scalars()
                .all()
            )
            for index, source_file in enumerate(source_files):
                url = _source_file_url(port, index)
                await session.execute(
                    update(TaskFile)
                    .where(TaskFile.id == source_file.id)
                    .values(url=url, safe_url=url)
                )

        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.post(
                f"{api_base_url.rstrip('/')}/api/v1/tasks/{SOURCE_TASK_ID}/retry"
            )
        if response.status_code != 202:
            report = {
                "status": "FAILED",
                "failure_stage": "RETRY_REQUEST",
                "failure_code": "RETRY_HTTP_STATUS",
                "http_status": response.status_code,
                "preflight": preflight,
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            return report
        accepted = response.json().get("data")
        task_id = accepted.get("task_id") if isinstance(accepted, dict) else None
        if not isinstance(task_id, str) or not task_id:
            report = {
                "status": "FAILED",
                "failure_stage": "RETRY_REQUEST",
                "failure_code": "RETRY_RESPONSE_INVALID",
                "preflight": preflight,
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            return report

        settings = base_settings.model_copy(
            update={
                "ALLOW_HTTP_DOWNLOADS": True,
                "DOWNLOAD_HOST_ALLOWLIST": "127.0.0.1",
                "LLM_ENABLED": True,
                "LLM_MAX_CONCURRENCY": 2,
                "LLM_EXTRACTION_TASK_CONCURRENCY": 2,
                "LLM_HTTP_RETRY_ATTEMPTS": 1,
                "LLM_RESPONSE_FORMAT": "json_schema",
                "LLM_NATIVE_STRUCTURED_OUTPUT": True,
                "DOCX_PAGE_LOCATION_ENABLED": True,
                "OCR_ENABLED": True,
                "OCR_HTTP_RETRY_ATTEMPTS": 0,
                "LLM_FACT_REVIEW_ENABLED": False,
                "LLM_MAPPING_REVIEW_ENABLED": False,
                "LLM_SEMANTIC_PLAN_ENABLED": False,
            }
        )
        llm_transport = CountingTransport()
        ocr_transport = CountingTransport()
        parser = DocumentParsingRouter(
            local=ParserRegistry(
                pdf_min_text_chars_per_page=settings.PDF_MIN_TEXT_CHARS_PER_PAGE
            ),
            external=CachedExternalDocumentParser(
                TextInDocumentParser(
                    settings,
                    client=TextInDocumentParserClient(settings, transport=ocr_transport),
                ),
                SqlAlchemyDocumentParseCache(factory),
                settings,
            ),
            page_location_cache=SqlAlchemyPageLocationSidecarCache(factory),
            docx_page_location_enabled=True,
        )
        draft = DraftReviewWorkflowExecutor(
            settings,
            document_router=parser,
            llm=OpenAIContractLlmClient(
                settings,
                transport=llm_transport,
                text_response_format_override="json_object",
                advice_response_format_override="json_object",
            ),
            checkpoint_store=SqlAlchemyExtractionCheckpointStore(factory),
        )
        runner = TimedWorkerRunner(
            settings,
            workflow=WorkflowRouter(settings, draft_review=draft),
            session_factory=factory,
        )
        runner_task = asyncio.create_task(runner.run_once())
        last_status: str | None = None
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            while not runner_task.done():
                detail = await client.get(f"{api_base_url.rstrip('/')}/api/v1/tasks/{task_id}")
                if detail.status_code == 200:
                    current = (detail.json().get("data") or {}).get("status")
                    if current != last_status:
                        last_status = current
                await asyncio.sleep(1)
        worker_claimed = await runner_task
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            detail_response = await client.get(f"{api_base_url.rstrip('/')}/api/v1/tasks/{task_id}")
            result_response = await client.get(
                f"{api_base_url.rstrip('/')}/api/v1/tasks/{task_id}/result"
            )
        task, task_files, stored, checkpoint_count = await _task_snapshot(factory, task_id)
        report = {
            "status": task.status.value,
            "task_id": task_id,
            "source_task_id": SOURCE_TASK_ID,
            "worker_claimed": worker_claimed,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "preflight": preflight,
            "checkpoint_count": checkpoint_count,
            "ocr_http_calls": ocr_transport.http_calls,
            "ocr_status_counts": dict(sorted(ocr_transport.statuses.items())),
            "llm_http_calls": llm_transport.http_calls,
            "llm_status_counts": dict(sorted(llm_transport.statuses.items())),
            "llm_finish_reasons": dict(
                sorted(
                    Counter(
                        call.get("finish_reason")
                        for call in llm_transport.safe_call_metadata
                        if isinstance(call.get("finish_reason"), str)
                    ).items()
                )
            ),
            "llm_call_metadata": llm_transport.safe_call_metadata,
            "api_get_status": detail_response.status_code,
            "api_get_result_status": result_response.status_code,
            "stage_events": [stage for stage, _ in runner.stage_events],
            "task_file_count": len(task_files),
            "console_tasks_path": "/console/#/tasks",
            "console_report_path": f"/console/#/tasks/{task_id}/report",
        }
        if stored is not None:
            report["result"] = _result_summary(stored.result)
        else:
            report.update(
                {
                    "stage": task.stage.value,
                    "progress": task.progress,
                    "error_code": task.error_code,
                    "failure": _safe_error(task.error_details),
                }
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    finally:
        if runner_task is not None and not runner_task.done():
            runner_task.cancel()
            await asyncio.gather(runner_task, return_exceptions=True)
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5)
        if llm_transport is not None:
            await llm_transport.close_all()
        if ocr_transport is not None:
            await ocr_transport.close_all()
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".real-diagnostic-temp/retry-five-file-mapping-20260829.json"),
    )
    args = parser.parse_args()
    report = asyncio.run(run(args.api_base_url, args.output))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("status") == "SUCCEEDED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
