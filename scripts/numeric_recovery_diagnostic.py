"""Run a safe, read-only numeric recovery Canary.

The Canary does not guess a failed historical batch. It parses only the
supplied local DOCX, reads source-task/checkpoint metadata, and sends at most
one request for each of the three largest exact-cache-miss numeric batches.
It never writes checkpoints or creates/retries a task.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.adapters.llm.base import ContractLlmClient
from app.adapters.llm.openai_client import OpenAIContractLlmClient
from app.core.config import Settings
from app.core.errors import WorkflowError
from app.db.models import CheckTask
from app.db.models import ExtractionCheckpoint as CheckpointRow
from app.documents.parsers import DocxParser
from app.draft_review.checkpoints import SqlAlchemyExtractionCheckpointStore
from app.draft_review.extraction import (
    _checkpoint_batch_id,
    _checkpoint_payload_digest,
    _safe_failure_code,
    _with_checkpoint_identity,
    numeric_recovery_blocks,
)
from app.draft_review.facts import (
    NUMERIC_EXTRACTION_VERSION,
    build_document_overview_payload,
    build_numeric_candidate_payload,
    expand_numeric_candidate_response,
    plan_numeric_document_batches,
    rehydrate_numeric_fact_evidence,
)
from app.services.downloader import DOCX_MIME, LocalFile

DEFAULT_SOURCE_TASK_ID = "tsk_01M10YQ3Z99FB3AP5PAKN5PNE5"
SAFE_FAILURE_KEYS = {
    "failure_stage",
    "chain",
    "file_id",
    "batch_depth",
    "unit_count",
    "batch_id",
    "failure_code",
}


def host_database_url(database_url: str) -> str:
    url = make_url(database_url)
    if url.host == "postgres" and url.port == 5432:
        return url.set(host="127.0.0.1", port=15432).render_as_string(
            hide_password=False
        )
    return url.render_as_string(hide_password=False)


def safe_task_details(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in SAFE_FAILURE_KEYS if key in value}


def prepare_numeric_plans(document, settings: Settings) -> list[dict[str, Any]]:
    plans = plan_numeric_document_batches(
        document,
        max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
        max_numeric_candidates=min(settings.LLM_EXTRACTION_MAX_NUMERIC_CANDIDATES, 24),
        max_numeric_units=settings.LLM_EXTRACTION_MAX_NUMERIC_UNITS,
        estimated_output_token_limit=min(
            settings.LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS, 2000
        ),
    )
    for plan in plans:
        plan["planned_batch_count"] = len(plans)
        plan["payload"].update(
            {
                "batch_depth": 0,
                "parent_batch_id": None,
                "planned_batch_count": len(plans),
                "extraction_version": plan["extraction_version"],
            }
        )
        _with_checkpoint_identity(plan, document)
    return plans


async def checkpoint_counts(
    session_factory, source_task_id: str
) -> dict[str, int]:
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    CheckpointRow.extraction_version,
                    func.count().label("count"),
                )
                .where(
                    CheckpointRow.task_id == source_task_id,
                    CheckpointRow.status == "SUCCEEDED",
                )
                .group_by(CheckpointRow.extraction_version)
            )
        ).all()
    return {str(version): int(count) for version, count in rows}


async def source_task_info(session_factory, source_task_id: str) -> tuple[dict[str, Any], Any]:
    async with session_factory() as session:
        task = (
            await session.execute(
                select(CheckTask)
                .options(selectinload(CheckTask.files))
                .where(CheckTask.id == source_task_id)
            )
        ).scalar_one_or_none()
    if task is None:
        raise WorkflowError("NUMERIC_SOURCE_TASK_NOT_FOUND", "来源任务不存在")
    target_files = [file for file in task.files if str(file.role) == "TARGET"]
    if len(target_files) != 1:
        raise WorkflowError("NUMERIC_SOURCE_TARGET_NOT_UNIQUE", "来源任务目标文件不唯一")
    return {
        "task_id": task.id,
        "status": str(task.status),
        "failure": safe_task_details(task.error_details),
    }, target_files[0]


async def strict_hit_counts(
    store: SqlAlchemyExtractionCheckpointStore,
    document,
    plans: list[dict[str, Any]],
    source_task_id: str,
) -> tuple[int, int, bool]:
    hits = 0
    misses = 0
    profile_payload = build_document_overview_payload(document)
    profile_payload.update(
        {
            "batch_id": _checkpoint_batch_id(document, document.blocks, "profile-v2"),
            "extraction_version": "profile-v2",
        }
    )
    profile = await store.load(
        profile_payload["batch_id"],
        task_id=source_task_id,
        file_sha256=document.sha256,
        extraction_version="profile-v2",
        payload_digest=_checkpoint_payload_digest(profile_payload),
        source_task_id=source_task_id,
    )
    profile_hit = profile is not None
    hits += int(profile_hit)
    for plan in plans:
        checkpoint = await store.load(
            plan["batch_id"],
            task_id=source_task_id,
            file_sha256=document.sha256,
            extraction_version=NUMERIC_EXTRACTION_VERSION,
            payload_digest=_checkpoint_payload_digest(plan["payload"]),
            source_task_id=source_task_id,
        )
        if checkpoint is None:
            misses += 1
        else:
            hits += 1
    return hits, misses, profile_hit


async def strict_numeric_miss_plans(
    store: SqlAlchemyExtractionCheckpointStore,
    document,
    plans: list[dict[str, Any]],
    source_task_id: str,
) -> list[dict[str, Any]]:
    misses: list[dict[str, Any]] = []
    for plan in plans:
        checkpoint = await store.load(
            plan["batch_id"],
            task_id=source_task_id,
            file_sha256=document.sha256,
            extraction_version=NUMERIC_EXTRACTION_VERSION,
            payload_digest=_checkpoint_payload_digest(plan["payload"]),
            source_task_id=source_task_id,
        )
        if checkpoint is None:
            misses.append(plan)
    return misses


def worst_numeric_plans(
    plans: list[dict[str, Any]], *, limit: int = 3
) -> list[dict[str, Any]]:
    """Select largest new requests using only safe structural metrics."""

    ranked = sorted(
        plans,
        key=lambda plan: (
            -len(json.dumps(plan["payload"], ensure_ascii=False, separators=(",", ":"))),
            -int(plan.get("numeric_candidate_count", 0)),
            -len(plan.get("blocks", [])),
            str(plan.get("batch_id", "")),
        ),
    )
    return ranked[:limit]


async def run_numeric_canary(
    client: ContractLlmClient,
    document,
    plans: list[dict[str, Any]],
) -> dict[str, Any]:
    """Send at most one validated request for each of three new batches."""

    selected = worst_numeric_plans(plans, limit=3)
    attempted: list[dict[str, Any]] = []
    for plan in selected:
        payload = plan["payload"]
        attempted.append(
            {
                "batch_id": plan["batch_id"],
                "unit_count": len(plan["blocks"]),
                "candidate_count": int(plan.get("numeric_candidate_count", 0)),
            }
        )
        try:
            result = await client.extract_numeric_candidates(
                payload, allow_structure_correction=False
            )
            facts, _classified = expand_numeric_candidate_response(payload, result.value)
            rehydrate_numeric_fact_evidence(document, facts)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return {
                "status": "FAILED",
                "canary_batches": attempted,
                "llm_calls": len(attempted),
                "failure_stage": "NUMERIC_CANARY",
                "failure_code": _safe_failure_code(exc),
            }
    if len(selected) < 3:
        return {
            "status": "SAFE_STOP",
            "canary_batches": attempted,
            "llm_calls": len(attempted),
            "failure_stage": "NUMERIC_CANARY_SELECTION",
            "failure_code": "NUMERIC_CANARY_INSUFFICIENT_NEW_BATCHES",
        }
    return {
        "status": "SUCCEEDED",
        "canary_batches": attempted,
        "llm_calls": len(attempted),
        "failure_stage": None,
        "failure_code": None,
    }


async def run_numeric_probe(
    client: ContractLlmClient,
    document,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Exercise one exact batch through the fixed 6 -> 3 -> 1 path."""

    calls = 0
    current_blocks = list(plan["blocks"])
    last_code = ""
    attempted_units: list[int] = []
    while calls < 3:
        calls += 1
        attempted_units.append(len(current_blocks))
        batch_id = _checkpoint_batch_id(
            document,
            current_blocks,
            NUMERIC_EXTRACTION_VERSION,
            variant=f"diagnostic_{calls}",
        )
        payload = build_numeric_candidate_payload(
            document, current_blocks, batch_id=batch_id
        )
        try:
            result = await client.extract_numeric_candidates(
                payload, allow_structure_correction=False
            )
            facts, _classified = expand_numeric_candidate_response(payload, result.value)
            rehydrate_numeric_fact_evidence(document, facts)
            return {
                "status": "SUCCEEDED",
                "llm_calls": calls,
                "attempted_unit_counts": attempted_units,
                "failure_code": None,
            }
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            last_code = _safe_failure_code(exc)
            if last_code != "LLM_OUTPUT_TRUNCATED":
                break
            children = numeric_recovery_blocks(current_blocks, last_code)
            if not children:
                break
            # Continue one deterministic branch for a bounded diagnosis. The
            # production workflow dispatches every returned child; this probe
            # only tests that each next request is smaller and newly identified.
            current_blocks = children[0]
    return {
        "status": "FAILED",
        "llm_calls": calls,
        "attempted_unit_counts": attempted_units,
        "failure_code": last_code or "LLM_OUTPUT_TRUNCATED",
    }


