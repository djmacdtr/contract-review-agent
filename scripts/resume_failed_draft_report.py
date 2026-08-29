"""Resume one failed DRAFT_REVIEW from its current-version checkpoints."""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import delete, func, select, update

from app.adapters.llm.openai_client import OpenAIContractLlmClient
from app.core.config import Settings
from app.core.enums import TaskStatus
from app.db.models import CheckTask, ExtractionCheckpoint, TaskFile, TaskResult
from app.db.session import SessionFactory
from app.documents.parsers import ParserRegistry
from app.documents.router import DocumentParsingRouter
from app.draft_review.checkpoints import SqlAlchemyExtractionCheckpointStore
from app.services.task_service import TaskService
from app.worker.runner import WorkerRunner
from app.workflows.draft_review import DraftReviewWorkflowExecutor
from app.workflows.router import WorkflowRouter

SAMPLE_DIR = Path(r"D:\work\contract_review\脱敏真实合同")
HISTORICAL_ANCHOR_ID = "tsk_01M0Z48FK9QFS0J83HV14GMNP0"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return None


async def run(source_task_id: str, output: Path) -> dict[str, Any]:
    settings = Settings().model_copy(
        update={
            "TEMP_ROOT": str(Path(".real-diagnostic-temp") / "resume-worker-temp"),
            "ALLOW_HTTP_DOWNLOADS": True,
            "DOWNLOAD_HOST_ALLOWLIST": "127.0.0.1",
            "LLM_ENABLED": True,
            "LLM_MAX_CONCURRENCY": 3,
            "LLM_EXTRACTION_TASK_CONCURRENCY": 3,
            # Operator-only recovery: checkpoint migration is counted against
            # these guards even when no new model request is made. Keep the
            # production defaults unchanged while allowing this one restore to
            # consume the already completed current-version shards.
            "LLM_EXTRACTION_MAX_LOGICAL_CALLS_TOTAL": 1024,
            "LLM_EXTRACTION_ABSOLUTE_MAX_REQUESTS_PER_DOCUMENT": 512,
            "LLM_EXTRACTION_MAX_LOGICAL_CALLS_TARGET": 128,
            "LLM_RESPONSE_FORMAT": "json_schema",
            "LLM_NATIVE_STRUCTURED_OUTPUT": True,
            "LLM_FACT_REVIEW_ENABLED": False,
            "LLM_MAPPING_REVIEW_ENABLED": False,
            "LLM_SEMANTIC_PLAN_ENABLED": False,
            "DOCX_PAGE_LOCATION_ENABLED": False,
            "OCR_ENABLED": False,
        }
    )
    async with SessionFactory() as session:
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
        checkpoint_count = int(
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
    if source is None or source.status != TaskStatus.FAILED or active_count:
        return {
            "status": "BLOCKED",
            "source_exists": source is not None,
            "source_status": source.status.value if source else None,
            "active_task_count": active_count,
            "checkpoint_count": checkpoint_count,
        }

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietHandler, directory=str(SAMPLE_DIR))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    task_id: str | None = None
    try:
        port = int(server.server_address[1])
        async with SessionFactory() as session, session.begin():
            source_files = (
                (
                    await session.execute(
                        select(TaskFile)
                        .where(TaskFile.task_id == source_task_id)
                        .order_by(TaskFile.sort_order)
                    )
                )
                .scalars()
                .all()
            )
            for item in source_files:
                url = f"http://127.0.0.1:{port}/{quote(item.file_name)}"
                await session.execute(
                    update(TaskFile)
                    .where(TaskFile.id == item.id)
                    .values(url=url, safe_url=url)
                )
        async with SessionFactory() as session:
            accepted = await TaskService(session, settings).retry(
                source_task_id,
                request_id="historical-report-incremental-resume",
            )
            task_id = accepted.task_id

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
        claimed = await WorkerRunner(
            settings,
            workflow=WorkflowRouter(settings, draft_review=draft),
        ).run_once()
        async with SessionFactory() as session:
            task = await session.get(CheckTask, task_id)
            stored = await session.get(TaskResult, task_id)
        if task is None:
            raise RuntimeError("resume task disappeared")
        report: dict[str, Any] = {
            "status": task.status.value,
            "task_id": task_id,
            "stage": task.stage.value,
            "progress": task.progress,
            "worker_claimed": claimed,
            "source_task_id": source_task_id,
            "source_checkpoint_count": checkpoint_count,
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
            async with SessionFactory() as session, session.begin():
                await session.execute(
                    delete(CheckTask).where(
                        CheckTask.id.in_([source_task_id, HISTORICAL_ANCHOR_ID])
                    )
                )
        return report
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-task-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    result = asyncio.run(run(args.source_task_id, args.output))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result.get("status") == "SUCCEEDED" else 2)
