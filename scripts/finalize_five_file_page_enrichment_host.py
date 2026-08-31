"""Finalize the one failed five-file task from its page-free result snapshot.

The tool is intentionally limited to page enrichment.  It reads the immutable
pre-page snapshot and persisted caches in a fresh process, performs a dry-run
by default, and only publishes after an explicit ``--apply``.  It never calls
OCR or an LLM and never alters the source task.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.document_parser.cached_parser import (
    CachedExternalDocumentParser,
    SqlAlchemyDocumentParseCache,
)
from app.core.config import Settings
from app.core.enums import Conclusion, EventType, TaskStage, TaskStatus, TaskType
from app.core.errors import WorkflowError
from app.db.models import CheckTask, ExtractionCheckpoint, TaskEvent, TaskFile, TaskResult
from app.documents.models import ParsedDocument
from app.documents.page_location_cache import SqlAlchemyPageLocationSidecarCache
from app.documents.page_locations import (
    DocxPageLocationSidecar,
    apply_docx_page_location_sidecars,
    rebind_docx_page_location_sidecar,
    validate_docx_page_location_sidecar,
    validate_public_page_coverage,
)
from app.schemas.results import TaskResultData

TASK_ID = "tsk_01M16XN8BFR11RPP7Y4RZR36KE"
PRE_PAGE_RESULT_SNAPSHOT_VERSION = "draft-result-pre-page-v1"


def _host_database_url(value: str) -> str:
    from sqlalchemy.engine import make_url

    url = make_url(value)
    if url.host == "postgres" and url.port == 5432:
        return url.set(host="127.0.0.1", port=15432).render_as_string(hide_password=False)
    return value


def _snapshot_hash(result: dict[str, Any]) -> str:
    return hashlib.md5(json.dumps(result, ensure_ascii=False).encode("utf-8")).hexdigest()


def _safe_error(exc: BaseException) -> dict[str, Any]:
    details = exc.details if isinstance(exc, WorkflowError) else None
    if isinstance(details, dict):
        return {
            key: details[key]
            for key in (
                "failure_stage",
                "failure_code",
                "required_evidence_count",
                "covered_evidence_count",
                "missing_evidence_count",
                "public_evidence_file_id",
                "public_evidence_location",
            )
            if key in details
        }
    return {
        "failure_stage": "PAGE_FINALIZATION",
        "failure_code": exc.code if isinstance(exc, WorkflowError) else type(exc).__name__,
    }


def _file_by_role(files: list[TaskFile]) -> dict[str, TaskFile]:
    if len(files) != 5:
        raise WorkflowError(
            "PAGE_FINALIZATION_BLOCKED",
            "指定任务文件数量无效",
            details={"failure_stage": "TASK_FILE_READ", "failure_code": "TASK_FILE_COUNT_INVALID"},
        )
    by_role: dict[str, TaskFile] = {}
    for file in files:
        role = file.role.value
        if role != "REFERENCE" and role in by_role:
            raise WorkflowError(
                "PAGE_FINALIZATION_BLOCKED",
                "指定任务文件角色重复",
                details={
                    "failure_stage": "TASK_FILE_READ",
                    "failure_code": "TASK_FILE_ROLES_INVALID",
                },
            )
        by_role[role] = file
    reference_count = sum(file.role.value == "REFERENCE" for file in files)
    if reference_count != 3 or set(by_role) != {"TARGET", "TEMPLATE", "REFERENCE"}:
        raise WorkflowError(
            "PAGE_FINALIZATION_BLOCKED",
            "指定任务文件角色不完整",
            details={
                "failure_stage": "TASK_FILE_READ",
                "failure_code": "TASK_FILE_ROLES_INVALID",
                "reference_file_count": reference_count,
            },
        )
    return by_role


async def _read_snapshot(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[CheckTask, list[TaskFile], dict[str, Any]]:
    async with factory() as session:
        task = await session.get(CheckTask, TASK_ID)
        if task is None:
            raise WorkflowError(
                "PAGE_FINALIZATION_BLOCKED",
                "指定任务不存在",
                details={"failure_stage": "TASK_READ", "failure_code": "TASK_NOT_FOUND"},
            )
        if task.task_type != TaskType.DRAFT_REVIEW or task.status != TaskStatus.FAILED:
            raise WorkflowError(
                "PAGE_FINALIZATION_BLOCKED",
                "指定任务不是可收口的失败起草任务",
                details={"failure_stage": "TASK_READ", "failure_code": "TASK_STATE_INVALID"},
            )
        if await session.get(TaskResult, TASK_ID) is not None:
            raise WorkflowError(
                "PAGE_FINALIZATION_ALREADY_DONE",
                "指定任务已经存在结果",
                details={
                    "failure_stage": "IDEMPOTENCY_CHECK",
                    "failure_code": "TASK_RESULT_ALREADY_EXISTS",
                },
            )
        files = list(
            (
                await session.execute(
                    select(TaskFile)
                    .where(TaskFile.task_id == TASK_ID)
                    .order_by(TaskFile.sort_order)
                )
            )
            .scalars()
            .all()
        )
        _file_by_role(files)
        rows = list(
            (
                await session.execute(
                    select(ExtractionCheckpoint).where(
                        ExtractionCheckpoint.task_id == TASK_ID,
                        ExtractionCheckpoint.extraction_version == PRE_PAGE_RESULT_SNAPSHOT_VERSION,
                        ExtractionCheckpoint.status == "SUCCEEDED",
                    )
                )
            )
            .scalars()
            .all()
        )
    if len(rows) != 5 or len({row.file_sha256 for row in rows}) != 5:
        raise WorkflowError(
            "PAGE_FINALIZATION_BLOCKED",
            "页码前结果快照未完整覆盖五份文件",
            details={
                "failure_stage": "SNAPSHOT_READ",
                "failure_code": "PRE_PAGE_SNAPSHOT_INCOMPLETE",
                "snapshot_count": len(rows),
            },
        )
    results = [
        row.value.get("result")
        for row in rows
        if isinstance(row.value, dict) and isinstance(row.value.get("result"), dict)
    ]
    if len(results) != 5 or any(result.get("task_id") != TASK_ID for result in results):
        raise WorkflowError(
            "PAGE_FINALIZATION_BLOCKED",
            "页码前结果快照身份无效",
            details={"failure_stage": "SNAPSHOT_READ", "failure_code": "SNAPSHOT_TASK_ID_INVALID"},
        )
    first_hash = _snapshot_hash(results[0])
    if any(_snapshot_hash(result) != first_hash for result in results[1:]):
        raise WorkflowError(
            "PAGE_FINALIZATION_BLOCKED",
            "五份页码前结果快照内容不一致",
            details={
                "failure_stage": "SNAPSHOT_READ",
                "failure_code": "PRE_PAGE_SNAPSHOT_CONTENT_MISMATCH",
            },
        )
    TaskResultData.model_validate(results[0])
    return task, files, results[0]


async def _load_sidecars(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    files: list[TaskFile],
    result: dict[str, Any],
) -> tuple[dict[str, DocxPageLocationSidecar], dict[str, int]]:
    result_files = {
        item.get("file_id"): item
        for item in result.get("files", [])
        if isinstance(item, dict) and isinstance(item.get("file_id"), str)
    }
    if len(result_files) != 5:
        raise WorkflowError(
            "PAGE_FINALIZATION_BLOCKED",
            "结果文件清单不完整",
            details={"failure_stage": "FILE_REBIND", "failure_code": "RESULT_FILE_COUNT_INVALID"},
        )
    cache = SqlAlchemyPageLocationSidecarCache(factory)
    sidecars: dict[str, DocxPageLocationSidecar] = {}
    page_counts: dict[str, int] = {}
    for file in files:
        item = result_files.get(file.id)
        if item is None or not isinstance(item.get("sha256"), str):
            raise WorkflowError(
                "PAGE_FINALIZATION_BLOCKED",
                "结果文件摘要缺失",
                details={"failure_stage": "FILE_REBIND", "failure_code": "RESULT_FILE_SHA_MISSING"},
            )
        if file.declared_mime_type != "application/pdf":
            sidecar = await cache.load(file_sha256=item["sha256"], file_id=file.id)
            if sidecar is None:
                raise WorkflowError(
                    "DOCX_PAGE_LOCATION_INCOMPLETE",
                    "DOCX 页码 sidecar 缓存未命中",
                    details={
                        "failure_stage": "SIDECAR_READ",
                        "failure_code": "PAGE_SIDECAR_CACHE_MISSING",
                    },
                )
            rebound = rebind_docx_page_location_sidecar(sidecar, file_id=file.id)
            validate_docx_page_location_sidecar(rebound, file_id=file.id)
            sidecars[file.id] = rebound
            page_counts[file.id] = rebound.page_count
        else:
            page_count = item.get("page_count")
            if not isinstance(page_count, int) or page_count < 1:
                raise WorkflowError(
                    "PAGE_FINALIZATION_BLOCKED",
                    "结果文件页数无效",
                    details={
                        "failure_stage": "SIDECAR_READ",
                        "failure_code": "PAGE_COUNT_INVALID",
                    },
                )
            page_counts[file.id] = page_count
    pdf_files = [file for file in files if file.declared_mime_type == "application/pdf"]
    if len(pdf_files) != 1:
        raise WorkflowError(
            "PAGE_FINALIZATION_BLOCKED",
            "PDF 文件数量无效",
            details={"failure_stage": "SIDECAR_READ", "failure_code": "PDF_FILE_COUNT_INVALID"},
        )
    pdf_file = pdf_files[0]
    pdf_item = result_files[pdf_file.id]
    parse_cache = SqlAlchemyDocumentParseCache(factory)
    cached_parser = CachedExternalDocumentParser(None, parse_cache, settings)
    cached = await parse_cache.load(
        file_sha256=pdf_item["sha256"],
        cache_key=cached_parser._cache_key(mode="auto", include_stamp_images=False),
    )
    cached_document = (
        ParsedDocument.model_validate(cached["document"])
        if isinstance(cached, dict) and isinstance(cached.get("document"), dict)
        else None
    )
    if cached_document is None or cached_document.sha256 != pdf_item["sha256"]:
        raise WorkflowError(
            "DOCX_PAGE_LOCATION_INCOMPLETE",
            "PDF 真实页码缓存未命中",
            details={"failure_stage": "SIDECAR_READ", "failure_code": "PDF_PAGE_CACHE_MISSING"},
        )
    if cached_document.page_count != page_counts[pdf_file.id]:
        raise WorkflowError(
            "DOCX_PAGE_LOCATION_INCOMPLETE",
            "PDF 真实页数与结果不一致",
            details={"failure_stage": "SIDECAR_READ", "failure_code": "PDF_PAGE_COUNT_MISMATCH"},
        )
    return sidecars, page_counts


def _enrich_single_page_pdf(result: dict[str, Any], pdf_file_id: str, page_count: int) -> None:
    """Bind a known one-page PDF location to its only physical page.

    This is not pagination estimation: the cached external parser has already
    established that the PDF contains exactly one physical page.
    """

    if page_count != 1:
        return
    for collection_name in ("diff_items", "risk_items"):
        for item in result.get(collection_name, []):
            if not isinstance(item, dict):
                continue
            sides = [item.get(name) for name in ("baseline", "target")]
            if collection_name == "risk_items":
                sides.extend(item.get("source_evidence", []))
            for evidence in sides:
                if not isinstance(evidence, dict):
                    continue
                if (
                    evidence.get("file_id") != pdf_file_id
                    and evidence.get("source_file_id") != pdf_file_id
                ):
                    continue
                locations = evidence.get("locations")
                if not isinstance(locations, list):
                    locations = [evidence.get("location")]
                for location in locations:
                    if isinstance(location, dict) and location.get("page") is None:
                        location["page"] = 1


def _page_enrich(
    source_result: dict[str, Any],
    files: list[TaskFile],
    sidecars: dict[str, DocxPageLocationSidecar],
    page_counts: dict[str, int],
) -> tuple[dict[str, Any], dict[str, int]]:
    result = deepcopy(source_result)
    apply_docx_page_location_sidecars(result, sidecars, strict=True)
    pdf_file = next(file for file in files if file.declared_mime_type == "application/pdf")
    _enrich_single_page_pdf(result, pdf_file.id, page_counts[pdf_file.id])
    coverage = validate_public_page_coverage(result, sidecars)
    TaskResultData.model_validate(result)
    return result, coverage


async def _apply(
    factory: async_sessionmaker[AsyncSession],
    result: dict[str, Any],
    files: list[TaskFile],
    coverage: dict[str, int],
    snapshot_hash: str,
) -> None:
    now = datetime.now(UTC)
    stats = result["summary"]["statistics"]
    async with factory() as session, session.begin():
        task = await session.get(CheckTask, TASK_ID, with_for_update=True)
        if task is None or task.status != TaskStatus.FAILED:
            raise WorkflowError(
                "PAGE_FINALIZATION_BLOCKED",
                "任务状态在发布前发生变化",
                details={
                    "failure_stage": "IDEMPOTENCY_CHECK",
                    "failure_code": "TASK_STATE_CHANGED",
                },
            )
        if await session.get(TaskResult, TASK_ID) is not None:
            raise WorkflowError(
                "PAGE_FINALIZATION_ALREADY_DONE",
                "任务已经存在结果",
                details={
                    "failure_stage": "IDEMPOTENCY_CHECK",
                    "failure_code": "TASK_RESULT_ALREADY_EXISTS",
                },
            )
        task.status = TaskStatus.SUCCEEDED
        task.stage = TaskStage.COMPLETED
        task.stage_message = "页码阶段收口完成"
        task.progress = 100
        task.conclusion = Conclusion(result["conclusion"])
        task.risk_count = stats["risk_count"]
        task.review_count = stats["review_count"]
        task.worker_id = None
        task.heartbeat_at = now
        task.error_code = None
        task.error_message = None
        task.error_details = None
        task.updated_at = now
        task.finished_at = now
        for file in files:
            snapshot_file = next(item for item in result["files"] if item.get("file_id") == file.id)
            await session.execute(
                update(TaskFile)
                .where(TaskFile.task_id == TASK_ID, TaskFile.id == file.id)
                .values(
                    sha256=snapshot_file.get("sha256"),
                    page_count=snapshot_file.get("page_count"),
                    parser_name=snapshot_file.get("parser_name"),
                    parse_status=snapshot_file.get("parse_status"),
                    parse_warnings=snapshot_file.get("parse_warnings", []),
                )
            )
        session.add(
            TaskResult(
                task_id=TASK_ID,
                schema_version=result["schema_version"],
                result=result,
                result_size=len(orjson.dumps(result)),
                rules_version=result["metadata"].get("rules_version"),
                workflow_version=result["metadata"].get("workflow_version"),
                model_name=result["metadata"].get("primary_model"),
            )
        )
        session.add(
            TaskEvent(
                task_id=TASK_ID,
                event_type=EventType.COMPLETED,
                stage=TaskStage.COMPLETED,
                progress=100,
                message="页码阶段收口完成",
                details={
                    "operation": "PAGE_ENRICHMENT_FINALIZE",
                    "snapshot_hash": snapshot_hash,
                    "snapshot_version": PRE_PAGE_RESULT_SNAPSHOT_VERSION,
                    "ocr_calls": 0,
                    "llm_calls": 0,
                    "page_coverage": coverage,
                },
            )
        )


async def run(*, apply: bool, output: Path) -> dict[str, Any]:
    settings = Settings()
    engine = create_async_engine(_host_database_url(settings.DATABASE_URL), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    report: dict[str, Any] = {
        "status": "DRY_RUN" if not apply else "FAILED",
        "task_id": TASK_ID,
        "ocr_calls": 0,
        "llm_calls": 0,
    }
    try:
        task, files, source_result = await _read_snapshot(factory)
        sidecars, page_counts = await _load_sidecars(factory, settings, files, source_result)
        result, coverage = _page_enrich(source_result, files, sidecars, page_counts)
        report.update(
            {
                "status": "APPLIED" if apply else "READY_TO_APPLY",
                "snapshot_hash": _snapshot_hash(source_result),
                "snapshot_version": PRE_PAGE_RESULT_SNAPSHOT_VERSION,
                "sidecar_count": len(sidecars),
                "pdf_page_cache_count": 1,
                "page_counts": page_counts,
                "page_coverage": coverage,
                "risk_count": len(result.get("risk_items", [])),
                "diff_count": len(result.get("diff_items", [])),
                "passed_count": len(result.get("passed_checks", [])),
                "advice_nonempty_count": sum(
                    bool(str(item.get("analysis_advice") or "").strip())
                    for item in result.get("risk_items", [])
                    if isinstance(item, dict)
                ),
                "previous_status": task.status.value,
            }
        )
        if apply:
            await _apply(
                factory,
                result=result,
                files=files,
                coverage=coverage,
                snapshot_hash=report["snapshot_hash"],
            )
    except Exception as exc:
        report.update(_safe_error(exc))
    finally:
        await engine.dispose()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(run(apply=args.apply, output=args.output))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] in {"READY_TO_APPLY", "APPLIED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
