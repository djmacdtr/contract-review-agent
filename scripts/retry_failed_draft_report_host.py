"""Run one host-side retry for a failed DRAFT_REVIEW task.

This operator script calls the public retry endpoint exactly once after
read-only preflight, then claims the accepted task with a host-side worker so
local file URLs and the external LLM are reachable from the same host. It
prints only safe counters and never persists a full result or response.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.document_parser.cached_parser import (
    CachedExternalDocumentParser,
    SqlAlchemyDocumentParseCache,
)
from app.adapters.document_parser.textin_parser import TextInDocumentParser
from app.adapters.llm.openai_client import OpenAIContractLlmClient
from app.core.config import Settings
from app.core.enums import TaskStatus
from app.db.models import CheckTask, ExtractionCheckpoint, TaskFile, TaskResult
from app.documents.page_location_cache import SqlAlchemyPageLocationSidecarCache
from app.documents.parsers import ParserRegistry
from app.documents.router import DocumentParsingRouter
from app.draft_review.checkpoints import SqlAlchemyExtractionCheckpointStore
from app.worker.runner import WorkerRunner
from app.workflows.draft_review import DraftReviewWorkflowExecutor
from app.workflows.router import WorkflowRouter
from scripts.draft_review_llm_readiness import CountingTransport

SAMPLE_DIR = Path(r"D:\work\contract_review\脱敏真实合同")
SAFE_RESULT_KEYS = {
    "failure_stage",
    "chain",
    "batch_id",
    "batch_depth",
    "unit_count",
    "failure_code",
    "underlying_failure_code",
    "public_evidence_file_id",
    "public_evidence_location",
    "required_evidence_count",
    "covered_evidence_count",
    "missing_evidence_count",
}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return None


def host_database_url(value: str) -> str:
    url = make_url(value)
    if url.host == "postgres" and url.port == 5432:
        return url.set(host="127.0.0.1", port=15432).render_as_string(
            hide_password=False
        )
    return url.render_as_string(hide_password=False)


def safe_details(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in SAFE_RESULT_KEYS if key in value}


def safe_model_counts(result: dict[str, Any]) -> dict[str, int]:
    runs = result.get("metadata", {}).get("model_runs", [])
    if not isinstance(runs, list):
        return {"model_run_count": 0, "request_attempts": 0, "checkpoint_reused": 0}
    return {
        "model_run_count": len(runs),
        "request_attempts": sum(
            int(item.get("request_attempts", 0) or 0)
            for item in runs
            if isinstance(item, dict)
        ),
        "checkpoint_reused": sum(
            bool(item.get("checkpoint_reused"))
            for item in runs
            if isinstance(item, dict)
        ),
    }


def safe_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    model_counts = safe_model_counts(result)
    advice_coverage = result.get("metadata", {}).get("advice_coverage", {})
    files = result.get("files", [])
    page_summary = {
        "files_with_page_count": sum(
            isinstance(item, dict) and isinstance(item.get("page_count"), int)
            for item in files
        ),
        "file_count": len(files),
        "diff_sides": 0,
        "diff_sides_with_page": 0,
    }
    for diff in result.get("diff_items", []):
        if not isinstance(diff, dict):
            continue
        for side_name in ("baseline", "target"):
            side = diff.get(side_name)
            if not isinstance(side, dict) or not side:
                continue
            page_summary["diff_sides"] += 1
            if isinstance((side.get("location") or {}).get("page"), int):
                page_summary["diff_sides_with_page"] += 1
    return {
        **model_counts,
        "workflow_version": result.get("metadata", {}).get("workflow_version"),
        "rules_version": result.get("metadata", {}).get("rules_version"),
        "diff_count": len(result.get("diff_items", [])),
        "risk_count": len(result.get("risk_items", [])),
        "passed_count": len(result.get("passed_checks", [])),
        "fact_matrix_count": len(result.get("fact_matrix", [])),
        "nonempty_advice_count": sum(
            bool(str(item.get("analysis_advice") or "").strip())
            for item in result.get("risk_items", [])
            if isinstance(item, dict)
        ),
        "advice_coverage": {
            key: advice_coverage.get(key)
            for key in (
                "risk_count",
                "model_count",
                "fallback_count",
                "model_rate",
                "fallback_rate",
            )
            if key in advice_coverage
        },
        "page_summary": page_summary,
    }


async def run(
    source_task_id: str,
    api_base_url: str,
    output: Path,
    *,
    text_response_format: str = "json_schema",
    text_model_override: str | None = None,
    numeric_model_override: str | None = None,
    advice_response_format: str = "json_object",
) -> dict[str, Any]:
    base = Settings()
    engine = create_async_engine(host_database_url(base.DATABASE_URL), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    server: ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None
    transport: CountingTransport | None = None
    started = time.monotonic()
    try:
        async with session_factory() as session:
            source = await session.get(CheckTask, source_task_id)
            active_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(CheckTask)
                        .where(CheckTask.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
                    )
                ).scalar_one()
            )
            existing_retry_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(CheckTask)
                        .where(CheckTask.source_task_id == source_task_id)
                    )
                ).scalar_one()
            )
            source_files = (
                (await session.execute(select(TaskFile).where(TaskFile.task_id == source_task_id)))
                .scalars()
                .all()
            )
            source_checkpoint_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(ExtractionCheckpoint)
                        .where(
                            ExtractionCheckpoint.task_id == source_task_id,
                            ExtractionCheckpoint.status == "SUCCEEDED",
                        )
                    )
                ).scalar_one()
            )
        if source is None or source.status != TaskStatus.FAILED:
            return {"status": "BLOCKED", "reason_code": "SOURCE_NOT_FAILED"}
        if active_count or existing_retry_count:
            return {
                "status": "BLOCKED",
                "reason_code": "RECOVERY_NOT_UNIQUE",
                "active_task_count": active_count,
                "existing_retry_count": existing_retry_count,
            }
        if len(source_files) != 3 or any(
            not (SAMPLE_DIR / item.file_name).is_file() for item in source_files
        ):
            return {"status": "BLOCKED", "reason_code": "SOURCE_FILES_NOT_AVAILABLE"}
        source_ports = {
            urlsplit(str(item.url)).port
            for item in source_files
            if urlsplit(str(item.url)).hostname in {"127.0.0.1", "localhost"}
        }
        if len(source_ports) != 1 or None in source_ports:
            return {"status": "BLOCKED", "reason_code": "SOURCE_URL_NOT_LOCAL"}
        port = next(iter(source_ports))
        assert port is not None

        server = ThreadingHTTPServer(
            ("127.0.0.1", port), partial(QuietHandler, directory=str(SAMPLE_DIR))
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        async with session_factory() as session, session.begin():
            for item in source_files:
                url = f"http://127.0.0.1:{port}/{quote(item.file_name)}"
                await session.execute(
                    update(TaskFile)
                    .where(TaskFile.id == item.id)
                    .values(url=url, safe_url=url)
                )

        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.post(
                f"{api_base_url.rstrip('/')}/api/v1/tasks/{source_task_id}/retry"
            )
        if response.status_code != 202:
            return {
                "status": "FAILED",
                "failure_stage": "RETRY_REQUEST",
                "failure_code": "RETRY_HTTP_STATUS",
                "http_status": response.status_code,
            }
        response_body = response.json()
        accepted = response_body.get("data") if isinstance(response_body, dict) else None
        task_id = accepted.get("task_id") if isinstance(accepted, dict) else None
        if not isinstance(task_id, str) or not task_id:
            return {
                "status": "FAILED",
                "failure_stage": "RETRY_REQUEST",
                "failure_code": "RETRY_RESPONSE_INVALID",
            }

        settings = base.model_copy(
            update={
                "DATABASE_URL": host_database_url(base.DATABASE_URL),
                "TEMP_ROOT": str(Path(".real-diagnostic-temp") / "numeric-schema-recovery"),
                "ALLOW_HTTP_DOWNLOADS": True,
                "DOWNLOAD_HOST_ALLOWLIST": "127.0.0.1",
                "LLM_ENABLED": True,
                "LLM_MAX_CONCURRENCY": 1,
                "LLM_EXTRACTION_TASK_CONCURRENCY": 1,
                "LLM_RESPONSE_FORMAT": "json_schema",
                "LLM_NATIVE_STRUCTURED_OUTPUT": True,
                "DOCX_PAGE_LOCATION_ENABLED": True,
                "OCR_ENABLED": False,
            }
        )
        parser = DocumentParsingRouter(
            local=ParserRegistry(
                pdf_min_text_chars_per_page=settings.PDF_MIN_TEXT_CHARS_PER_PAGE
            ),
            external=CachedExternalDocumentParser(
                TextInDocumentParser(settings),
                SqlAlchemyDocumentParseCache(session_factory),
                settings,
            ),
            page_location_cache=SqlAlchemyPageLocationSidecarCache(session_factory),
            docx_page_location_enabled=True,
        )
        transport = CountingTransport()
        draft = DraftReviewWorkflowExecutor(
            settings,
            document_router=parser,
            llm=OpenAIContractLlmClient(
                settings,
                transport=transport,
                text_response_format_override=text_response_format,
                text_model_override=text_model_override,
                numeric_model_override=numeric_model_override,
                advice_response_format_override=advice_response_format,
            ),
            checkpoint_store=SqlAlchemyExtractionCheckpointStore(session_factory),
        )
        claimed = await WorkerRunner(
            settings,
            workflow=WorkflowRouter(settings, draft_review=draft),
            session_factory=session_factory,
        ).run_once()
        async with session_factory() as session:
            task = await session.get(CheckTask, task_id)
            stored = await session.get(TaskResult, task_id)
            new_checkpoint_count = int(
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
        if task is None:
            return {
                "status": "FAILED",
                "failure_stage": "RECOVERY_RESULT",
                "failure_code": "TASK_MISSING",
            }
        report: dict[str, Any] = {
            "status": task.status.value,
            "task_id": task_id,
            "source_task_id": source_task_id,
            "source_checkpoint_count": source_checkpoint_count,
            "new_task_checkpoint_count": new_checkpoint_count,
            "worker_claimed": claimed,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "llm_http_calls": transport.http_calls,
            "llm_status_counts": dict(sorted(transport.statuses.items())),
            "text_response_format": text_response_format,
            "text_model_override": text_model_override,
            "numeric_model_override": numeric_model_override,
            "advice_response_format": advice_response_format,
            "llm_call_metadata": transport.safe_call_metadata,
            "console_tasks_path": "/console/#/tasks",
            "console_report_path": f"/console/#/tasks/{task_id}/report",
        }
        if stored is not None:
            report["result"] = safe_result_summary(stored.result)
        else:
            report.update(
                {
                    "stage": task.stage.value,
                    "progress": task.progress,
                    "error_code": task.error_code,
                    "failure": safe_details(task.error_details),
                }
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        if transport is not None:
            await transport.close_all()
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-task-id", required=True)
    parser.add_argument("--api-base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--text-response-format",
        choices=("json_object", "json_schema"),
        default="json_schema",
    )
    parser.add_argument("--text-model")
    parser.add_argument("--numeric-model")
    parser.add_argument(
        "--advice-response-format",
        choices=("json_object", "json_schema"),
        default="json_object",
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(
            run(
                args.source_task_id,
                args.api_base_url,
                args.output,
                text_response_format=args.text_response_format,
                text_model_override=args.text_model,
                numeric_model_override=args.numeric_model,
                advice_response_format=args.advice_response_format,
            )
        )
    except Exception:
        report = {
            "status": "FAILED",
            "failure_stage": "RECOVERY_SETUP",
            "failure_code": "RECOVERY_SETUP_ERROR",
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("status") == "SUCCEEDED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
