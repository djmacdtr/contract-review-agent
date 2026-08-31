"""Run one exact, read-only Mapping canary for the failed PDF reference.

The canary rebuilds the production full-mapping payload from the failed task's
validated document snapshots.  It never downloads, parses with OCR, writes a
checkpoint, or mutates task state.  Output contains aggregate diagnostics only.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.llm.openai_client import LlmClientError, OpenAIContractLlmClient
from app.adapters.llm.schemas import DocumentFactExtraction
from app.core.config import get_settings
from app.db.models import CheckTask, ExtractionCheckpoint, TaskFile
from app.draft_review.facts import stable_fact_id
from app.workflows.draft_review import _compact_mapping_fact, target_fact_catalog

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_TASK_ID = "tsk_01M16W32545DN9NC65XXEPJG1D"
PDF_REFERENCE_INDEX = 3
CHECKPOINT_VERSION = "document-extraction-v1"
LOCAL_FILES = (
    REPO_ROOT.parent / "04 合同素材文件" / "02 合同起草版本" / "融资租赁合同（回租）.docx",
    REPO_ROOT.parent / "04 合同素材文件" / "01 合同制式模版" / "融资租赁合同（回租）.docx",
    REPO_ROOT.parent
    / "04 合同素材文件"
    / "04 基准材料文件"
    / "法律合规风险报告-XX公司合规报告.docx",
    REPO_ROOT.parent
    / "04 合同素材文件"
    / "04 基准材料文件"
    / "评审会评审意见表（对内版).pdf",
    REPO_ROOT.parent / "04 合同素材文件" / "04 基准材料文件" / "项目方案确认函.docx",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _host_database_url(value: str) -> str:
    url = make_url(value)
    if url.host == "postgres" and url.port == 5432:
        url = url.set(host="127.0.0.1", port=15432)
    return url.render_as_string(hide_password=False)


def _safe_error(exc: BaseException) -> dict[str, Any]:
    details: dict[str, Any] = {
        "failure_code": (
            getattr(exc, "failure_code", None)
            or getattr(exc, "code", None)
            or type(exc).__name__
        )
    }
    for key in (
        "finish_reason",
        "content_chars",
        "reasoning_content_chars",
        "max_tokens",
        "http_status",
        "code_fence",
        "json_error_position",
        "request_attempts",
        "structure_retries",
    ):
        value = getattr(exc, key, None)
        if isinstance(value, bool) or (isinstance(value, int) and value >= 0) or (
            isinstance(value, str) and value
        ):
            details[key] = value
    usage = getattr(exc, "usage", None)
    if isinstance(usage, dict):
        safe_usage = {
            key: value
            for key, value in usage.items()
            if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
            and type(value) is int
            and value >= 0
        }
        if safe_usage:
            details["usage"] = safe_usage
    return details


def _settings_for_canary() -> Any:
    settings = get_settings()
    return settings.model_copy(
        update={
            "DATABASE_URL": _host_database_url(settings.DATABASE_URL),
            "LLM_HTTP_RETRY_ATTEMPTS": 0,
            "LLM_STRUCTURE_RETRY_ATTEMPTS": 0,
            "LLM_NATIVE_STRUCTURED_OUTPUT": True,
            "LLM_RESPONSE_FORMAT": "json_schema",
            "LLM_EXTRACTION_MODEL": "GLM-5.3-Flash",
            "LLM_REVIEW_MODEL": "GLM-5.3-Flash",
            "LLM_MAX_CONCURRENCY": 1,
        }
    )


async def _load_source(
    session: AsyncSession,
) -> tuple[CheckTask, list[TaskFile], dict[str, ExtractionCheckpoint]]:
    task = await session.get(CheckTask, SOURCE_TASK_ID)
    if task is None:
        raise RuntimeError("SOURCE_TASK_NOT_FOUND")
    files = list(
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
    if len(files) != len(LOCAL_FILES):
        raise RuntimeError("SOURCE_FILE_COUNT_INVALID")
    checkpoints = list(
        (
            await session.execute(
                select(ExtractionCheckpoint).where(
                    ExtractionCheckpoint.task_id == SOURCE_TASK_ID,
                    ExtractionCheckpoint.extraction_version == CHECKPOINT_VERSION,
                    ExtractionCheckpoint.status == "SUCCEEDED",
                )
            )
        )
        .scalars()
        .all()
    )
    by_sha: dict[str, list[ExtractionCheckpoint]] = {}
    for checkpoint in checkpoints:
        by_sha.setdefault(checkpoint.file_sha256, []).append(checkpoint)
    matched: dict[str, ExtractionCheckpoint] = {}
    for path in LOCAL_FILES:
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError("LOCAL_FILE_MISSING")
        digest = _sha256(path)
        candidates = by_sha.get(digest, [])
        if len(candidates) != 1:
            raise RuntimeError("DOCUMENT_SNAPSHOT_NOT_UNIQUE")
        matched[digest] = candidates[0]
    return task, files, matched


def _build_payload(
    files: list[TaskFile],
    snapshots: dict[str, ExtractionCheckpoint],
) -> tuple[dict[str, Any], dict[str, Any]]:
    extractions: list[DocumentFactExtraction] = []
    for path in LOCAL_FILES:
        checkpoint = snapshots[_sha256(path)]
        extraction = DocumentFactExtraction.model_validate(checkpoint.value)
        file_index = len(extractions)
        expected_file_id = files[file_index].id
        if extraction.profile.file_id != expected_file_id or any(
            fact.source_file_id != expected_file_id for fact in extraction.facts
        ):
            raise RuntimeError("DOCUMENT_SNAPSHOT_FILE_ID_MISMATCH")
        extractions.append(extraction)

    target = extractions[0]
    reference = extractions[PDF_REFERENCE_INDEX]
    threshold = _settings_for_canary().LLM_CONSENSUS_MIN_CONFIDENCE
    accepted_target = [fact for fact in target.facts if fact.confidence >= threshold]
    accepted_reference = [fact for fact in reference.facts if fact.confidence >= threshold]
    accepted_target_ids = {stable_fact_id(fact) for fact in accepted_target}
    catalog = [
        _compact_mapping_fact(item)
        for fact, item in zip(target.facts, target_fact_catalog(target), strict=True)
        if stable_fact_id(fact) in accepted_target_ids
    ]
    payload = {
        "reference_file_id": files[PDF_REFERENCE_INDEX].id,
        "reference_profile": reference.profile.model_dump(mode="json"),
        "target_facts": catalog,
        "reference_facts": [
            _compact_mapping_fact(fact.model_dump(mode="json"))
            for fact in accepted_reference
        ],
    }
    summary = {
        "target_file_id": files[0].id,
        "reference_file_id": files[PDF_REFERENCE_INDEX].id,
        "target_fact_count": len(catalog),
        "reference_fact_count": len(accepted_reference),
        "source_snapshot_count": len(snapshots),
        "source_snapshot_version": CHECKPOINT_VERSION,
    }
    return payload, summary


async def run(output: Path) -> dict[str, Any]:
    settings = _settings_for_canary()
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as session:
            task, files, snapshots = await _load_source(session)
            if task.task_type.value != "DRAFT_REVIEW":
                raise RuntimeError("SOURCE_TASK_TYPE_INVALID")
            payload, summary = _build_payload(files, snapshots)
        client = OpenAIContractLlmClient(settings)
        try:
            result = await client.map_facts(payload)
        except LlmClientError as exc:
            report = {
                "status": "FAILED",
                "source_task_id": SOURCE_TASK_ID,
                "canary_file_index": PDF_REFERENCE_INDEX,
                **summary,
                "failure_stage": "FACT_MAPPING",
                "diagnostics": _safe_error(exc),
            }
        else:
            report = {
                "status": "SUCCEEDED",
                "source_task_id": SOURCE_TASK_ID,
                "canary_file_index": PDF_REFERENCE_INDEX,
                **summary,
                "mapping_count": len(result.value.get("mappings", [])),
                "missing_requirement_count": len(
                    result.value.get("missing_requirements", [])
                ),
                "configured_model": result.configured_model,
                "actual_model": result.actual_model,
                "request_attempts": result.request_attempts,
                "structure_retries": result.structure_retries,
                "finish_reason": result.finish_reason,
                "response_format": result.response_format,
                "response_metadata": result.response_metadata,
            }
    except RuntimeError as exc:
        report = {
            "status": "BLOCKED",
            "source_task_id": SOURCE_TASK_ID,
            "canary_file_index": PDF_REFERENCE_INDEX,
            "failure_stage": "SNAPSHOT_REBUILD",
            "failure_code": str(exc),
            "llm_calls": 0,
        }
    finally:
        await engine.dispose()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / ".real-diagnostic-temp" / "mapping-pdf-canary-20260829.json",
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.output)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
