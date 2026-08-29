"""Finalize one failed draft report from its page-free result snapshot.

This is a deliberately narrow operator tool. It reads the immutable pre-page
snapshot and the current content-addressed sidecars in a fresh process, then
either reports a dry-run or atomically publishes the page-enriched result for
the one explicitly named task. It never downloads files or calls OCR/LLM.
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

from app.core.config import get_settings
from app.core.enums import Conclusion, EventType, TaskStage, TaskStatus, TaskType
from app.core.errors import WorkflowError
from app.db.models import CheckTask, ExtractionCheckpoint, TaskEvent, TaskFile, TaskResult
from app.documents.page_location_cache import SqlAlchemyPageLocationSidecarCache
from app.documents.page_locations import (
    PAGE_LOCATION_CACHE_OWNER,
    PAGE_LOCATION_CACHE_VERSION,
    DocxPageLocationSidecar,
    apply_docx_page_location_sidecars,
    rebind_docx_page_location_sidecar,
    validate_docx_page_location_sidecar,
    validate_public_page_coverage,
)
from app.schemas.results import TaskResultData

TASK_ID = "tsk_01M16QQ6WG4S4D73ME0QF13HSH"
PRE_PAGE_RESULT_SNAPSHOT_VERSION = "draft-result-pre-page-v1"
EXPECTED_SNAPSHOT_HASH = "630a125466ff06d68700e826039d7fce"
SNAPSHOT_BATCH_PREFIX = "page_result_"


def host_database_url(value: str) -> str:
    from sqlalchemy.engine import make_url

    url = make_url(value)
    if url.host == "postgres" and url.port == 5432:
        return url.set(host="127.0.0.1", port=15432).render_as_string(
            hide_password=False
        )
    return value


def snapshot_content_hash(result: dict[str, Any]) -> str:
    """Hash the page-free result with the historical snapshot convention."""

    return hashlib.md5(json.dumps(result, ensure_ascii=False).encode("utf-8")).hexdigest()


def _safe_error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, WorkflowError):
        details = exc.details or {}
        return {
            "failure_stage": details.get("failure_stage", "PAGE_FINALIZATION"),
            "failure_code": details.get("failure_code", exc.code),
        }
    return {
        "failure_stage": "PAGE_FINALIZATION",
        "failure_code": type(exc).__name__,
    }


def _safe_advice_coverage(result: dict[str, Any]) -> dict[str, Any]:
    risks = [item for item in result.get("risk_items", []) if isinstance(item, dict)]
    model = result.get("metadata", {}).get("advice_coverage", {})
    fallback_count = model.get("fallback_count")
    model_count = model.get("model_count")
    return {
        "risk_count": len(risks),
        "nonempty_count": sum(bool(item.get("analysis_advice")) for item in risks),
        "model_count": model_count if isinstance(model_count, int) else None,
        "fallback_count": fallback_count if isinstance(fallback_count, int) else None,
    }


def _task_file_by_role(files: list[TaskFile]) -> dict[str, TaskFile]:
    by_role = {file.role.value: file for file in files}
    if set(by_role) != {"TARGET", "TEMPLATE", "REFERENCE"} or len(files) != 3:
        raise WorkflowError(
            "PAGE_FINALIZATION_BLOCKED",
            "指定任务的三份文件角色不完整",
            details={
                "failure_stage": "TASK_FILE_READ",
                "failure_code": "TASK_FILE_ROLES_INVALID",
            },
        )
    return by_role


def _snapshot_rows_by_sha(
    rows: list[ExtractionCheckpoint],
) -> dict[str, ExtractionCheckpoint]:
    if len(rows) != 3:
        raise WorkflowError(
            "PAGE_FINALIZATION_BLOCKED",
            "页码前结果快照数量不是三份",
            details={
                "failure_stage": "SNAPSHOT_READ",
                "failure_code": "PRE_PAGE_SNAPSHOT_INCOMPLETE",
                "snapshot_count": len(rows),
            },
        )
    by_sha: dict[str, ExtractionCheckpoint] = {}
    for row in rows:
        if row.status != "SUCCEEDED" or not row.batch_id.startswith(SNAPSHOT_BATCH_PREFIX):
            raise WorkflowError(
                "PAGE_FINALIZATION_BLOCKED",
                "页码前结果快照状态无效",
                details={
                    "failure_stage": "SNAPSHOT_READ",
                    "failure_code": "PRE_PAGE_SNAPSHOT_INVALID",
                },
            )
        if row.file_sha256 in by_sha:
            raise WorkflowError(
                "PAGE_FINALIZATION_BLOCKED",
                "页码前结果快照存在重复文件摘要",
                details={
                    "failure_stage": "SNAPSHOT_READ",
                    "failure_code": "PRE_PAGE_SNAPSHOT_DUPLICATE",
                },
            )
        by_sha[row.file_sha256] = row
    return by_sha


async def _read_inputs(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[CheckTask, dict[str, TaskFile], dict[str, Any], dict[str, DocxPageLocationSidecar]]:
    async with session_factory() as session:
        task = await session.get(CheckTask, TASK_ID)
        if task is None:
            raise WorkflowError(
                "PAGE_FINALIZATION_BLOCKED",
                "指定页码收口任务不存在",
                details={"failure_stage": "TASK_READ", "failure_code": "TASK_NOT_FOUND"},
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
        if task.task_type != TaskType.DRAFT_REVIEW or task.status != TaskStatus.FAILED:
            raise WorkflowError(
                "PAGE_FINALIZATION_BLOCKED",
                "指定任务不是可收口的失败起草任务",
                details={
                    "failure_stage": "TASK_READ",
                    "failure_code": "TASK_STATE_INVALID",
                },
            )
        files = list(
            (
                await session.execute(
                    select(TaskFile)
                    .where(TaskFile.task_id == TASK_ID)
                    .order_by(TaskFile.sort_order)
                )
            ).scalars()
        )
        files_by_role = _task_file_by_role(files)
        snapshots = list(
            (
                await session.execute(
                    select(ExtractionCheckpoint)
                    .where(
                        ExtractionCheckpoint.task_id == TASK_ID,
                        ExtractionCheckpoint.extraction_version
                        == PRE_PAGE_RESULT_SNAPSHOT_VERSION,
                        ExtractionCheckpoint.status == "SUCCEEDED",
                    )
                    .order_by(ExtractionCheckpoint.file_sha256)
                )
            ).scalars()
        )
    rows_by_sha = _snapshot_rows_by_sha(snapshots)
    first = next(iter(rows_by_sha.values()))
    value = first.value
    result = value.get("result") if isinstance(value, dict) else None
    if not isinstance(result, dict) or result.get("task_id") != TASK_ID:
        raise WorkflowError(
            "PAGE_FINALIZATION_BLOCKED",
            "页码前结果快照身份无效",
            details={"failure_stage": "SNAPSHOT_READ", "failure_code": "SNAPSHOT_TASK_ID_INVALID"},
        )
    content_hash = snapshot_content_hash(result)
    if content_hash != EXPECTED_SNAPSHOT_HASH:
        raise WorkflowError(
            "PAGE_FINALIZATION_BLOCKED",
            "页码前结果快照内容摘要不一致",
            details={
                "failure_stage": "SNAPSHOT_READ",
                "failure_code": "SNAPSHOT_HASH_MISMATCH",
            },
        )
    for row in rows_by_sha.values():
        row_result = row.value.get("result") if isinstance(row.value, dict) else None
        if not isinstance(row_result, dict) or snapshot_content_hash(row_result) != content_hash:
            raise WorkflowError(
                "PAGE_FINALIZATION_BLOCKED",
                "三份页码前结果快照内容不一致",
                details={
                    "failure_stage": "SNAPSHOT_READ",
                    "failure_code": "PRE_PAGE_SNAPSHOT_CONTENT_MISMATCH",
                },
            )

    result_files = {
        item.get("role"): item
        for item in result.get("files", [])
        if isinstance(item, dict) and item.get("role")
    }
    if set(result_files) != set(files_by_role):
        raise WorkflowError(
            "PAGE_FINALIZATION_BLOCKED",
            "快照与当前任务文件角色不一致",
            details={
                "failure_stage": "FILE_REBIND",
                "failure_code": "SNAPSHOT_FILE_ROLES_INVALID",
            },
        )
    for role, file in files_by_role.items():
        snapshot_file = result_files[role]
        if snapshot_file.get("file_id") != file.id:
            raise WorkflowError(
                "PAGE_FINALIZATION_BLOCKED",
                "快照文件身份与当前任务不一致",
                details={
                    "failure_stage": "FILE_REBIND",
                    "failure_code": "CURRENT_FILE_ID_MISMATCH",
                },
            )
        file_sha = snapshot_file.get("sha256")
        if not isinstance(file_sha, str) or file_sha not in rows_by_sha:
            raise WorkflowError(
                "PAGE_FINALIZATION_BLOCKED",
                "快照文件摘要缺失或未命中",
                details={
                    "failure_stage": "SNAPSHOT_READ",
                    "failure_code": "SNAPSHOT_FILE_SHA_MISSING",
                },
            )

    sidecar_cache = SqlAlchemyPageLocationSidecarCache(session_factory)
    sidecars: dict[str, DocxPageLocationSidecar] = {}
    for role, file in files_by_role.items():
        file_sha = result_files[role]["sha256"]
        sidecar = await sidecar_cache.load(file_sha256=file_sha, file_id=file.id)
        if sidecar is None:
            raise WorkflowError(
                "DOCX_PAGE_LOCATION_INCOMPLETE",
                "页码 sidecar 缓存未命中",
                details={
                    "failure_stage": "SIDECAR_READ",
                    "failure_code": "PAGE_SIDECAR_CACHE_MISSING",
                },
            )
        rebound = rebind_docx_page_location_sidecar(sidecar, file_id=file.id)
        validate_docx_page_location_sidecar(rebound, file_id=file.id)
        sidecars[file.id] = rebound
    if len(sidecars) != 3:
        raise WorkflowError(
            "DOCX_PAGE_LOCATION_INCOMPLETE",
            "页码 sidecar 未覆盖三份当前文件",
            details={
                "failure_stage": "SIDECAR_READ",
                "failure_code": "PAGE_SIDECAR_COUNT_INVALID",
            },
        )
    return task, files_by_role, result, sidecars


def _page_enrich(
    source_result: dict[str, Any],
    sidecars: dict[str, DocxPageLocationSidecar],
) -> tuple[dict[str, Any], dict[str, int], dict[str, Any]]:
    result = deepcopy(source_result)
    apply_docx_page_location_sidecars(result, sidecars, strict=True)
    coverage = validate_public_page_coverage(result, sidecars)
    TaskResultData.model_validate(result)
    advice = _safe_advice_coverage(result)
    if advice["nonempty_count"] != advice["risk_count"]:
        raise WorkflowError(
            "PAGE_FINALIZATION_BLOCKED",
            "报告 Advice 覆盖不完整",
            details={
                "failure_stage": "ADVICE_VALIDATION",
                "failure_code": "ADVICE_COVERAGE_INCOMPLETE",
            },
        )
    return result, coverage, advice


async def _apply_result(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    result: dict[str, Any],
    coverage: dict[str, int],
    advice: dict[str, Any],
    snapshot_hash: str,
    files_by_role: dict[str, TaskFile],
) -> None:
    now = datetime.now(UTC)
    stats = result["summary"]["statistics"]
    safe_audit = {
        "operation": "PAGE_ENRICHMENT_FINALIZE",
        "snapshot_hash": snapshot_hash,
        "snapshot_version": PRE_PAGE_RESULT_SNAPSHOT_VERSION,
        "sidecar_owner": PAGE_LOCATION_CACHE_OWNER,
        "sidecar_version": PAGE_LOCATION_CACHE_VERSION,
        "ocr_calls": 0,
        "llm_calls": 0,
        "page_coverage": coverage,
        "advice_coverage": advice,
    }
    async with session_factory() as session, session.begin():
        task = await session.get(CheckTask, TASK_ID, with_for_update=True)
        if task is None or task.status != TaskStatus.FAILED:
            raise WorkflowError(
                "PAGE_FINALIZATION_BLOCKED",
                "页码收口任务状态在写入前发生变化",
                details={
                    "failure_stage": "IDEMPOTENCY_CHECK",
                    "failure_code": "TASK_STATE_CHANGED",
                },
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
        for role, file in files_by_role.items():
            snapshot_file = next(
                item for item in result["files"] if item.get("role") == role
            )
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
                details=safe_audit,
            )
        )


async def run(*, apply: bool, output: Path) -> dict[str, Any]:
    settings = get_settings()
    engine = create_async_engine(host_database_url(settings.DATABASE_URL))
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    report: dict[str, Any] = {
        "status": "DRY_RUN" if not apply else "FAILED",
        "task_id": TASK_ID,
        "snapshot_version": PRE_PAGE_RESULT_SNAPSHOT_VERSION,
        "ocr_calls": 0,
        "llm_calls": 0,
    }
    try:
        task, files_by_role, source_result, sidecars = await _read_inputs(session_factory)
        snapshot_hash = snapshot_content_hash(source_result)
        result, coverage, advice = _page_enrich(source_result, sidecars)
        report.update(
            {
                "status": "READY_TO_APPLY" if not apply else "APPLIED",
                "snapshot_hash": snapshot_hash,
                "sidecar_count": len(sidecars),
                "page_counts": {
                    role: sidecars[file.id].page_count
                    for role, file in files_by_role.items()
                },
                "page_coverage": coverage,
                "advice_coverage": advice,
                "diff_count": len(result.get("diff_items", [])),
                "risk_count": len(result.get("risk_items", [])),
                "passed_check_count": len(result.get("passed_checks", [])),
            }
        )
        if apply:
            await _apply_result(
                session_factory,
                result=result,
                coverage=coverage,
                advice=advice,
                snapshot_hash=snapshot_hash,
                files_by_role=files_by_role,
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = asyncio.run(run(apply=args.apply, output=args.output))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] in {"READY_TO_APPLY", "APPLIED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
