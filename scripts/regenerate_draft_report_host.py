"""Run one host-side report regeneration from a successful DRAFT_REVIEW task.

This operator-only entry point creates a new console task through the private
task service.  It never calls the public retry endpoint, never mutates the
source result, and never permits fact extraction to fall back to a model call.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import multiprocessing as mp
import time
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.adapters.document_parser.cached_parser import (
    CachedExternalDocumentParser,
    SqlAlchemyDocumentParseCache,
)
from app.adapters.document_parser.textin_parser import TextInDocumentParser
from app.adapters.llm.openai_client import OpenAIContractLlmClient
from app.core.config import get_settings
from app.core.enums import FileRole, TaskStatus, TaskType
from app.core.errors import AppError, WorkflowError
from app.core.ids import new_request_id
from app.db.models import (
    CheckTask,
    TaskFile,
    TaskResult,
)
from app.db.models import ExtractionCheckpoint as ExtractionCheckpointRow
from app.documents.models import ParsedDocument
from app.documents.page_locations import (
    PAGE_LOCATION_CACHE_OWNER,
    PAGE_LOCATION_CACHE_VERSION,
    DocxPageLocationSidecar,
    bind_docx_page_locations,
    build_docx_page_location_sidecar,
    deserialize_docx_page_location_sidecar,
    page_location_cache_identity,
    rebind_docx_page_location_sidecar,
    serialize_docx_page_location_sidecar,
    validate_docx_page_location_sidecar,
)
from app.documents.parsers import ParserRegistry
from app.documents.router import DocumentParsingRouter
from app.draft_review.checkpoints import (
    ExtractionCheckpoint,
    SqlAlchemyExtractionCheckpointStore,
)
from app.draft_review.extraction import (
    DOCUMENT_EXTRACTION_CHECKPOINT_VERSION,
    _validated_document_checkpoint,
)
from app.services.downloader import DOCX_MIME, LocalFile
from app.services.task_service import TaskService
from app.worker.runner import WorkerRunner
from app.workflows.report_regeneration import (
    LocalRegenerationDownloader,
    ReportRegenerationWorkflowExecutor,
    remap_file_references,
    validate_file_reference_remap,
)
from app.workflows.router import WorkflowRouter
from scripts.draft_review_llm_readiness import CountingTransport

SOURCE_TASK_ID = "tsk_01M161GFY6Q7YSP07R877XQM2B"
REGENERATION_TASK_ID = "tsk_01M167E69YV0MGNHENB9HW10DG"
SAMPLE_DIR = Path(r"D:\work\contract_review\脱敏真实合同")
PROGRESS_PREFIX = "report-regeneration-final"


class SetupStageError(Exception):
    """Safe diagnostic wrapper for local setup failures."""

    def __init__(self, stage: str, component: str, exc: BaseException) -> None:
        self.stage = stage
        self.component = component
        self.exception_type = type(exc).__name__
        self.failure_code = exc.code if isinstance(exc, (AppError, WorkflowError)) else None
        super().__init__(self.exception_type)


def setup_stage(stage: str, component: str, exc: BaseException) -> SetupStageError:
    return SetupStageError(stage, component, exc)


def host_database_url(value: str) -> str:
    url = make_url(value)
    if url.host == "postgres" and url.port == 5432:
        return url.set(host="127.0.0.1", port=15432).render_as_string(hide_password=False)
    return url.render_as_string(hide_password=False)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    metadata = result.get("metadata") or {}
    runs = metadata.get("model_runs") or []
    report = metadata.get("report_regeneration") or {}
    advice = metadata.get("advice_coverage") or {}
    return {
        "workflow_version": metadata.get("workflow_version"),
        "rules_version": metadata.get("rules_version"),
        "diff_count": len(result.get("diff_items", [])),
        "risk_count": len(result.get("risk_items", [])),
        "passed_count": len(result.get("passed_checks", [])),
        "fact_matrix_count": len(result.get("fact_matrix", [])),
        "model_run_count": len(runs) if isinstance(runs, list) else 0,
        "fact_extraction_calls": report.get("fact_extraction_calls", 0),
        "mapping_call_count": report.get("mapping_call_count", 0),
        "advice_call_count": report.get("advice_call_count", 0),
        "advice_coverage": {
            key: advice.get(key)
            for key in (
                "risk_count",
                "model_count",
                "fallback_count",
                "model_rate",
                "fallback_rate",
            )
            if key in advice
        },
        "page_counts": report.get("page_counts", {}),
        "model_names": sorted(
            {
                str(item.get("actual_model") or item.get("configured_model"))
                for item in runs
                if isinstance(item, dict)
                and (item.get("actual_model") or item.get("configured_model"))
            }
        ),
    }


async def _read_preflight(
    session: AsyncSession,
    source_task_id: str,
) -> tuple[CheckTask, TaskResult, list[TaskFile], dict[str, Any]]:
    source = (
        await session.execute(
            select(CheckTask)
            .where(CheckTask.id == source_task_id)
            .options(selectinload(CheckTask.files))
        )
    ).scalar_one_or_none()
    result = await session.get(TaskResult, source_task_id)
    if source is None:
        raise AppError("REPORT_REGENERATION_SOURCE_INVALID", "来源任务不存在", status_code=409)
    if source.status != TaskStatus.SUCCEEDED or source.task_type != TaskType.DRAFT_REVIEW:
        raise AppError(
            "REPORT_REGENERATION_SOURCE_INVALID",
            "来源任务不是成功的起草检查任务",
            status_code=409,
            details={
                "source_status": source.status.value,
                "source_task_type": source.task_type.value,
            },
        )
    if result is None:
        raise AppError("REPORT_REGENERATION_SOURCE_INVALID", "来源任务结果缺失", status_code=409)
    # Validation is repeated by TaskService; doing it here ensures no task row
    # is created when the source is not a publishable result.
    from app.schemas.results import TaskResultData

    TaskResultData.model_validate(result.result)
    files = sorted(source.files, key=lambda item: item.sort_order)
    if len(files) != 3 or [item.role for item in files] != [
        FileRole.TARGET,
        FileRole.TEMPLATE,
        FileRole.REFERENCE,
    ]:
        raise AppError(
            "REPORT_REGENERATION_SOURCE_INVALID",
            "来源任务文件角色不完整",
            status_code=409,
            details={"source_file_count": len(files)},
        )
    if any(not item.sha256 for item in files):
        raise AppError(
            "REPORT_REGENERATION_SOURCE_INVALID",
            "来源文件摘要缺失",
            status_code=409,
            details={"missing_sha_count": sum(not item.sha256 for item in files)},
        )
    for item in files:
        path = SAMPLE_DIR / item.file_name
        if not path.is_file() or sha256(path) != item.sha256:
            raise AppError(
                "REPORT_REGENERATION_FILE_MISMATCH",
                "本地脱敏文件未通过来源摘要校验",
                status_code=409,
                details={
                    "failure_stage": "LOCAL_FILE_PREFLIGHT",
                    "failure_code": "SHA256_MISMATCH",
                },
            )
    checkpoint_rows = (
        await session.execute(
            select(
                ExtractionCheckpointRow.file_sha256,
                ExtractionCheckpointRow.extraction_version,
                func.count(),
            )
            .where(
                ExtractionCheckpointRow.task_id == source_task_id,
                ExtractionCheckpointRow.status == "SUCCEEDED",
            )
            .group_by(
                ExtractionCheckpointRow.file_sha256,
                ExtractionCheckpointRow.extraction_version,
            )
        )
    ).all()
    document_versions = {
        str(row[0]): int(row[2])
        for row in checkpoint_rows
        if row[1] == "document-extraction-v1"
    }
    expected_hashes = {str(item.sha256) for item in files}
    if set(document_versions) != expected_hashes:
        raise AppError(
            "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
            "来源任务未保存三份完整文档抽取快照",
            status_code=409,
            details={
                "failure_stage": "SNAPSHOT_PREFLIGHT",
                "failure_code": "DOCUMENT_EXTRACTION_CHECKPOINT_MISSING",
                "document_snapshot_count": len(document_versions),
            },
        )
    children = (
        await session.execute(select(CheckTask).where(CheckTask.source_task_id == source_task_id))
    ).scalars().all()
    existing = next(
        (
            item
            for item in children
            if isinstance(item.options, dict)
            and item.options.get("_report_regeneration_source_task_id") == source_task_id
        ),
        None,
    )
    if existing is not None:
        raise AppError(
            "REPORT_REGENERATION_ALREADY_EXISTS",
            "该来源任务已经存在报告再生成任务",
            status_code=409,
            details={"existing_task_id": existing.id, "existing_status": existing.status.value},
        )
    pending_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(CheckTask)
                .where(CheckTask.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
            )
        ).scalar_one()
    )
    if pending_count:
        raise AppError(
            "REPORT_REGENERATION_WORKER_NOT_EXCLUSIVE",
            "存在其他待处理任务，无法安全执行唯一再生成任务",
            status_code=409,
            details={"active_task_count": pending_count},
        )
    return source, result, files, {
        "document_snapshot_count": 3,
        "checkpoint_row_count": len(checkpoint_rows),
    }


async def _read_existing_regeneration_task(
    session: AsyncSession,
    task_id: str,
    *,
    require_pending: bool = True,
) -> tuple[CheckTask, TaskResult, list[TaskFile], list[TaskFile], dict[str, Any]]:
    task = (
        await session.execute(
            select(CheckTask)
            .where(CheckTask.id == task_id)
            .options(selectinload(CheckTask.files))
        )
    ).scalar_one_or_none()
    if task is None:
        raise AppError(
            "REPORT_REGENERATION_TASK_INVALID",
            "再生成任务不存在",
            status_code=409,
            details={"failure_stage": "TASK_PREFLIGHT", "failure_code": "TASK_NOT_FOUND"},
        )
    if task.source_task_id != SOURCE_TASK_ID:
        raise AppError(
            "REPORT_REGENERATION_TASK_INVALID",
            "再生成任务来源不符合固定验收范围",
            status_code=409,
            details={"failure_stage": "TASK_PREFLIGHT", "failure_code": "SOURCE_TASK_ID_MISMATCH"},
        )
    if require_pending and (
        task.status != TaskStatus.PENDING or task.attempt_count >= task.max_attempts
    ):
        raise AppError(
            "REPORT_REGENERATION_TASK_NOT_PENDING",
            "再生成任务已被领取或不处于待执行状态",
            status_code=409,
            details={
                "failure_stage": "TASK_PREFLIGHT",
                "failure_code": "TASK_NOT_PENDING",
                "task_status": task.status.value,
                "attempt_count": task.attempt_count,
            },
        )
    if not isinstance(task.options, dict) or task.options.get(
        "_report_regeneration_source_task_id"
    ) != SOURCE_TASK_ID:
        raise AppError(
            "REPORT_REGENERATION_TASK_INVALID",
            "再生成任务缺少受控来源标记",
            status_code=409,
            details={
                "failure_stage": "TASK_PREFLIGHT",
                "failure_code": "REGENERATION_MARKER_MISSING",
            },
        )

    source = (
        await session.execute(
            select(CheckTask)
            .where(CheckTask.id == SOURCE_TASK_ID)
            .options(selectinload(CheckTask.files))
        )
    ).scalar_one_or_none()
    source_result = await session.get(TaskResult, SOURCE_TASK_ID)
    if source is None or source_result is None:
        raise AppError(
            "REPORT_REGENERATION_SOURCE_INVALID",
            "再生成来源任务或结果缺失",
            status_code=409,
            details={"failure_stage": "SOURCE_PREFLIGHT", "failure_code": "SOURCE_NOT_FOUND"},
        )
    from app.schemas.results import TaskResultData

    TaskResultData.model_validate(source_result.result)
    source_files = sorted(source.files, key=lambda item: item.sort_order)
    current_files = sorted(task.files, key=lambda item: item.sort_order)
    expected_roles = [FileRole.TARGET, FileRole.TEMPLATE, FileRole.REFERENCE]
    if len(source_files) != 3 or len(current_files) != 3:
        raise AppError(
            "REPORT_REGENERATION_TASK_INVALID",
            "再生成任务文件数量不完整",
            status_code=409,
            details={
                "failure_stage": "TASK_PREFLIGHT",
                "failure_code": "FILE_COUNT_INVALID",
                "source_file_count": len(source_files),
                "current_file_count": len(current_files),
            },
        )
    if [item.role for item in source_files] != expected_roles or [
        item.role for item in current_files
    ] != expected_roles:
        raise AppError(
            "REPORT_REGENERATION_TASK_INVALID",
            "再生成任务文件角色顺序不完整",
            status_code=409,
            details={"failure_stage": "TASK_PREFLIGHT", "failure_code": "FILE_ROLE_ORDER_INVALID"},
        )
    for source_file, current_file in zip(source_files, current_files, strict=True):
        if (
            not source_file.sha256
            or not current_file.sha256
            or source_file.sha256 != current_file.sha256
        ):
            raise AppError(
                "REPORT_REGENERATION_FILE_MISMATCH",
                "再生成任务文件摘要不一致",
                status_code=409,
                details={"failure_stage": "TASK_PREFLIGHT", "failure_code": "SHA256_MISMATCH"},
            )
        path = SAMPLE_DIR / current_file.file_name
        if not path.is_file() or sha256(path) != current_file.sha256:
            raise AppError(
                "REPORT_REGENERATION_FILE_MISMATCH",
                "本地脱敏文件未通过再生成任务摘要校验",
                status_code=409,
                details={
                    "failure_stage": "LOCAL_FILE_PREFLIGHT",
                    "failure_code": "SHA256_MISMATCH",
                },
            )

    checkpoint_rows = (
        await session.execute(
            select(
                ExtractionCheckpointRow.file_sha256,
                ExtractionCheckpointRow.extraction_version,
                func.count(),
            )
            .where(
                ExtractionCheckpointRow.task_id == SOURCE_TASK_ID,
                ExtractionCheckpointRow.status == "SUCCEEDED",
            )
            .group_by(
                ExtractionCheckpointRow.file_sha256,
                ExtractionCheckpointRow.extraction_version,
            )
        )
    ).all()
    snapshot_hashes = {
        str(row[0])
        for row in checkpoint_rows
        if row[1] == "document-extraction-v1"
    }
    if snapshot_hashes != {str(item.sha256) for item in source_files}:
        raise AppError(
            "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
            "来源任务未保存三份完整文档抽取快照",
            status_code=409,
            details={
                "failure_stage": "SNAPSHOT_PREFLIGHT",
                "failure_code": "DOCUMENT_EXTRACTION_CHECKPOINT_MISSING",
                "document_snapshot_count": len(snapshot_hashes),
            },
        )
    return task, source_result, source_files, current_files, {
        "document_snapshot_count": len(snapshot_hashes),
        "checkpoint_row_count": len(checkpoint_rows),
    }


async def _load_source_snapshot_records(
    session: AsyncSession,
    source_files: list[TaskFile],
) -> dict[str, dict[str, Any]]:
    """Read exactly one successful document snapshot per source file digest."""

    expected_hashes = {str(item.sha256) for item in source_files}
    rows = (
        await session.execute(
            select(ExtractionCheckpointRow)
            .where(
                ExtractionCheckpointRow.task_id == SOURCE_TASK_ID,
                ExtractionCheckpointRow.extraction_version
                == DOCUMENT_EXTRACTION_CHECKPOINT_VERSION,
                ExtractionCheckpointRow.status == "SUCCEEDED",
                ExtractionCheckpointRow.file_sha256.in_(expected_hashes),
            )
            .order_by(
                ExtractionCheckpointRow.file_sha256,
                ExtractionCheckpointRow.updated_at.desc(),
            )
        )
    ).scalars().all()
    grouped: dict[str, list[ExtractionCheckpointRow]] = {}
    for row in rows:
        grouped.setdefault(str(row.file_sha256), []).append(row)
    if set(grouped) != expected_hashes or any(
        len(grouped.get(file_sha, [])) != 1 for file_sha in expected_hashes
    ):
        raise AppError(
            "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
            "来源任务未为每份文件提供唯一文档抽取快照",
            status_code=409,
            details={
                "failure_stage": "SNAPSHOT_SOURCE_READ",
                "failure_code": "DOCUMENT_EXTRACTION_CHECKPOINT_NOT_UNIQUE",
                "required_count": len(expected_hashes),
                "matched_count": sum(
                    len(grouped.get(file_sha, [])) == 1 for file_sha in expected_hashes
                ),
            },
        )
    return {
        file_sha: {
            "batch_id": grouped[file_sha][0].batch_id,
            "payload_digest": grouped[file_sha][0].payload_digest,
            "value": grouped[file_sha][0].value,
            "model_name": grouped[file_sha][0].model_name,
        }
        for file_sha in expected_hashes
    }


class CacheOnlyExternalDocumentParser:
    """Read the configured parser cache without falling back to the network."""

    def __init__(self, cached_parser: CachedExternalDocumentParser) -> None:
        self.cached_parser = cached_parser

    async def parse(self, file: LocalFile, *, mode: str) -> ParsedDocument:
        cache_key = self.cached_parser._cache_key(
            mode=mode,
            include_stamp_images=False,
        )
        cached = await self.cached_parser.cache.load(
            file_sha256=file.sha256,
            cache_key=cache_key,
        )
        if not isinstance(cached, dict) or not isinstance(cached.get("document"), dict):
            raise WorkflowError(
                "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
                "正式页码诊断缺少本地解析缓存",
                details={
                    "failure_stage": "FORMAL_PARSE_PREFLIGHT",
                    "failure_code": "OCR_PARSE_CACHE_MISSING",
                },
            )
        document = ParsedDocument.model_validate(cached["document"])
        return self.cached_parser._rebind(document, file)


class CachedPageSidecarDocumentRouter(DocumentParsingRouter):
    """Parse DOCX locally and bind already validated page sidecars."""

    def __init__(
        self,
        *,
        local: ParserRegistry,
        sidecars_by_file_id: dict[str, DocxPageLocationSidecar],
    ) -> None:
        super().__init__(local=local, external=None, docx_page_location_enabled=False)
        self.sidecars_by_file_id = dict(sidecars_by_file_id)

    async def parse_draft_review(self, files: list[LocalFile]) -> list[ParsedDocument]:
        self.page_location_sidecars = {}
        parsed: list[ParsedDocument] = []
        for file in files:
            document = await self.parse_draft_review_file(file)
            sidecar = self.sidecars_by_file_id.get(file.file_id)
            if sidecar is None:
                raise WorkflowError(
                    "DOCX_PAGE_LOCATION_INCOMPLETE",
                    "再生成任务缺少已验证的页码 sidecar",
                    details={
                        "failure_stage": "PAGE_SIDECAR_LOAD",
                        "failure_code": "PAGE_SIDECAR_MISSING",
                    },
                )
            rebound = rebind_docx_page_location_sidecar(sidecar, file_id=file.file_id)
            validate_docx_page_location_sidecar(rebound, file_id=file.file_id)
            bind_docx_page_locations(document, rebound)
            self.page_location_sidecars[file.file_id] = rebound
            parsed.append(document)
        return parsed


def _page_sidecar_process_entry(
    local_value: dict[str, Any],
    external_value: dict[str, Any],
    connection: Any,
) -> None:
    """Build one sidecar in a killable process for the operator timeout."""

    try:
        local_document = ParsedDocument.model_validate(local_value)
        external_document = ParsedDocument.model_validate(external_value)
        sidecar = build_docx_page_location_sidecar(local_document, external_document)
        connection.send(("OK", serialize_docx_page_location_sidecar(sidecar)))
    except (AppError, WorkflowError) as exc:
        details = exc.details if isinstance(exc.details, dict) else {}
        connection.send(
            (
                "ERROR",
                {
                    "failure_stage": details.get("failure_stage", "PAGE_SIDECAR_BUILD"),
                    "failure_code": details.get("failure_code", exc.code),
                },
            )
        )
    except Exception as exc:
        connection.send(
            (
                "ERROR",
                {
                    "failure_stage": "PAGE_SIDECAR_BUILD",
                    "failure_code": type(exc).__name__,
                },
            )
        )
    finally:
        connection.close()


async def _build_page_sidecar_isolated(
    local_document: ParsedDocument,
    external_document: ParsedDocument,
    *,
    timeout_seconds: int = 300,
) -> DocxPageLocationSidecar:
    """Run the CPU-heavy mapper in a process whose timeout can be enforced."""

    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_page_sidecar_process_entry,
        args=(
            local_document.model_dump(mode="json"),
            external_document.model_dump(mode="json"),
            child,
        ),
    )
    process.start()
    child.close()
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            if parent.poll():
                status, value = parent.recv()
                if status == "ERROR":
                    raise WorkflowError(
                        "DOCX_PAGE_LOCATION_INCOMPLETE",
                        "DOCX 页码 sidecar 生成失败",
                        details=value,
                    )
                sidecar = deserialize_docx_page_location_sidecar(
                    value,
                    file_id=local_document.file_id,
                )
                validate_docx_page_location_sidecar(
                    sidecar,
                    file_id=local_document.file_id,
                )
                return sidecar
            if not process.is_alive():
                raise WorkflowError(
                    "DOCX_PAGE_LOCATION_INCOMPLETE",
                    "DOCX 页码 sidecar 进程异常退出",
                    details={
                        "failure_stage": "PAGE_SIDECAR_BUILD",
                        "failure_code": "PAGE_SIDECAR_PROCESS_EXITED",
                    },
                )
            await asyncio.sleep(0.1)
        raise WorkflowError(
            "DOCX_PAGE_LOCATION_INCOMPLETE",
            "DOCX 页码 sidecar 计算超过单文件时间上限",
            details={
                "failure_stage": "PAGE_SIDECAR_BUILD",
                "failure_code": "PAGE_SIDECAR_TIMEOUT",
            },
        )
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
        parent.close()


async def _load_page_sidecars(
    session_factory: async_sessionmaker[AsyncSession],
    current_files: list[TaskFile],
) -> dict[str, DocxPageLocationSidecar]:
    """Load one content-addressed sidecar per current task file."""

    expected_hashes = {str(item.sha256) for item in current_files if item.sha256}
    rows = (
        await _fetch_page_sidecar_rows(session_factory, expected_hashes)
    )
    if (
        len(rows) != len(expected_hashes)
        or {str(row.file_sha256) for row in rows} != expected_hashes
    ):
        raise WorkflowError(
            "DOCX_PAGE_LOCATION_INCOMPLETE",
            "三份文件的页码 sidecar 缓存不完整",
            details={
                "failure_stage": "PAGE_SIDECAR_LOAD",
                "failure_code": "PAGE_SIDECAR_CACHE_INCOMPLETE",
                "required_count": len(expected_hashes),
                "matched_count": len(rows),
            },
        )
    by_sha = {str(row.file_sha256): row for row in rows}
    sidecars: dict[str, DocxPageLocationSidecar] = {}
    for file in current_files:
        file_sha = str(file.sha256)
        row = by_sha.get(file_sha)
        if row is None:
            raise WorkflowError(
                "DOCX_PAGE_LOCATION_INCOMPLETE",
                "文件页码 sidecar 缓存缺失",
                details={
                    "failure_stage": "PAGE_SIDECAR_LOAD",
                    "failure_code": "PAGE_SIDECAR_CACHE_MISSING",
                },
            )
        expected_batch_id, expected_digest = page_location_cache_identity(file_sha)
        if row.batch_id != expected_batch_id or row.payload_digest != expected_digest:
            raise WorkflowError(
                "DOCX_PAGE_LOCATION_INCOMPLETE",
                "页码 sidecar 身份校验失败",
                details={
                    "failure_stage": "PAGE_SIDECAR_LOAD",
                    "failure_code": "PAGE_SIDECAR_IDENTITY_INVALID",
                },
            )
        try:
            sidecars[file.id] = deserialize_docx_page_location_sidecar(
                row.value,
                file_id=file.id,
            )
            validate_docx_page_location_sidecar(sidecars[file.id], file_id=file.id)
        except (TypeError, ValueError) as exc:
            raise WorkflowError(
                "DOCX_PAGE_LOCATION_INCOMPLETE",
                "页码 sidecar 内容校验失败",
                details={
                    "failure_stage": "PAGE_SIDECAR_LOAD",
                    "failure_code": "PAGE_SIDECAR_VALUE_INVALID",
                },
            ) from exc
    return sidecars


async def _load_page_sidecar_for_file(
    session_factory: async_sessionmaker[AsyncSession],
    file: TaskFile,
) -> DocxPageLocationSidecar | None:
    rows = await _fetch_page_sidecar_rows(session_factory, {str(file.sha256)})
    if len(rows) != 1:
        return None
    row = rows[0]
    expected_batch_id, expected_digest = page_location_cache_identity(str(file.sha256))
    if row.batch_id != expected_batch_id or row.payload_digest != expected_digest:
        return None
    try:
        sidecar = deserialize_docx_page_location_sidecar(row.value, file_id=file.id)
        validate_docx_page_location_sidecar(sidecar, file_id=file.id)
    except (TypeError, ValueError):
        return None
    return sidecar


async def _fetch_page_sidecar_rows(
    session_factory: async_sessionmaker[AsyncSession],
    expected_hashes: set[str],
) -> list[ExtractionCheckpointRow]:
    if not expected_hashes:
        return []
    async with session_factory() as session:
        return (
            await session.execute(
                select(ExtractionCheckpointRow)
                .where(
                    ExtractionCheckpointRow.task_id == PAGE_LOCATION_CACHE_OWNER,
                    ExtractionCheckpointRow.extraction_version
                    == PAGE_LOCATION_CACHE_VERSION,
                    ExtractionCheckpointRow.status == "SUCCEEDED",
                    ExtractionCheckpointRow.file_sha256.in_(expected_hashes),
                )
                .order_by(ExtractionCheckpointRow.file_sha256)
            )
        ).scalars().all()


async def _save_page_sidecar(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    file_sha256: str,
    sidecar: DocxPageLocationSidecar,
) -> None:
    batch_id, payload_digest = page_location_cache_identity(file_sha256)
    await SqlAlchemyExtractionCheckpointStore(session_factory).save(
        ExtractionCheckpoint(
            task_id=PAGE_LOCATION_CACHE_OWNER,
            file_sha256=file_sha256,
            extraction_version=PAGE_LOCATION_CACHE_VERSION,
            batch_id=batch_id,
            payload_digest=payload_digest,
            value=serialize_docx_page_location_sidecar(sidecar),
            status="SUCCEEDED",
            model_name="DOCX_PAGE_LOCATION",
        )
    )


async def _build_regeneration_runner(
    settings: Any,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    source_result: TaskResult,
    source_files: list[TaskFile],
    current_files: list[TaskFile],
    source_task_id: str,
    source_snapshot_records: dict[str, dict[str, Any]] | None = None,
    page_sidecars: dict[str, DocxPageLocationSidecar] | None = None,
) -> tuple[WorkerRunner, CountingTransport]:
    file_id_map = {
        source_file.id: current_file.id
        for source_file, current_file in zip(source_files, current_files, strict=True)
    }
    paths_by_id = {item.id: SAMPLE_DIR / item.file_name for item in current_files}
    hashes_by_id = {item.id: str(item.sha256) for item in current_files}
    try:
        local_parser = ParserRegistry(
            pdf_min_text_chars_per_page=settings.PDF_MIN_TEXT_CHARS_PER_PAGE
        )
        if page_sidecars is not None:
            parser = CachedPageSidecarDocumentRouter(
                local=local_parser,
                sidecars_by_file_id=page_sidecars,
            )
        else:
            parser = DocumentParsingRouter(
                local=local_parser,
                external=CachedExternalDocumentParser(
                    TextInDocumentParser(settings),
                    SqlAlchemyDocumentParseCache(session_factory),
                    settings,
                ),
                docx_page_location_enabled=True,
            )
    except Exception as exc:
        raise setup_stage("PARSER_CONSTRUCTION", "document_parser", exc) from exc
    transport = CountingTransport()
    try:
        llm = OpenAIContractLlmClient(
            settings,
            transport=transport,
            advice_response_format_override="json_object",
        )
    except Exception as exc:
        await transport.close_all()
        raise setup_stage("LLM_CLIENT_CONSTRUCTION", "llm_client", exc) from exc
    try:
        executor = ReportRegenerationWorkflowExecutor(
            settings,
            source_result=source_result.result,
            source_file_ids={item.id for item in source_files},
            current_file_ids={item.id for item in current_files},
            file_id_map=file_id_map,
            source_task_id=source_task_id,
            downloader=LocalRegenerationDownloader(paths_by_id, hashes_by_id),
            llm=llm,
            document_router=parser,
            checkpoint_store=SqlAlchemyExtractionCheckpointStore(session_factory),
            source_snapshot_records=source_snapshot_records,
        )
    except Exception as exc:
        await transport.close_all()
        raise setup_stage("EXECUTOR_CONSTRUCTION", "workflow_executor", exc) from exc
    try:
        workflow = WorkflowRouter(settings, draft_review=executor)
        runner = WorkerRunner(settings, workflow=workflow, session_factory=session_factory)
    except Exception as exc:
        await transport.close_all()
        raise setup_stage("WORKER_CONSTRUCTION", "worker_runner", exc) from exc
    return runner, transport


async def setup_only(task_id: str, output: Path) -> dict[str, Any]:
    if task_id != REGENERATION_TASK_ID:
        report = {
            "status": "BLOCKED",
            "task_id": task_id,
            "failure_stage": "TASK_PREFLIGHT",
            "failure_code": "TASK_ID_NOT_ALLOWED",
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    base = get_settings()
    database_url = host_database_url(base.DATABASE_URL)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    transport: CountingTransport | None = None
    try:
        try:
            settings = base.model_copy(
                update={
                    "DATABASE_URL": database_url,
                    "TEMP_ROOT": str(Path(".real-diagnostic-temp") / "report-regeneration"),
                    "LLM_ENABLED": True,
                    "OCR_ENABLED": False,
                    "DOCX_PAGE_LOCATION_ENABLED": True,
                }
            )
        except Exception as exc:
            raise setup_stage("SETTINGS_CONSTRUCTION", "settings", exc) from exc
        if not settings.llm_configured:
            raise AppError(
                "REPORT_REGENERATION_NOT_CONFIGURED",
                "宿主机 LLM 配置未就绪",
                status_code=409,
                details={"failure_stage": "CONFIG_PREFLIGHT", "failure_code": "LLM_NOT_CONFIGURED"},
            )
        if not settings.document_parser_configured:
            raise AppError(
                "REPORT_REGENERATION_NOT_CONFIGURED",
                "DOCX 页码解析配置未就绪",
                status_code=409,
                details={
                    "failure_stage": "CONFIG_PREFLIGHT",
                    "failure_code": "DOCX_PAGE_PARSER_NOT_CONFIGURED",
                },
            )
        try:
            async with session_factory() as session:
                _, source_result, source_files, current_files, preflight = (
                    await _read_existing_regeneration_task(session, task_id)
                )
        except Exception as exc:
            raise setup_stage("TASK_PREFLIGHT", "database_task_mapping", exc) from exc
        runner, transport = await _build_regeneration_runner(
            settings,
            session_factory=session_factory,
            source_result=source_result,
            source_files=source_files,
            current_files=current_files,
            source_task_id=SOURCE_TASK_ID,
        )
        del runner
        report = {
            "status": "SETUP_OK",
            "task_id": task_id,
            "source_task_id": SOURCE_TASK_ID,
            "failure_stage": None,
            "failure_code": None,
            "external_calls": 0,
            "components": {
                "local_file_count": len(current_files),
                "parser": "constructed",
                "llm_client": "constructed",
                "workflow_executor": "constructed",
                "worker_runner": "constructed",
            },
            "snapshot_reuse": {
                "document_snapshot_count": preflight["document_snapshot_count"],
                "fact_extraction_calls": 0,
            },
        }
    except SetupStageError as exc:
        report = {
            "status": "FAILED",
            "task_id": task_id,
            "source_task_id": SOURCE_TASK_ID,
            "failure_stage": exc.stage,
            "failure_component": exc.component,
            "failure_code": exc.failure_code or "LOCAL_SETUP_ERROR",
            "exception_type": exc.exception_type,
            "external_calls": 0,
        }
    except (AppError, WorkflowError) as exc:
        details = exc.details if isinstance(exc.details, dict) else {}
        report = {
            "status": "FAILED",
            "task_id": task_id,
            "source_task_id": SOURCE_TASK_ID,
            "failure_stage": details.get("failure_stage", "SETUP_PREFLIGHT"),
            "failure_code": details.get("failure_code", exc.code),
            "exception_type": type(exc).__name__,
            "external_calls": 0,
        }
    except Exception as exc:
        report = {
            "status": "FAILED",
            "task_id": task_id,
            "source_task_id": SOURCE_TASK_ID,
            "failure_stage": "SETUP_UNKNOWN",
            "failure_code": "LOCAL_SETUP_ERROR",
            "exception_type": type(exc).__name__,
            "external_calls": 0,
        }
    finally:
        if transport is not None:
            await transport.close_all()
        await engine.dispose()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


async def snapshot_only(task_id: str, output: Path) -> dict[str, Any]:
    """Verify all source snapshots against the formal local parser identity."""

    if task_id != REGENERATION_TASK_ID:
        report = {
            "status": "BLOCKED",
            "task_id": task_id,
            "failure_stage": "SNAPSHOT_PREFLIGHT",
            "failure_code": "TASK_ID_NOT_ALLOWED",
            "external_calls": 0,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    base = get_settings()
    database_url = host_database_url(base.DATABASE_URL)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        settings = base.model_copy(
            update={
                "DATABASE_URL": database_url,
                "LLM_ENABLED": False,
                "OCR_ENABLED": False,
                "DOCX_PAGE_LOCATION_ENABLED": False,
            }
        )
        async with session_factory() as session:
            task, _, source_files, current_files, _ = await _read_existing_regeneration_task(
                session,
                task_id,
                require_pending=False,
            )
            source_snapshot_records = await _load_source_snapshot_records(session, source_files)
        source_file_ids_by_file_id = {
            current_file.id: source_file.id
            for source_file, current_file in zip(source_files, current_files, strict=True)
        }
        parser = ParserRegistry(pdf_min_text_chars_per_page=settings.PDF_MIN_TEXT_CHARS_PER_PAGE)
        formal_parser = DocumentParsingRouter(
            local=parser,
            external=None,
            docx_page_location_enabled=False,
        )
        documents = []
        for item in current_files:
            path = SAMPLE_DIR / item.file_name
            local_file = LocalFile(
                file_id=item.id,
                role=item.role.value,
                file_name=item.file_name,
                safe_url="",
                path=path,
                file_size=path.stat().st_size,
                sha256=str(item.sha256),
                detected_mime_type=DOCX_MIME,
            )
            documents.append(await formal_parser.parse_draft_review_file(local_file))
        checkpoint_store = SqlAlchemyExtractionCheckpointStore(session_factory)
        documents_report: list[dict[str, Any]] = []
        materialized: list[ExtractionCheckpoint] = []
        for document in documents:
            source_record = source_snapshot_records.get(document.sha256)
            item = {
                "role": document.role,
                "file_id": document.file_id,
                "status": "INVALID",
            }
            if source_record is None or not isinstance(source_record.get("value"), dict):
                raise WorkflowError(
                    "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
                    "来源文档快照缺失",
                    details={
                        "failure_stage": "SNAPSHOT_INJECTION",
                        "failure_code": "DOCUMENT_EXTRACTION_CHECKPOINT_MISSING",
                        "file_id": document.file_id,
                    },
                )
            extraction = _validated_document_checkpoint(
                document,
                source_record["value"],
                source_file_id=source_file_ids_by_file_id.get(document.file_id),
            )
            if extraction is None:
                raise WorkflowError(
                    "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
                    "来源文档快照证据无法重绑定到正式解析文档",
                    details={
                        "failure_stage": "SNAPSHOT_INJECTION",
                        "failure_code": "SNAPSHOT_EVIDENCE_REBIND_FAILED",
                        "file_id": document.file_id,
                    },
                )
            batch_id = source_record.get("batch_id")
            payload_digest = source_record.get("payload_digest")
            if not isinstance(batch_id, str) or not isinstance(payload_digest, str):
                raise WorkflowError(
                    "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
                    "来源文档快照身份不完整",
                    details={
                        "failure_stage": "SNAPSHOT_INJECTION",
                        "failure_code": "SNAPSHOT_IDENTITY_INVALID",
                        "file_id": document.file_id,
                    },
                )
            materialized.append(
                ExtractionCheckpoint(
                    task_id=task_id,
                    file_sha256=document.sha256,
                    extraction_version=DOCUMENT_EXTRACTION_CHECKPOINT_VERSION,
                    batch_id=batch_id,
                    payload_digest=payload_digest,
                    value=extraction.model_dump(mode="json"),
                    status="SUCCEEDED",
                    model_name=source_record.get("model_name"),
                    source_task_id=SOURCE_TASK_ID,
                )
            )
            item.update(
                {
                    "batch_id": batch_id,
                    "payload_digest": payload_digest,
                    "status": "HIT",
                    "fact_count": len(extraction.facts),
                    "evidence_rebound": True,
                }
            )
            documents_report.append(item)
        for checkpoint in materialized:
            await checkpoint_store.save(checkpoint)
        async with session_factory() as session:
            materialized_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(ExtractionCheckpointRow)
                        .where(
                            ExtractionCheckpointRow.task_id == task_id,
                            ExtractionCheckpointRow.extraction_version
                            == DOCUMENT_EXTRACTION_CHECKPOINT_VERSION,
                            ExtractionCheckpointRow.status == "SUCCEEDED",
                        )
                    )
                ).scalar_one()
            )
        complete = (
            len(materialized) == 3
            and materialized_count == 3
        )
        report = {
            "status": "SNAPSHOT_OK" if complete else "FAILED",
            "task_id": task_id,
            "source_task_id": SOURCE_TASK_ID,
            "failure_stage": None if complete else "SNAPSHOT_PREFLIGHT",
            "failure_code": None if complete else "DOCUMENT_EXTRACTION_CHECKPOINT_MISSING",
            "external_calls": 0,
            "snapshot_reuse": {
                "required_count": 3,
                "source_snapshot_count": len(source_snapshot_records),
                "formal_context_count": len(documents_report),
                "evidence_rebound_count": sum(
                    item.get("evidence_rebound") is True for item in documents_report
                ),
                "materialized_count": materialized_count,
                "formal_page_sidecar_count": 0,
                "documents": documents_report,
            },
        }
    except (AppError, WorkflowError) as exc:
        details = exc.details if isinstance(exc.details, dict) else {}
        report = {
            "status": "FAILED",
            "task_id": task_id,
            "source_task_id": SOURCE_TASK_ID,
            "failure_stage": details.get("failure_stage", "SNAPSHOT_PREFLIGHT"),
            "failure_code": details.get("failure_code", exc.code),
            "exception_type": type(exc).__name__,
            "external_calls": 0,
        }
    except Exception as exc:
        report = {
            "status": "FAILED",
            "task_id": task_id,
            "source_task_id": SOURCE_TASK_ID,
            "failure_stage": "SNAPSHOT_PREFLIGHT",
            "failure_code": "SNAPSHOT_DIAGNOSTIC_ERROR",
            "exception_type": type(exc).__name__,
            "external_calls": 0,
        }
    finally:
        await engine.dispose()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


async def page_sidecar_only(task_id: str, output: Path) -> dict[str, Any]:
    """Build page sidecars from the OCR cache without calling OCR or LLM."""

    if task_id != REGENERATION_TASK_ID:
        report = {
            "status": "BLOCKED",
            "task_id": task_id,
            "failure_stage": "PAGE_SIDECAR_PREFLIGHT",
            "failure_code": "TASK_ID_NOT_ALLOWED",
            "external_calls": 0,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report

    base = get_settings()
    database_url = host_database_url(base.DATABASE_URL)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    started = time.monotonic()
    try:
        settings = base.model_copy(
            update={
                "DATABASE_URL": database_url,
                "LLM_ENABLED": False,
                "OCR_ENABLED": False,
                "DOCX_PAGE_LOCATION_ENABLED": True,
            }
        )
        async with session_factory() as session:
            _, _, _, current_files, _ = await _read_existing_regeneration_task(
                session,
                task_id,
                require_pending=False,
            )
        cached_external = CachedExternalDocumentParser(
            TextInDocumentParser(settings),
            SqlAlchemyDocumentParseCache(session_factory),
            settings,
        )
        semaphore = asyncio.Semaphore(2)

        async def process_file(file: TaskFile) -> dict[str, Any]:
            async with semaphore:
                cached_sidecar = await _load_page_sidecar_for_file(session_factory, file)
                if cached_sidecar is not None:
                    return {
                        "file_id": file.id,
                        "role": file.role.value,
                        "page_count": cached_sidecar.page_count,
                        "mapped_location_count": cached_sidecar.mapped_location_count,
                        "required_location_count": cached_sidecar.required_location_count,
                        "candidate_mapping_count": cached_sidecar.candidate_mapping_count,
                        "unmapped_location_count": cached_sidecar.unmapped_location_count,
                        "status": "CACHED",
                    }
                path = SAMPLE_DIR / file.file_name
                local_file = LocalFile(
                    file_id=file.id,
                    role=file.role.value,
                    file_name=file.file_name,
                    safe_url="",
                    path=path,
                    file_size=path.stat().st_size,
                    sha256=str(file.sha256),
                    detected_mime_type=DOCX_MIME,
                )
                local_document = await ParserRegistry(
                    pdf_min_text_chars_per_page=settings.PDF_MIN_TEXT_CHARS_PER_PAGE
                ).parse(local_file)
                external_document = await CacheOnlyExternalDocumentParser(
                    cached_external
                ).parse(local_file, mode="auto")
                sidecar = await _build_page_sidecar_isolated(
                    local_document,
                    external_document,
                    timeout_seconds=300,
                )
                sidecar = rebind_docx_page_location_sidecar(sidecar, file_id=file.id)
                validate_docx_page_location_sidecar(sidecar, file_id=file.id)
                await _save_page_sidecar(
                    session_factory,
                    file_sha256=str(file.sha256),
                    sidecar=sidecar,
                )
                return {
                    "file_id": file.id,
                    "role": file.role.value,
                    "page_count": sidecar.page_count,
                    "mapped_location_count": sidecar.mapped_location_count,
                    "required_location_count": sidecar.required_location_count,
                    "candidate_mapping_count": sidecar.candidate_mapping_count,
                    "unmapped_location_count": sidecar.unmapped_location_count,
                    "status": "HIT",
                }

        results = await asyncio.gather(
            *(
                process_file(file)
                for file in sorted(current_files, key=lambda item: item.sort_order)
            ),
            return_exceptions=True,
        )
        documents: list[dict[str, Any]] = []
        failure: dict[str, Any] | None = None
        for result in results:
            if isinstance(result, BaseException):
                if failure is None:
                    if isinstance(result, (AppError, WorkflowError)):
                        details = result.details if isinstance(result.details, dict) else {}
                        failure = {
                            "failure_stage": details.get("failure_stage", "PAGE_SIDECAR_BUILD"),
                            "failure_code": details.get("failure_code", result.code),
                        }
                    else:
                        failure = {
                            "failure_stage": "PAGE_SIDECAR_BUILD",
                            "failure_code": type(result).__name__,
                        }
            else:
                documents.append(result)
        # Preserve the first file-level failure.  Loading the aggregate cache
        # after a build error would replace a useful timeout/parser diagnostic
        # with the less actionable aggregate "incomplete" code.
        sidecars = (
            await _load_page_sidecars(session_factory, current_files)
            if failure is None
            else []
        )
        complete = failure is None and len(sidecars) == 3 and len(documents) == 3
        report = {
            "status": "PAGE_SIDECARS_OK" if complete else "FAILED",
            "task_id": task_id,
            "failure_stage": None if complete else (failure or {}).get(
                "failure_stage", "PAGE_SIDECAR_LOAD"
            ),
            "failure_code": None if complete else (failure or {}).get(
                "failure_code", "PAGE_SIDECAR_CACHE_INCOMPLETE"
            ),
            "external_calls": 0,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "page_sidecar_cache": {
                "owner": PAGE_LOCATION_CACHE_OWNER,
                "version": PAGE_LOCATION_CACHE_VERSION,
                "required_count": 3,
                "sidecar_count": len(sidecars),
                "documents": documents,
                "max_concurrency": 2,
                "per_file_timeout_seconds": 300,
            },
        }
    except (AppError, WorkflowError) as exc:
        details = exc.details if isinstance(exc.details, dict) else {}
        report = {
            "status": "FAILED",
            "task_id": task_id,
            "failure_stage": details.get("failure_stage", "PAGE_SIDECAR_PREFLIGHT"),
            "failure_code": details.get("failure_code", exc.code),
            "external_calls": 0,
        }
    except Exception as exc:
        report = {
            "status": "FAILED",
            "task_id": task_id,
            "failure_stage": "PAGE_SIDECAR_PREFLIGHT",
            "failure_code": type(exc).__name__,
            "external_calls": 0,
        }
    finally:
        await engine.dispose()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


async def _zero_call_regeneration_gate(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
) -> dict[str, Any]:
    """Verify every no-external-call prerequisite before requeueing."""

    async with session_factory() as session:
        task, source_result, source_files, current_files, _ = (
            await _read_existing_regeneration_task(
                session,
                task_id,
                require_pending=False,
            )
        )
        source_snapshot_records = await _load_source_snapshot_records(session, source_files)
        current_rows = (
            await session.execute(
                select(ExtractionCheckpointRow)
                .where(
                    ExtractionCheckpointRow.task_id == task_id,
                    ExtractionCheckpointRow.extraction_version
                    == DOCUMENT_EXTRACTION_CHECKPOINT_VERSION,
                    ExtractionCheckpointRow.status == "SUCCEEDED",
                    ExtractionCheckpointRow.file_sha256.in_(
                        {str(item.sha256) for item in current_files}
                    ),
                )
            )
        ).scalars().all()
    grouped_current: dict[str, list[ExtractionCheckpointRow]] = {}
    for row in current_rows:
        grouped_current.setdefault(str(row.file_sha256), []).append(row)
    if set(grouped_current) != {
        str(item.sha256) for item in current_files
    } or any(len(items) != 1 for items in grouped_current.values()):
        raise WorkflowError(
            "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
            "当前任务尚未物化完整文档抽取快照",
            details={
                "failure_stage": "ZERO_CALL_GATE",
                "failure_code": "CURRENT_SNAPSHOT_MATERIALIZATION_INCOMPLETE",
                "required_count": 3,
                "materialized_count": len(grouped_current),
                "fact_extraction_calls": 0,
            },
        )

    source_id_by_current_id = {
        current_file.id: source_file.id
        for source_file, current_file in zip(source_files, current_files, strict=True)
    }
    parser = ParserRegistry()
    evidence_rebound_count = 0
    for current_file in current_files:
        path = SAMPLE_DIR / current_file.file_name
        local_file = LocalFile(
            file_id=current_file.id,
            role=current_file.role.value,
            file_name=current_file.file_name,
            safe_url="",
            path=path,
            file_size=path.stat().st_size,
            sha256=str(current_file.sha256),
            detected_mime_type=DOCX_MIME,
        )
        document = await parser.parse(local_file)
        row = grouped_current[str(current_file.sha256)][0]
        rebound = _validated_document_checkpoint(
            document,
            row.value,
            source_file_id=source_id_by_current_id[current_file.id],
        )
        if rebound is None:
            raise WorkflowError(
                "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
                "当前任务快照证据无法完成正式重绑定",
                details={
                    "failure_stage": "ZERO_CALL_GATE",
                    "failure_code": "SNAPSHOT_EVIDENCE_REBIND_FAILED",
                    "fact_extraction_calls": 0,
                },
            )
        evidence_rebound_count += 1

    file_id_map = {
        source_file.id: current_file.id
        for source_file, current_file in zip(source_files, current_files, strict=True)
    }
    remapped_source = remap_file_references(
        source_result.result,
        file_id_map,
        task_id=task.id,
    )
    validate_file_reference_remap(
        remapped_source,
        old_file_ids={item.id for item in source_files},
        new_file_ids={item.id for item in current_files},
    )
    return {
        "source_snapshot_count": len(source_snapshot_records),
        "formal_context_count": 3,
        "evidence_rebound_count": evidence_rebound_count,
        "current_materialized_count": len(grouped_current),
        "page_sidecar_count": len(
            await _load_page_sidecars(session_factory, current_files)
        ),
        "old_file_reference_count": 0,
        "ocr_calls": 0,
        "fact_extraction_calls": 0,
        "expected_external_calls_before_worker": 0,
    }


async def requeue_existing(task_id: str, output: Path) -> dict[str, Any]:
    """Requeue the same failed task once, without creating a child task."""

    if task_id != REGENERATION_TASK_ID:
        report = {
            "status": "BLOCKED",
            "task_id": task_id,
            "failure_stage": "TASK_REQUEUE",
            "failure_code": "TASK_ID_NOT_ALLOWED",
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    base = get_settings()
    database_url = host_database_url(base.DATABASE_URL)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        gate = await _zero_call_regeneration_gate(session_factory, task_id)
        async with session_factory() as session:
            await TaskService(session, base).requeue_report_regeneration_after_checkpoint_fix(
                task_id
            )
        report = {
            "status": "REQUEUED",
            "task_id": task_id,
            "source_task_id": SOURCE_TASK_ID,
            "attempt_count": 2,
            "max_attempts": 3,
            "zero_call_gate": gate,
            "failure_stage": None,
            "failure_code": None,
        }
    except (AppError, WorkflowError) as exc:
        details = exc.details if isinstance(exc.details, dict) else {}
        report = {
            "status": "BLOCKED",
            "task_id": task_id,
            "source_task_id": SOURCE_TASK_ID,
            "failure_stage": details.get("failure_stage", "TASK_REQUEUE"),
            "failure_code": details.get("failure_code", exc.code),
            "exception_type": type(exc).__name__,
        }
    except Exception as exc:
        report = {
            "status": "FAILED",
            "task_id": task_id,
            "source_task_id": SOURCE_TASK_ID,
            "failure_stage": "TASK_REQUEUE",
            "failure_code": "TASK_REQUEUE_ERROR",
            "exception_type": type(exc).__name__,
        }
    finally:
        await engine.dispose()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


async def execute_existing(task_id: str, output: Path) -> dict[str, Any]:
    """Claim and execute the one already-created regeneration task exactly once."""

    if task_id != REGENERATION_TASK_ID:
        report = {
            "status": "BLOCKED",
            "task_id": task_id,
            "failure_stage": "TASK_PREFLIGHT",
            "failure_code": "TASK_ID_NOT_ALLOWED",
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    base = get_settings()
    database_url = host_database_url(base.DATABASE_URL)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    transport: CountingTransport | None = None
    started = time.monotonic()
    try:
        settings = base.model_copy(
            update={
                "DATABASE_URL": database_url,
                "TEMP_ROOT": str(Path(".real-diagnostic-temp") / "report-regeneration"),
                "LLM_ENABLED": True,
                "OCR_ENABLED": False,
                "DOCX_PAGE_LOCATION_ENABLED": True,
            }
        )
        if not settings.llm_configured:
            raise AppError(
                "REPORT_REGENERATION_NOT_CONFIGURED",
                "宿主机 LLM 配置未就绪",
                status_code=409,
                details={"failure_stage": "CONFIG_PREFLIGHT", "failure_code": "LLM_NOT_CONFIGURED"},
            )
        if not settings.document_parser_configured:
            raise AppError(
                "REPORT_REGENERATION_NOT_CONFIGURED",
                "DOCX 页码解析配置未就绪",
                status_code=409,
                details={
                    "failure_stage": "CONFIG_PREFLIGHT",
                    "failure_code": "DOCX_PAGE_PARSER_NOT_CONFIGURED",
                },
            )
        async with session_factory() as session:
            task, source_result, source_files, current_files, preflight = (
                await _read_existing_regeneration_task(session, task_id)
            )
            source_snapshot_records = await _load_source_snapshot_records(session, source_files)
            page_sidecars = await _load_page_sidecars(session_factory, current_files)
        runner, transport = await _build_regeneration_runner(
            settings,
            session_factory=session_factory,
            source_result=source_result,
            source_files=source_files,
            current_files=current_files,
            source_task_id=SOURCE_TASK_ID,
            source_snapshot_records=source_snapshot_records,
            page_sidecars=page_sidecars,
        )
        claimed = await runner.run_once()
        async with session_factory() as session:
            task = await session.get(CheckTask, task_id)
            stored = await session.get(TaskResult, task_id)
            current_checkpoint_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(ExtractionCheckpointRow)
                        .where(
                            ExtractionCheckpointRow.task_id == task_id,
                            ExtractionCheckpointRow.status == "SUCCEEDED",
                        )
                    )
                ).scalar_one()
            )
        if task is None:
            report = {
                "status": "FAILED",
                "task_id": task_id,
                "failure_stage": "RESULT_READ",
                "failure_code": "TASK_MISSING",
            }
        else:
            report = {
                "status": task.status.value,
                "source_task_id": SOURCE_TASK_ID,
                "task_id": task_id,
                "worker_claimed": claimed,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "snapshot_reuse": {
                    **preflight,
                    "current_checkpoint_count": current_checkpoint_count,
                    "fact_extraction_calls": 0,
                },
                "llm_http_calls": transport.http_calls,
                "llm_status_counts": dict(sorted(transport.statuses.items())),
                "llm_call_metadata": transport.safe_call_metadata,
                "console_tasks_path": "/console/#/tasks",
                "console_report_path": f"/console/#/reports/draft/{task_id}",
            }
            if stored is not None:
                report["result"] = safe_result_summary(stored.result)
            else:
                details = task.error_details if isinstance(task.error_details, dict) else {}
                report.update(
                    {
                        "failure_stage": details.get("failure_stage", task.stage.value),
                        "failure_code": details.get("failure_code", task.error_code),
                        "underlying_failure_code": details.get("underlying_failure_code"),
                    }
                )
    except (AppError, WorkflowError) as exc:
        details = exc.details if isinstance(exc.details, dict) else {}
        report = {
            "status": "BLOCKED" if isinstance(exc, AppError) else "FAILED",
            "source_task_id": SOURCE_TASK_ID,
            "task_id": task_id,
            "failure_stage": details.get("failure_stage"),
            "failure_code": details.get("failure_code", exc.code),
            "underlying_failure_code": details.get("underlying_failure_code"),
        }
    except Exception as exc:
        report = {
            "status": "FAILED",
            "source_task_id": SOURCE_TASK_ID,
            "task_id": task_id,
            "failure_stage": "REGENERATION_EXECUTION",
            "failure_code": "REPORT_REGENERATION_EXECUTION_ERROR",
            "exception_type": type(exc).__name__,
        }
    finally:
        if transport is not None:
            await transport.close_all()
        await engine.dispose()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


async def run(source_task_id: str, output: Path) -> dict[str, Any]:
    if source_task_id != SOURCE_TASK_ID:
        return {"status": "BLOCKED", "failure_code": "SOURCE_TASK_ID_NOT_ALLOWED"}
    base = get_settings()
    database_url = host_database_url(base.DATABASE_URL)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    transport: CountingTransport | None = None
    task_id: str | None = None
    execution_started = False
    started = time.monotonic()
    report: dict[str, Any]
    try:
        settings = base.model_copy(
            update={
                "DATABASE_URL": database_url,
                "TEMP_ROOT": str(Path(".real-diagnostic-temp") / "report-regeneration"),
                "LLM_ENABLED": True,
                "OCR_ENABLED": False,
                "DOCX_PAGE_LOCATION_ENABLED": True,
            }
        )
        if not settings.llm_configured:
            raise AppError(
                "REPORT_REGENERATION_NOT_CONFIGURED",
                "宿主机 LLM 配置未就绪，未创建再生成任务",
                status_code=409,
                details={"failure_stage": "CONFIG_PREFLIGHT", "failure_code": "LLM_NOT_CONFIGURED"},
            )
        if not settings.document_parser_configured:
            raise AppError(
                "REPORT_REGENERATION_NOT_CONFIGURED",
                "DOCX 页码解析配置未就绪，未创建再生成任务",
                status_code=409,
                details={
                    "failure_stage": "CONFIG_PREFLIGHT",
                    "failure_code": "DOCX_PAGE_PARSER_NOT_CONFIGURED",
                },
            )
        async with session_factory() as session:
            source, source_result, source_files, preflight = await _read_preflight(
                session, source_task_id
            )
            accepted = await TaskService(session, base).create_report_regeneration(
                source_task_id, new_request_id()
            )
            task_id = accepted.task_id
        async with session_factory() as session:
            current_files = (
                await session.execute(select(TaskFile).where(TaskFile.task_id == task_id))
            ).scalars().all()
            source_snapshot_records = await _load_source_snapshot_records(session, source_files)
        current_files = sorted(current_files, key=lambda item: item.sort_order)
        runner, transport = await _build_regeneration_runner(
            settings,
            session_factory=session_factory,
            source_result=source_result,
            source_files=source_files,
            current_files=current_files,
            source_task_id=source_task_id,
            source_snapshot_records=source_snapshot_records,
        )
        execution_started = True
        claimed = await runner.run_once()
        async with session_factory() as session:
            task = await session.get(CheckTask, task_id)
            stored = await session.get(TaskResult, task_id)
            current_checkpoint_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(ExtractionCheckpointRow)
                        .where(
                            ExtractionCheckpointRow.task_id == task_id,
                            ExtractionCheckpointRow.status == "SUCCEEDED",
                        )
                    )
                ).scalar_one()
            )
        if task is None:
            report = {
                "status": "FAILED",
                "failure_stage": "RESULT_READ",
                "failure_code": "TASK_MISSING",
            }
        else:
            report = {
                "status": task.status.value,
                "source_task_id": source_task_id,
                "task_id": task_id,
                "worker_claimed": claimed,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "snapshot_reuse": {
                    **preflight,
                    "current_checkpoint_count": current_checkpoint_count,
                    "fact_extraction_calls": 0,
                },
                "llm_http_calls": transport.http_calls,
                "llm_status_counts": dict(sorted(transport.statuses.items())),
                "llm_call_metadata": transport.safe_call_metadata,
                "console_tasks_path": "/console/#/tasks",
                "console_report_path": f"/console/#/reports/draft/{task_id}",
            }
            if stored is not None:
                report["result"] = safe_result_summary(stored.result)
            else:
                details = task.error_details if isinstance(task.error_details, dict) else {}
                report.update(
                    {
                        "failure_stage": details.get("failure_stage", task.stage.value),
                        "failure_code": details.get("failure_code", task.error_code),
                        "underlying_failure_code": details.get("underlying_failure_code"),
                    }
                )
    except (AppError, WorkflowError) as exc:
        details = exc.details if isinstance(exc.details, dict) else {}
        report = {
            "status": "BLOCKED" if isinstance(exc, AppError) else "FAILED",
            "source_task_id": source_task_id,
            "task_id": task_id,
            "failure_stage": details.get("failure_stage"),
            "failure_code": details.get("failure_code", exc.code),
            "underlying_failure_code": details.get("underlying_failure_code"),
        }
    except Exception as exc:
        report = {
            "status": "FAILED",
            "source_task_id": source_task_id,
            "task_id": task_id,
            "failure_stage": "WORKER_EXECUTION" if execution_started else "REGENERATION_SETUP",
            "failure_code": "REPORT_REGENERATION_SETUP_ERROR",
            "exception_type": type(exc).__name__,
        }
    finally:
        if transport is not None:
            await transport.close_all()
        await engine.dispose()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-task-id", default=SOURCE_TASK_ID)
    parser.add_argument("--task-id")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--setup-only", action="store_true")
    modes.add_argument("--snapshot-only", action="store_true")
    modes.add_argument("--page-sidecar-only", action="store_true")
    modes.add_argument("--requeue-existing", action="store_true")
    modes.add_argument("--execute-existing", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.setup_only:
        report = asyncio.run(setup_only(args.task_id or REGENERATION_TASK_ID, args.output))
    elif args.snapshot_only:
        report = asyncio.run(snapshot_only(args.task_id or REGENERATION_TASK_ID, args.output))
    elif args.page_sidecar_only:
        report = asyncio.run(page_sidecar_only(args.task_id or REGENERATION_TASK_ID, args.output))
    elif args.requeue_existing:
        report = asyncio.run(requeue_existing(args.task_id or REGENERATION_TASK_ID, args.output))
    elif args.execute_existing:
        report = asyncio.run(execute_existing(args.task_id or REGENERATION_TASK_ID, args.output))
    else:
        report = asyncio.run(run(args.source_task_id, args.output))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("status") in {
        TaskStatus.SUCCEEDED.value,
        "SETUP_OK",
        "SNAPSHOT_OK",
        "PAGE_SIDECARS_OK",
        "REQUEUED",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
