"""Restore a console-visible DRAFT_REVIEW report from orphaned checkpoints.

This is an operator-only recovery utility. It recreates a minimal source task for
the deleted historical task, creates one retry task through the normal service,
and runs that task through the normal worker/repository persistence path.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import threading
from datetime import UTC, datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url

from app.adapters.llm.openai_client import OpenAIContractLlmClient
from app.core.config import Settings
from app.core.enums import EventType, FileRole, TaskStage, TaskStatus, TaskType
from app.db.models import CheckTask, ExtractionCheckpoint, TaskEvent, TaskFile, TaskResult
from app.db.session import SessionFactory
from app.documents.parsers import ParserRegistry
from app.documents.router import DocumentParsingRouter
from app.draft_review.checkpoints import SqlAlchemyExtractionCheckpointStore
from app.services.task_service import TaskService
from app.worker.runner import WorkerRunner
from app.workflows.draft_review import DraftReviewWorkflowExecutor
from app.workflows.router import WorkflowRouter

SOURCE_TASK_ID = "tsk_01M0Z48FK9QFS0J83HV14GMNP0"
SAMPLE_DIR = Path(r"D:\work\contract_review\脱敏真实合同")
FILES = (
    (
        "融资租赁合同（回租）.docx",
        FileRole.TARGET,
        0,
        "fil_01M0Z48FKA6DTSQ621NAEECXMK",
    ),
    (
        "融资租赁合同（回租）模版.docx",
        FileRole.TEMPLATE,
        1,
        "fil_01M0Z48FKKF5J3K4V4R8NMHD3J",
    ),
    (
        "项目方案确认函.docx",
        FileRole.REFERENCE,
        2,
        "fil_01M0Z48FKKF5J3K4V4R8NMHD3K",
    ),
)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return None


def _host_database_url(value: str) -> str:
    url = make_url(value)
    if url.host == "postgres" and url.port == 5432:
        return url.set(host="127.0.0.1", port=15432).render_as_string(
            hide_password=False
        )
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _preflight() -> dict[str, Any]:
    expected_hashes = {_sha256(SAMPLE_DIR / name) for name, *_ in FILES}
    async with SessionFactory() as session:
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
        rows = (
            await session.execute(
                select(
                    ExtractionCheckpoint.file_sha256,
                    ExtractionCheckpoint.extraction_version,
                    func.count(),
                )
                .where(
                    ExtractionCheckpoint.task_id == SOURCE_TASK_ID,
                    ExtractionCheckpoint.status == "SUCCEEDED",
                )
                .group_by(
                    ExtractionCheckpoint.file_sha256,
                    ExtractionCheckpoint.extraction_version,
                )
            )
        ).all()
    checkpoint_hashes = {str(row[0]) for row in rows}
    return {
        "source_task_exists": source is not None,
        "active_task_count": active_count,
        "checkpoint_count": sum(int(row[2]) for row in rows),
        "checkpoint_hashes_match": checkpoint_hashes == expected_hashes,
        "checkpoint_versions": {
            f"{row[0]}:{row[1]}": int(row[2]) for row in rows
        },
    }


async def _create_source_and_retry(port: int, settings: Settings) -> str:
    options = {
        "ignore_formatting": True,
        "ignore_headers_footers": True,
        "check_blank_fields": True,
        "check_numeric_consistency": True,
    }
    source_files: list[TaskFile] = []
    snapshot_files: list[dict[str, Any]] = []
    for file_name, role, sort_order, file_id in FILES:
        path = SAMPLE_DIR / file_name
        url = f"http://127.0.0.1:{port}/{quote(file_name)}"
        source_files.append(
            TaskFile(
                id=file_id,
                task_id=SOURCE_TASK_ID,
                role=role,
                reference_type=None,
                sort_order=sort_order,
                url=url,
                safe_url=url,
                file_name=file_name,
                sha256=_sha256(path),
                parse_warnings=[],
            )
        )
        snapshot_files.append(
            {
                "role": role.value,
                "reference_type": None,
                "safe_url": url,
                "file_name": file_name,
                "mime_type": None,
                "display_name": None,
                "sort_order": sort_order,
            }
        )

    now = datetime.now(UTC)
    source = CheckTask(
        id=SOURCE_TASK_ID,
        task_type=TaskType.DRAFT_REVIEW,
        client_reference_id="historical-checkpoint-source-20260826",
        status=TaskStatus.FAILED,
        stage=TaskStage.FACT_EXTRACTION,
        stage_message="历史恢复来源，仅用于复用已验证抽取结果",
        progress=75,
        options=options,
        input_snapshot={"files": snapshot_files, "options": options},
        request_id="historical-report-recovery-source",
        attempt_count=1,
        max_attempts=2,
        error_code="HISTORICAL_RECORD_DELETED",
        error_message="原任务行已删除，当前行为恢复来源锚点",
        created_at=now,
        updated_at=now,
        finished_at=now,
    )
    async with SessionFactory() as session, session.begin():
        session.add(source)
        session.add_all(source_files)
        session.add(
            TaskEvent(
                task_id=SOURCE_TASK_ID,
                event_type=EventType.FAILED,
                stage=TaskStage.FACT_EXTRACTION,
                progress=75,
                message="历史恢复来源已建立",
            )
        )

    async with SessionFactory() as session:
        accepted = await TaskService(session, settings).retry(
            SOURCE_TASK_ID,
            request_id="historical-report-recovery",
        )
        return accepted.task_id


async def _remove_source_anchor() -> None:
    async with SessionFactory() as session, session.begin():
        await session.execute(delete(CheckTask).where(CheckTask.id == SOURCE_TASK_ID))


async def run(output: Path) -> dict[str, Any]:
    preflight = await _preflight()
    if (
        preflight["source_task_exists"]
        or preflight["active_task_count"]
        or preflight["checkpoint_count"] != 114
        or not preflight["checkpoint_hashes_match"]
    ):
        return {"status": "BLOCKED", "preflight": preflight}

    base = Settings()
    settings = base.model_copy(
        update={
            "DATABASE_URL": _host_database_url(base.DATABASE_URL),
            "TEMP_ROOT": str(Path(".real-diagnostic-temp") / "restore-worker-temp"),
            "ALLOW_HTTP_DOWNLOADS": True,
            "DOWNLOAD_HOST_ALLOWLIST": "127.0.0.1",
            "LLM_ENABLED": True,
            "LLM_MAX_CONCURRENCY": 3,
            "LLM_EXTRACTION_TASK_CONCURRENCY": 3,
            "LLM_RESPONSE_FORMAT": "json_schema",
            "LLM_NATIVE_STRUCTURED_OUTPUT": True,
            "LLM_FACT_REVIEW_ENABLED": False,
            "LLM_MAPPING_REVIEW_ENABLED": False,
            "LLM_SEMANTIC_PLAN_ENABLED": False,
            "DOCX_PAGE_LOCATION_ENABLED": False,
            "OCR_ENABLED": False,
        }
    )
    if not settings.llm_configured:
        return {"status": "BLOCKED", "reason": "LLM_NOT_CONFIGURED", "preflight": preflight}

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietHandler, directory=str(SAMPLE_DIR))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    task_id: str | None = None
    try:
        task_id = await _create_source_and_retry(int(server.server_address[1]), settings)
        parser = DocumentParsingRouter(
            local=ParserRegistry(
                pdf_min_text_chars_per_page=settings.PDF_MIN_TEXT_CHARS_PER_PAGE
            ),
            external=None,
            docx_page_location_enabled=False,
        )
        draft = DraftReviewWorkflowExecutor(
            settings,
            document_router=parser,
            llm=OpenAIContractLlmClient(settings),
            checkpoint_store=SqlAlchemyExtractionCheckpointStore(SessionFactory),
        )
        runner = WorkerRunner(
            settings,
            workflow=WorkflowRouter(settings, draft_review=draft),
        )
        claimed = await runner.run_once()
        async with SessionFactory() as session:
            task = await session.get(CheckTask, task_id)
            stored = await session.get(TaskResult, task_id)
        if task is None:
            raise RuntimeError("recovery task disappeared")
        report: dict[str, Any] = {
            "status": task.status.value,
            "task_id": task_id,
            "stage": task.stage.value,
            "progress": task.progress,
            "worker_claimed": claimed,
            "preflight": preflight,
            "error_code": task.error_code,
            "error_details": task.error_details,
        }
        if stored is not None:
            result = stored.result
            statistics = result.get("summary", {}).get("statistics", {})
            report["result_summary"] = {
                "risk_count": statistics.get("risk_count"),
                "review_count": statistics.get("review_count"),
                "diff_count": len(result.get("diff_items", [])),
                "passed_count": len(result.get("passed_checks", [])),
                "fact_matrix_count": len(result.get("fact_matrix", [])),
                "nonempty_advice_count": sum(
                    bool(str(item.get("analysis_advice") or "").strip())
                    for item in result.get("risk_items", [])
                ),
            }
            full_result_path = output.with_name(f"{task_id}_result.json")
            full_result_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            report["full_result_path"] = str(full_result_path.resolve())
            await _remove_source_anchor()
        return report
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    result = asyncio.run(run(args.output))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result.get("status") == "SUCCEEDED" else 2)