async def diagnose(path: Path, source_task_id: str) -> dict[str, Any]:
    raw = path.read_bytes()
    settings = Settings(
        DOCX_PAGE_LOCATION_ENABLED=False,
        OCR_ENABLED=False,
        OCR_HTTP_RETRY_ATTEMPTS=0,
    )
    database_url = host_database_url(settings.DATABASE_URL)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        task_info, source_file = await source_task_info(session_factory, source_task_id)
        local_sha = hashlib.sha256(raw).hexdigest()
        if source_file.sha256 and source_file.sha256 != local_sha:
            return {
                "ok": False,
                "status": "SAFE_STOP",
                "source_task": task_info,
                "source_file_id": source_file.id,
                "sha256": local_sha,
                "profile_checkpoint_count": 0,
                "numeric_checkpoint_count": 0,
                "strict_hit_count": 0,
                "cache_miss_count": 0,
                "planned_batch_count": 0,
                "max_unit_count": 0,
                "llm_calls": 0,
                "failure_stage": "SOURCE_FILE_VALIDATION",
                "failure_code": "SOURCE_FILE_SHA_MISMATCH",
            }
        local_file = LocalFile(
            file_id=source_file.id,
            role="TARGET",
            file_name=path.name,
            safe_url="local-diagnostic://redacted",
            path=path,
            file_size=len(raw),
            sha256=local_sha,
            detected_mime_type=DOCX_MIME,
        )
        document = await DocxParser().parse(local_file)
        plans = prepare_numeric_plans(document, settings)
        store = SqlAlchemyExtractionCheckpointStore(session_factory)
        hits, misses, profile_hit = await strict_hit_counts(
            store, document, plans, source_task_id
        )
        miss_plans = await strict_numeric_miss_plans(
            store, document, plans, source_task_id
        )
        counts = await checkpoint_counts(session_factory, source_task_id)
        result = await run_numeric_canary(
            OpenAIContractLlmClient(settings), document, miss_plans
        )
        return {
            "ok": result["status"] == "SUCCEEDED",
            "status": result["status"],
            "source_task": task_info,
            "source_file_id": source_file.id,
            "sha256": local_sha,
            "profile_checkpoint_count": counts.get("profile-v2", 0),
            "numeric_checkpoint_count": counts.get(NUMERIC_EXTRACTION_VERSION, 0),
            "strict_hit_count": hits,
            "cache_miss_count": misses,
            "profile_strict_hit": profile_hit,
            "planned_batch_count": len(plans),
            "max_unit_count": max((len(plan["blocks"]) for plan in plans), default=0),
            "llm_calls": result["llm_calls"],
            "canary_batches": result["canary_batches"],
            "failure_stage": result["failure_stage"],
            "failure_code": result["failure_code"],
        }
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--source-task-id", default=DEFAULT_SOURCE_TASK_ID)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    path = args.path.resolve()
    if not path.is_file() or path.suffix.casefold() != ".docx":
        parser.error("diagnostic file must be an existing DOCX")
    try:
        result = asyncio.run(diagnose(path, args.source_task_id))
    except WorkflowError as exc:
        result = {
            "ok": False,
            "status": "SAFE_STOP",
            "failure_stage": "DIAGNOSTIC_SETUP",
            "failure_code": _safe_failure_code(exc),
        }
    except Exception as exc:
        result = {
            "ok": False,
            "status": "SAFE_STOP",
            "failure_stage": "DIAGNOSTIC_SETUP",
            "failure_code": type(exc).__name__,
        }
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
    print(encoded)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
