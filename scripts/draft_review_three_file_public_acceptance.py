"""Create and execute one fresh public three-file DRAFT_REVIEW task.

This is an acceptance harness, not a recovery path. It uses the public API to
create the task, the normal WorkerRunner/DraftReviewWorkflowExecutor path to
execute it, and only content-addressed OCR/page caches. It never reads or
merges an old task result or fact checkpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import threading
import time
from collections import Counter
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.document_parser.cached_parser import (
    OCR_CACHE_OWNER,
    OCR_CACHE_VERSION,
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
from app.documents.page_locations import (
    PAGE_LOCATION_CACHE_OWNER,
    PAGE_LOCATION_CACHE_VERSION,
    page_location_cache_identity,
)
from app.documents.parsers import ParserRegistry
from app.documents.router import DocumentParsingRouter
from app.draft_review.checkpoints import SqlAlchemyExtractionCheckpointStore
from app.worker.runner import WorkerRunner
from app.workflows.draft_review import DraftReviewWorkflowExecutor
from app.workflows.router import WorkflowRouter

SAMPLE_DIR = Path(r"D:\work\contract_review\脱敏真实合同")
FILES = (
    ("融资租赁合同（回租）.docx", "TARGET"),
    ("融资租赁合同（回租）模版.docx", "TEMPLATE"),
    ("项目方案确认函.docx", "REFERENCE"),
)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return None


class CountingTransport(httpx.AsyncBaseTransport):
    """Record only transport counts/statuses and response finish reasons."""

    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport(retries=0)
        self.http_calls = 0
        self.statuses: Counter[int] = Counter()
        self.finish_reasons: Counter[str] = Counter()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.http_calls += 1
        response = await self.inner.handle_async_request(request)
        try:
            body = await response.aread()
            self.statuses[response.status_code] += 1
            try:
                payload = json.loads(body)
                choices = payload.get("choices") if isinstance(payload, dict) else None
                finish = (
                    choices[0].get("finish_reason")
                    if isinstance(choices, list)
                    and choices
                    and isinstance(choices[0], dict)
                    else None
                )
                if isinstance(finish, str):
                    self.finish_reasons[finish] += 1
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=body,
                request=request,
                extensions=response.extensions,
            )
        finally:
            await response.aclose()

    async def aclose(self) -> None:
        return None

    async def close_all(self) -> None:
        await self.inner.aclose()


class TimedWorkerRunner(WorkerRunner):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.stage_events: list[tuple[str, float]] = []

    async def _progress(
        self, task_id: str, stage: Any, progress: int, message: str
    ) -> None:
        if not self.stage_events or self.stage_events[-1][0] != stage.value:
            self.stage_events.append((stage.value, time.monotonic()))
        await super()._progress(task_id, stage, progress, message)


def host_database_url(value: str) -> str:
    url = make_url(value)
    if url.host == "postgres" and url.port == 5432:
        return url.set(host="127.0.0.1", port=15432).render_as_string(
            hide_password=False
        )
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def exclusive_lock(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump({"started_at": time.time(), "purpose": "fresh_public_task"}, stream)


async def host_session_factory(
    settings: Settings,
) -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(host_database_url(settings.DATABASE_URL), pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def cache_preflight(
    session_factory: async_sessionmaker[AsyncSession], hashes: dict[str, str]
) -> dict[str, Any]:
    async with session_factory() as session:
        active_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(CheckTask)
                    .where(CheckTask.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
                )
            ).scalar_one()
        )
        ocr_hits: dict[str, bool] = {}
        sidecar_hits: dict[str, bool] = {}
        for role, file_sha in hashes.items():
            ocr_hits[role] = bool(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(ExtractionCheckpoint)
                        .where(
                            ExtractionCheckpoint.task_id == OCR_CACHE_OWNER,
                            ExtractionCheckpoint.file_sha256 == file_sha,
                            ExtractionCheckpoint.extraction_version == OCR_CACHE_VERSION,
                            ExtractionCheckpoint.status == "SUCCEEDED",
                        )
                    )
                ).scalar_one()
            )
            batch_id, payload_digest = page_location_cache_identity(file_sha)
            sidecar_hits[role] = bool(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(ExtractionCheckpoint)
                        .where(
                            ExtractionCheckpoint.task_id == PAGE_LOCATION_CACHE_OWNER,
                            ExtractionCheckpoint.file_sha256 == file_sha,
                            ExtractionCheckpoint.batch_id == batch_id,
                            ExtractionCheckpoint.extraction_version
                            == PAGE_LOCATION_CACHE_VERSION,
                            ExtractionCheckpoint.payload_digest == payload_digest,
                            ExtractionCheckpoint.status == "SUCCEEDED",
                        )
                    )
                ).scalar_one()
            )
    return {
        "active_task_count": active_count,
        "sha256": hashes,
        "ocr_cache_hits": ocr_hits,
        "page_sidecar_hits": sidecar_hits,
        "ocr_cache_complete": all(ocr_hits.values()),
        "page_sidecar_complete": all(sidecar_hits.values()),
    }


def safe_error(details: Any) -> dict[str, Any]:
    if not isinstance(details, dict):
        return {}
    allowed = {
        "failure_stage",
        "failure_code",
        "underlying_failure_code",
        "component",
        "missing_file_count",
        "unmapped_location_count",
        "required_evidence_count",
        "covered_evidence_count",
        "missing_evidence_count",
    }
    return {key: details[key] for key in allowed if key in details}


def result_summary(result: dict[str, Any]) -> dict[str, Any]:
    risks = result.get("risk_items", [])
    passes = result.get("passed_checks", [])
    diffs = result.get("diff_items", [])
    metadata = result.get("metadata", {})
    model_runs = metadata.get("model_runs", [])
    model_advice = sum(
        1
        for risk in risks
        if isinstance(risk, dict) and risk.get("analysis_advice")
    )
    return {
        "risk_count": len(risks),
        "passed_count": len(passes),
        "diff_count": len(diffs),
        "model_advice_count": model_advice,
        "fallback_advice_count": len(risks) - model_advice,
        "advice_coverage": round(model_advice / len(risks), 6) if risks else 1.0,
        "fact_matrix_count": len(result.get("fact_matrix", [])),
        "model_run_count": len(model_runs),
        "page_location_coverage": metadata.get("page_location_coverage", {}),
        "cross_candidate_stats": metadata.get("cross_candidate_stats", {}),
    }


async def execute(api_base_url: str, output: Path) -> dict[str, Any]:
    started = time.monotonic()
    base = Settings()
    hashes = {
        role: sha256(SAMPLE_DIR / file_name) for file_name, role in FILES
    }
    engine, session_factory = await host_session_factory(base)
    preflight = await cache_preflight(session_factory, hashes)
    if preflight["active_task_count"]:
        await engine.dispose()
        return {"status": "BLOCKED", "reason_code": "ACTIVE_TASKS", "preflight": preflight}
    if not preflight["page_sidecar_complete"]:
        await engine.dispose()
        return {
            "status": "BLOCKED",
            "reason_code": "PAGE_SIDECAR_CACHE_INCOMPLETE",
            "preflight": preflight,
        }
    settings = base.model_copy(
        update={
            "DATABASE_URL": host_database_url(base.DATABASE_URL),
            "TEMP_ROOT": str(Path(".real-diagnostic-temp") / "fresh-public-worker"),
            "ALLOW_HTTP_DOWNLOADS": True,
            "DOWNLOAD_HOST_ALLOWLIST": "127.0.0.1",
            "LLM_ENABLED": True,
            "LLM_EXTRACTION_MODEL": "GLM-5.3-Flash",
            "LLM_REVIEW_MODEL": "GLM-5.3-Flash",
            "LLM_ADVICE_MODEL": "GLM-5.3-Flash",
            "LLM_RESPONSE_FORMAT": "json_schema",
            "LLM_NATIVE_STRUCTURED_OUTPUT": True,
            "LLM_MAX_CONCURRENCY": 2,
            "LLM_EXTRACTION_TASK_CONCURRENCY": 2,
            "LLM_HTTP_RETRY_ATTEMPTS": 1,
            "LLM_FACT_REVIEW_ENABLED": False,
            "LLM_MAPPING_REVIEW_ENABLED": False,
            "LLM_SEMANTIC_PLAN_ENABLED": False,
            "DOCX_PAGE_LOCATION_ENABLED": True,
            "OCR_ENABLED": True,
        }
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), lambda *args, **kwargs: QuietHandler(
            *args, directory=str(SAMPLE_DIR), **kwargs
        )
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    llm_transport = CountingTransport()
    ocr_transport = CountingTransport()
    task_id: str | None = None
    runner: TimedWorkerRunner | None = None
    try:
        port = int(server.server_address[1])
        urls = {
            role: f"http://127.0.0.1:{port}/{quote(file_name, safe='')}"
            for file_name, role in FILES
        }
        payload = {
            "client_reference_id": f"fresh-draft-review-{int(time.time())}",
            "target_file": {
                "url": urls["TARGET"],
                "file_name": FILES[0][0],
                "mime_type": (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                ),
            },
            "template_file": {
                "url": urls["TEMPLATE"],
                "file_name": FILES[1][0],
                "mime_type": (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                ),
            },
            "reference_files": [
                {
                    "url": urls["REFERENCE"],
                    "file_name": FILES[2][0],
                    "mime_type": (
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    ),
                }
            ],
            "options": {
                "ignore_formatting": True,
                "ignore_headers_footers": True,
                "check_blank_fields": True,
                "check_numeric_consistency": True,
            },
        }
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.post(f"{api_base_url}/api/v1/draft-reviews", json=payload)
        if response.status_code != 202:
            return {
                "status": "BLOCKED",
                "reason_code": "PUBLIC_CREATE_FAILED",
                "http_status": response.status_code,
                "preflight": preflight,
            }
        accepted = response.json().get("data") or {}
        task_id = accepted.get("task_id")
        if not isinstance(task_id, str):
            return {"status": "BLOCKED", "reason_code": "PUBLIC_CREATE_NO_TASK_ID"}
        async with session_factory() as session:
            task = (
                await session.execute(select(CheckTask).where(CheckTask.id == task_id))
            ).scalar_one()
            task_identity = {
                "source_task_id": task.source_task_id,
                "legacy_option_keys": sorted(
                    key
                    for key in (task.options or {})
                    if key.startswith("_") or "legacy" in key.casefold()
                ),
                "task_file_ids": [
                    row.id
                    for row in (
                        await session.execute(
                            select(TaskFile)
                            .where(TaskFile.task_id == task_id)
                            .order_by(TaskFile.sort_order)
                        )
                    ).scalars().all()
                ],
            }
        local_parser = ParserRegistry(
            pdf_min_text_chars_per_page=settings.PDF_MIN_TEXT_CHARS_PER_PAGE
        )
        ocr_client = TextInDocumentParserClient(settings, transport=ocr_transport)
        document_router = DocumentParsingRouter(
            local=local_parser,
            external=CachedExternalDocumentParser(
                TextInDocumentParser(settings, client=ocr_client),
                SqlAlchemyDocumentParseCache(session_factory),
                settings,
            ),
            page_location_cache=SqlAlchemyPageLocationSidecarCache(session_factory),
            docx_page_location_enabled=True,
        )
        draft = DraftReviewWorkflowExecutor(
            settings,
            document_router=document_router,
            llm=OpenAIContractLlmClient(
                settings,
                transport=llm_transport,
                text_response_format_override="json_object",
                advice_response_format_override="json_object",
            ),
            checkpoint_store=SqlAlchemyExtractionCheckpointStore(session_factory),
        )
        runner = TimedWorkerRunner(
            settings,
            workflow=WorkflowRouter(settings, draft_review=draft),
            session_factory=session_factory,
        )
        claimed = await runner.run_once()
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            detail_response = await client.get(f"{api_base_url}/api/v1/tasks/{task_id}")
            result_response = await client.get(
                f"{api_base_url}/api/v1/tasks/{task_id}/result"
            ) if claimed else None
        async with session_factory() as session:
            task = (
                await session.execute(select(CheckTask).where(CheckTask.id == task_id))
            ).scalar_one()
            stored = (
                await session.execute(select(TaskResult).where(TaskResult.task_id == task_id))
            ).scalar_one_or_none()
        report: dict[str, Any] = {
            "status": task.status.value,
            "task_id": task_id,
            "stage": task.stage.value,
            "progress": task.progress,
            "worker_claimed": claimed,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "task_identity": task_identity,
            "preflight": preflight,
            "ocr_http_calls": ocr_transport.http_calls,
            "ocr_status_counts": dict(sorted(ocr_transport.statuses.items())),
            "llm_http_calls": llm_transport.http_calls,
            "llm_status_counts": dict(sorted(llm_transport.statuses.items())),
            "llm_finish_reasons": dict(sorted(llm_transport.finish_reasons.items())),
            "mapping_calls": sum(
                1
                for stage, _started in (runner.stage_events if runner else [])
                if stage == "CROSS_VALIDATE"
            ),
            "api_get_status": detail_response.status_code,
            "api_get_result": result_response.status_code if result_response else None,
        }
        if stored is not None:
            report["result"] = result_summary(stored.result)
        else:
            report["error_code"] = task.error_code
            report["first_failure"] = safe_error(task.error_details)
        return report
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        await llm_transport.close_all()
        await ocr_transport.close_all()
        await engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    exclusive_lock(arguments.lock)
    result = asyncio.run(execute(arguments.api_base_url, arguments.output))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if result.get("status") == "SUCCEEDED" else 2)
