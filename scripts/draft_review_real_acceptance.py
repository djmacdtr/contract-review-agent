"""Run exactly one host-side three-file DRAFT_REVIEW acceptance task."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import threading
import time
from datetime import UTC, datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.llm.openai_client import OpenAIContractLlmClient
from app.core.config import Settings
from app.core.enums import EventType, FileRole, TaskStage, TaskStatus, TaskType
from app.db.models import CheckTask, ExtractionCheckpoint, TaskEvent, TaskFile, TaskResult
from app.documents.parsers import ParserRegistry
from app.documents.router import DocumentParsingRouter
from app.draft_review.checkpoints import SqlAlchemyExtractionCheckpointStore
from app.schemas.files import RemoteFile
from app.services.task_service import TaskService
from app.worker.runner import WorkerRunner
from app.workflows.draft_review import DraftReviewWorkflowExecutor
from app.workflows.router import WorkflowRouter
from scripts.draft_review_llm_readiness import CountingTransport

SAMPLE_DIR = Path(r"D:\work\contract_review\脱敏真实合同")
FILES = (
    ("融资租赁合同（回租）.docx", "TARGET"),
    ("融资租赁合同（回租）模版.docx", "TEMPLATE"),
    ("项目方案确认函.docx", "REFERENCE"),
)
BASELINE_TASK_ID = "tsk_01M0Z48FK9QFS0J83HV14GMNP0"
BASELINE_FILE_IDS = {
    "0": "fil_01M0Z48FKA6DTSQ621NAEECXMK",
    "1": "fil_01M0Z48FKKF5J3K4V4R8NMHD3J",
    "2": "fil_01M0Z48FKKF5J3K4V4R8NMHD3K",
}
HISTORICAL_BASELINE_METRICS = (
    Path(".real-diagnostic-temp") / "single-model-recovery-20260826-172503.jsonl"
)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return None


class TimedWorkerRunner(WorkerRunner):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.stage_events: list[tuple[str, float]] = []

    async def _progress(self, task_id: str, stage: Any, progress: int, message: str) -> None:
        now = time.monotonic()
        if not self.stage_events or self.stage_events[-1][0] != stage.value:
            self.stage_events.append((stage.value, now))
        await super()._progress(task_id, stage, progress, message)


def _host_database_url(value: str) -> str:
    url = make_url(value)
    if url.host == "postgres" and url.port == 5432:
        return url.set(host="127.0.0.1", port=15432).render_as_string(
            hide_password=False
        )
    return value


def _claim_once(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump({"status": "STARTED"}, stream)


def _safe_details(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "failure_stage",
        "chain",
        "batch_id",
        "batch_depth",
        "unit_count",
        "failure_code",
        "risk_count",
        "model_advice_count",
        "fallback_advice_count",
        "missing_file_count",
        "unmapped_location_count",
    }
    return {key: value[key] for key in allowed if key in value}


async def _preflight(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    local_hashes = {
        role: hashlib.sha256((SAMPLE_DIR / file_name).read_bytes()).hexdigest()
        for file_name, role in FILES
    }
    async with session_factory() as session:
        active = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(CheckTask)
                    .where(CheckTask.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
                )
            ).scalar_one()
        )
        baseline = (
            (
                await session.execute(
                    select(TaskFile).where(TaskFile.task_id == BASELINE_TASK_ID)
                )
            )
            .scalars()
            .all()
        )
        checkpoint_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ExtractionCheckpoint)
                    .where(
                        ExtractionCheckpoint.task_id == BASELINE_TASK_ID,
                        ExtractionCheckpoint.status == "SUCCEEDED",
                    )
                )
            ).scalar_one()
        )
    baseline_hashes = {
        item.role.value: item.sha256 for item in baseline if item.sha256 is not None
    }
    historical_baseline = False
    if HISTORICAL_BASELINE_METRICS.is_file():
        for line in HISTORICAL_BASELINE_METRICS.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "final_summary" and event.get("status") == "SUCCEEDED":
                historical_baseline = True
                break
    return {
        "active_task_count": active,
        "sample_count": len(local_hashes),
        "baseline_file_count": len(baseline),
        "baseline_checkpoint_count": checkpoint_count,
        "baseline_file_ids": {
            str(item.sort_order): item.id for item in baseline
        },
        "baseline_hashes_match": all(
            baseline_hashes.get(role) == digest for role, digest in local_hashes.items()
        )
        if baseline
        else None,
        "historical_baseline_metrics_available": historical_baseline,
    }


def _stage_durations(
    events: list[tuple[str, float]], finished: float
) -> dict[str, float]:
    output: dict[str, float] = {}
    for index, (stage, started) in enumerate(events):
        ended = events[index + 1][1] if index + 1 < len(events) else finished
        output[stage] = round(output.get(stage, 0.0) + ended - started, 3)
    return output


def _host_session_factory(
    database_url: str,
) -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(_host_database_url(database_url), pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def _ensure_source_anchor(
    session_factory: async_sessionmaker[AsyncSession],
    port: int,
) -> tuple[dict[str, str], bool]:
    """Recreate only the deleted FK anchor needed to read orphaned checkpoints."""

    async with session_factory() as session:
        source = await session.get(CheckTask, BASELINE_TASK_ID)
        if source is not None:
            files = (
                await session.execute(
                    select(TaskFile).where(TaskFile.task_id == BASELINE_TASK_ID)
                )
            ).scalars().all()
            return (
                {str(item.sort_order): item.id for item in files},
                False,
            )
        now = datetime.now(UTC)
        source = CheckTask(
            id=BASELINE_TASK_ID,
            task_type=TaskType.DRAFT_REVIEW,
            client_reference_id="historical-checkpoint-source",
            status=TaskStatus.SUCCEEDED,
            stage=TaskStage.PERSISTING_RESULT,
            stage_message="历史 checkpoint 来源锚点",
            progress=100,
            options={},
            input_snapshot={"files": [], "options": {}},
            request_id="historical-checkpoint-source",
            attempt_count=1,
            max_attempts=1,
            created_at=now,
            updated_at=now,
            finished_at=now,
        )
        source_files: list[TaskFile] = []
        for order, (file_name, role) in enumerate(FILES):
            path = SAMPLE_DIR / file_name
            url = f"http://127.0.0.1:{port}/{quote(file_name)}"
            source_files.append(
                TaskFile(
                    id=BASELINE_FILE_IDS[str(order)],
                    task_id=BASELINE_TASK_ID,
                    role=FileRole[role],
                    reference_type=None,
                    sort_order=order,
                    url=url,
                    safe_url=url,
                    file_name=file_name,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    parse_warnings=[],
                )
            )
        session.add(source)
        session.add_all(source_files)
        session.add(
            TaskEvent(
                task_id=BASELINE_TASK_ID,
                event_type=EventType.COMPLETED,
                stage=TaskStage.PERSISTING_RESULT,
                progress=100,
                message="历史 checkpoint 来源锚点已恢复",
            )
        )
        await session.commit()
        return dict(BASELINE_FILE_IDS), True


def _contextual_advice_count(result: dict[str, Any]) -> int:
    diffs = {
        item.get("diff_id"): item
        for item in result.get("diff_items", [])
        if isinstance(item, dict)
    }
    count = 0
    for risk in result.get("risk_items", []):
        advice = str(risk.get("analysis_advice") or "")
        context: list[str] = [str(risk.get("title") or ""), str(risk.get("description") or "")]
        for diff_id in risk.get("related_diff_ids", []):
            diff = diffs.get(diff_id) or {}
            for side_name in ("baseline", "target"):
                context.append(str((diff.get(side_name) or {}).get("text") or ""))
            context.extend(
                str(segment.get("text") or "")
                for segment in diff.get("segments", [])
                if isinstance(segment, dict)
            )
        compact = "".join(advice.split())
        fragments = {
            "".join(text.split())[start : start + 4]
            for text in context
            for start in range(max(0, len("".join(text.split())) - 3))
        }
        if compact and any(fragment and fragment in compact for fragment in fragments):
            count += 1
    return count


def _result_summary(result: dict[str, Any]) -> dict[str, Any]:
    risks = result.get("risk_items", [])
    passes = result.get("passed_checks", [])
    matrix = result.get("fact_matrix", [])
    metadata = result.get("metadata", {})
    model_runs = metadata.get("model_runs", [])
    advices = [str(item.get("analysis_advice") or "") for item in risks]
    evidence_ok = [
        bool(risk.get("source_evidence"))
        and all(
            evidence.get("text") and evidence.get("location")
            for evidence in risk.get("source_evidence", [])
            if isinstance(evidence, dict)
        )
        for risk in risks
    ]
    comparison = metadata.get("comparison_diagnostics", {})
    return {
        "workflow_version": metadata.get("workflow_version"),
        "rules_version": metadata.get("rules_version"),
        "execution_mode": metadata.get("execution_mode"),
        "risk_count": len(risks),
        "passed_count": len(passes),
        "dynamic_passed_count": sum(
            item.get("module_code") == "FACT_CONSISTENCY" for item in passes
        ),
        "diff_count": len(result.get("diff_items", [])),
        "fact_matrix_count": len(matrix),
        "fact_matrix_status_counts": dict(
            sorted(
                {
                    str(status): sum(item.get("status") == status for item in matrix)
                    for status in {item.get("status") for item in matrix}
                }.items()
            )
        ),
        "template_comparison_reliable": comparison.get("reliable"),
        "all_risks_have_grounded_evidence": all(evidence_ok),
        "nonempty_advice_count": sum(bool(item.strip()) for item in advices),
        "distinct_advice_count": len(set(advices)),
        "contextual_advice_count": _contextual_advice_count(result),
        "advice_coverage": metadata.get("advice_coverage", {}),
        "model_run_count": len(model_runs),
        "model_request_attempts": sum(
            int(item.get("request_attempts", 0) or 0) for item in model_runs
        ),
        "checkpoint_reused_run_count": sum(
            bool(item.get("checkpoint_reused")) for item in model_runs
        ),
        "page_enriched_file_count": sum(
            isinstance(item.get("page_count"), int) for item in result.get("files", [])
        ),
    }


async def run(api_base_url: str) -> dict[str, Any]:
    started = time.monotonic()
    base = Settings()
    host_engine, session_factory = _host_session_factory(base.DATABASE_URL)
    preflight = await _preflight(session_factory)
    if (
        preflight["active_task_count"]
        or preflight["sample_count"] != 3
        or (
            preflight["baseline_file_count"] != 3
            and not preflight["historical_baseline_metrics_available"]
        )
        or preflight["baseline_checkpoint_count"] <= 0
        or preflight["baseline_hashes_match"] is False
    ):
        await host_engine.dispose()
        return {"status": "BLOCKED", "reason_code": "PREFLIGHT_FAILED", "preflight": preflight}

    settings = base.model_copy(
        update={
            "DATABASE_URL": _host_database_url(base.DATABASE_URL),
            "TEMP_ROOT": str(Path(".real-diagnostic-temp") / "worker-temp"),
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
        await host_engine.dispose()
        return {
            "status": "BLOCKED",
            "reason_code": "HOST_EXTERNAL_SERVICE_NOT_CONFIGURED",
            "preflight": preflight,
        }

    handler = partial(QuietHandler, directory=str(SAMPLE_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    task_id: str | None = None
    llm_transport = CountingTransport()
    runner: TimedWorkerRunner | None = None
    try:
        port = int(server.server_address[1])
        source_file_ids, source_anchor_created = await _ensure_source_anchor(
            session_factory, port
        )
        urls = {
            role: f"http://127.0.0.1:{port}/{quote(file_name)}"
            for file_name, role in FILES
        }
        names = {role: file_name for file_name, role in FILES}
        task_files = [
            (
                FileRole[role],
                RemoteFile(url=urls[role], file_name=names[role]),
                order,
            )
            for order, (file_name, role) in enumerate(FILES)
        ]
        async with session_factory() as session:
            accepted = await TaskService(session, settings)._create(
                TaskType.DRAFT_REVIEW,
                f"draft-review-glm-5-3-flash-{int(time.time())}",
                {
                    "ignore_formatting": True,
                    "ignore_headers_footers": True,
                    "check_blank_fields": True,
                    "check_numeric_consistency": True,
                },
                task_files,
                request_id="draft-review-glm-5-3-flash-acceptance",
                source_task_id=BASELINE_TASK_ID,
                checkpoint_source_file_ids=source_file_ids,
            )
            task_id = accepted.task_id

        local_parser = ParserRegistry(
            pdf_min_text_chars_per_page=settings.PDF_MIN_TEXT_CHARS_PER_PAGE
        )
        document_router = DocumentParsingRouter(
            local=local_parser,
            external=None,
            docx_page_location_enabled=False,
        )
        draft = DraftReviewWorkflowExecutor(
            settings,
            document_router=document_router,
            llm=OpenAIContractLlmClient(settings, transport=llm_transport),
            checkpoint_store=SqlAlchemyExtractionCheckpointStore(session_factory),
        )
        runner = TimedWorkerRunner(
            settings,
            workflow=WorkflowRouter(settings, draft_review=draft),
            session_factory=session_factory,
        )
        claimed = await runner.run_once()
        finished = time.monotonic()
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
            "elapsed_seconds": round(finished - started, 3),
            "stage_durations_seconds": _stage_durations(runner.stage_events, finished),
            "preflight": preflight,
            "source_anchor_created": source_anchor_created,
            "llm_http_calls": llm_transport.http_calls,
            "llm_status_counts": dict(sorted(llm_transport.statuses.items())),
            "ocr_http_calls": 0,
            "ocr_status_counts": {},
        }
        if stored is not None:
            report["result"] = _result_summary(stored.result)
        else:
            report.update(
                {
                    "error_code": task.error_code,
                    "first_failure": _safe_details(task.error_details),
                }
            )
        return report
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        await llm_transport.close_all()
        await host_engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    _claim_once(args.lock)
    result = asyncio.run(run(args.api_base_url))
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    raise SystemExit(0 if result.get("status") == "SUCCEEDED" else 2)
