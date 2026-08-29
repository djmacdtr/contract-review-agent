"""Run a safe, read-only numeric recovery Canary.

The diagnostic parses only the supplied local DOCX, reads source-task and
checkpoint metadata, and never writes checkpoints or creates/retries a task.
When ``--batch-id`` is provided, exactly that failure batch is reconstructed;
no fallback selection is allowed.
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
from app.adapters.llm.openai_client import (
    LlmClientError,
    OpenAIContractLlmClient,
    _numeric_candidate_response_summary,
)
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
    "numeric_candidate_count",
    "failure_code",
}
SAFE_RESPONSE_METADATA_KEYS = {
    "finish_reason",
    "content_chars",
    "reasoning_content_chars",
    "usage",
    "max_tokens",
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


def safe_response_metadata(value: Any) -> dict[str, Any]:
    """Expose only aggregate response diagnostics, never response content."""

    if not isinstance(value, dict):
        return {}
    metadata = {
        key: value[key]
        for key in SAFE_RESPONSE_METADATA_KEYS
        if key in value
    }
    usage = metadata.get("usage")
    if isinstance(usage, dict):
        metadata["usage"] = {
            key: int(item)
            for key, item in usage.items()
            if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
            and type(item) is int
            and item >= 0
        }
    elif usage is not None:
        metadata.pop("usage", None)
    return metadata


def safe_llm_error_metadata(exc: BaseException) -> dict[str, Any]:
    if not isinstance(exc, LlmClientError):
        return {}
    return safe_response_metadata(
        {
            "finish_reason": exc.finish_reason,
            "content_chars": exc.content_chars,
            "reasoning_content_chars": exc.reasoning_content_chars,
            "usage": exc.usage,
            "max_tokens": exc.max_tokens,
        }
    )


def prepare_numeric_plans(
    document, settings: Settings, *, include_empty: bool = False
) -> list[dict[str, Any]]:
    plans = plan_numeric_document_batches(
        document,
        max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
        max_numeric_candidates=min(settings.LLM_EXTRACTION_MAX_NUMERIC_CANDIDATES, 24),
        max_numeric_units=settings.LLM_EXTRACTION_MAX_NUMERIC_UNITS,
        estimated_output_token_limit=min(
            settings.LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS, 2000
        ),
        include_empty=include_empty,
    )
    for plan in plans:
        planned_batch_count = int(
            plan.get("checkpoint_planned_batch_count", len(plans))
        )
        plan["planned_batch_count"] = planned_batch_count
        plan["payload"].update(
            {
                "batch_depth": 0,
                "parent_batch_id": None,
                "planned_batch_count": planned_batch_count,
                "extraction_version": plan["extraction_version"],
            }
        )
        _with_checkpoint_identity(plan, document)
    return plans


def reconstruct_exact_numeric_plan(
    document,
    plans: list[dict[str, Any]],
    requested_batch_id: str,
) -> list[dict[str, Any]]:
    """Rebuild an exact historical singleton by its deterministic batch ID.

    Recovery children are not part of the current initial planner output.  A
    singleton can still be reconstructed safely when its full content-derived
    ID matches exactly; no position, order, or fuzzy text inference is used.
    """

    matches = [plan for plan in plans if plan["batch_id"] == requested_batch_id]
    if matches:
        return matches

    candidates: list[dict[str, Any]] = []
    seen_block_ids: set[str] = set()
    for parent in plans:
        for block in parent["blocks"]:
            if block.block_id in seen_block_ids:
                continue
            seen_block_ids.add(block.block_id)
            batch_id = _checkpoint_batch_id(
                document,
                [block],
                NUMERIC_EXTRACTION_VERSION,
            )
            if batch_id != requested_batch_id:
                continue
            payload = build_numeric_candidate_payload(
                document,
                [block],
                batch_id=batch_id,
            )
            payload.update(
                {
                    "batch_depth": 1,
                    "parent_batch_id": None,
                    "planned_batch_count": parent.get("planned_batch_count", 0),
                    "extraction_version": NUMERIC_EXTRACTION_VERSION,
                }
            )
            candidates.append(
                {
                    "batch_id": batch_id,
                    "document_id": document.file_id,
                    "file_sha256": document.sha256,
                    "blocks": [block],
                    "unit_ids": [block.block_id],
                    "payload": payload,
                    "numeric_candidate_count": len(payload["numeric_candidates"]),
                    "depth": 1,
                    "parent_batch_id": None,
                    "planned_batch_count": parent.get("planned_batch_count", 0),
                    "extraction_version": NUMERIC_EXTRACTION_VERSION,
                }
            )
    return candidates


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
    llm_calls = 0
    for plan in selected:
        payload = plan["payload"]
        attempted.append(
            {
                "batch_id": plan["batch_id"],
                "unit_count": len(plan["blocks"]),
                "candidate_count": int(plan.get("numeric_candidate_count", 0)),
            }
        )
        if int(plan.get("numeric_candidate_count", 0)) == 0:
            continue
        try:
            llm_calls += 1
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
                "llm_calls": llm_calls,
                "failure_stage": "NUMERIC_CANARY",
                "failure_code": _safe_failure_code(exc),
            }
    if len(selected) < 3:
        return {
            "status": "SAFE_STOP",
            "canary_batches": attempted,
            "llm_calls": llm_calls,
            "failure_stage": "NUMERIC_CANARY_SELECTION",
            "failure_code": "NUMERIC_CANARY_INSUFFICIENT_NEW_BATCHES",
        }
    if not any(int(plan.get("numeric_candidate_count", 0)) > 0 for plan in selected):
        return {
            "status": "SKIPPED_EMPTY",
            "canary_batches": attempted,
            "llm_calls": llm_calls,
            "failure_stage": None,
            "failure_code": None,
        }
    return {
        "status": "SUCCEEDED",
        "canary_batches": attempted,
        "llm_calls": llm_calls,
        "failure_stage": None,
        "failure_code": None,
    }


async def run_exact_numeric_canary(
    client: ContractLlmClient,
    document,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Send one validated request for one explicitly identified batch."""

    attempted = {
        "batch_id": plan["batch_id"],
        "unit_count": len(plan["blocks"]),
        "candidate_count": int(plan.get("numeric_candidate_count", 0)),
    }
    if int(plan.get("numeric_candidate_count", 0)) == 0:
        return {
            "status": "SKIPPED_EMPTY",
            "canary_batches": [attempted],
            "llm_calls": 0,
            "failure_stage": None,
            "failure_code": None,
        }
    try:
        result = await client.extract_numeric_candidates(
            plan["payload"], allow_structure_correction=False
        )
        facts, _classified = expand_numeric_candidate_response(plan["payload"], result.value)
        rehydrate_numeric_fact_evidence(document, facts)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        response_summary = getattr(exc, "validation_summary", None)
        if not isinstance(response_summary, dict):
            response_summary = _numeric_candidate_response_summary({}, plan["payload"])
        return {
            "status": "FAILED",
            "canary_batches": [attempted],
            "llm_calls": 1,
                "failure_stage": "NUMERIC_CANARY",
                "failure_code": _safe_failure_code(exc),
                **safe_llm_error_metadata(exc),
                **{
                key: int(response_summary.get(key, 0))
                for key in (
                    "expected_count",
                    "returned_count",
                    "missing_index_count",
                    "duplicate_index_count",
                    "invalid_index_count",
                )
            },
        }
    return {
        "status": "SUCCEEDED",
        "canary_batches": [attempted],
        "llm_calls": 1,
        "failure_stage": None,
        "failure_code": None,
        **safe_response_metadata(result.response_metadata),
        "configured_model": result.configured_model,
        "actual_model": result.actual_model,
        "request_attempts": result.request_attempts,
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


async def diagnose(
    path: Path,
    source_task_id: str,
    requested_batch_id: str | None = None,
    model_override: str | None = None,
) -> dict[str, Any]:
    raw = path.read_bytes()
    settings = Settings(
        DOCX_PAGE_LOCATION_ENABLED=False,
        OCR_ENABLED=False,
        OCR_HTTP_RETRY_ATTEMPTS=0,
        LLM_HTTP_RETRY_ATTEMPTS=0,
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
        all_plans = prepare_numeric_plans(document, settings, include_empty=True)
        store = SqlAlchemyExtractionCheckpointStore(session_factory)
        hits, misses, profile_hit = await strict_hit_counts(
            store, document, plans, source_task_id
        )
        miss_plans = await strict_numeric_miss_plans(
            store, document, plans, source_task_id
        )
        counts = await checkpoint_counts(session_factory, source_task_id)
        if requested_batch_id is not None:
            failure_batch_id = task_info["failure"].get("batch_id")
            if failure_batch_id != requested_batch_id:
                result = {
                    "status": "SAFE_STOP",
                    "canary_batches": [],
                    "llm_calls": 0,
                    "failure_stage": "NUMERIC_CANARY_SELECTION",
                    "failure_code": "NUMERIC_CANARY_FAILURE_BATCH_MISMATCH",
                }
            else:
                exact_plans = reconstruct_exact_numeric_plan(
                    document,
                    all_plans,
                    requested_batch_id,
                )
                if len(exact_plans) != 1:
                    result = {
                        "status": "SAFE_STOP",
                        "canary_batches": [],
                        "llm_calls": 0,
                        "failure_stage": "NUMERIC_CANARY_SELECTION",
                        "failure_code": "NUMERIC_CANARY_BATCH_NOT_UNIQUE",
                    }
                else:
                    result = await run_exact_numeric_canary(
                        OpenAIContractLlmClient(
                            settings, numeric_model_override=model_override
                        ),
                        document,
                        exact_plans[0],
                    )
        else:
            result = await run_numeric_canary(
                OpenAIContractLlmClient(
                    settings, numeric_model_override=model_override
                ),
                document,
                miss_plans,
            )
        return {
            "ok": result["status"] in {"SUCCEEDED", "SKIPPED_EMPTY"},
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
            **{
                key: result[key]
                for key in (
                    "finish_reason",
                    "content_chars",
                    "reasoning_content_chars",
                    "usage",
                    "max_tokens",
                    "configured_model",
                    "actual_model",
                    "request_attempts",
                )
                if key in result
            },
        }
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--source-task-id", default=DEFAULT_SOURCE_TASK_ID)
    parser.add_argument("--batch-id")
    parser.add_argument("--model")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    path = args.path.resolve()
    if not path.is_file() or path.suffix.casefold() != ".docx":
        parser.error("diagnostic file must be an existing DOCX")
    try:
        result = asyncio.run(
            diagnose(path, args.source_task_id, args.batch_id, args.model)
        )
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
