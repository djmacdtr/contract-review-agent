"""Diagnose one exact text extraction batch without mutating task state.

The command is deliberately narrow: it parses one local DOCX with
``python-docx``, reconstructs one deterministic batch identity, and performs
at most one text-model call.  It never downloads files, calls OCR, runs the
workflow, or retries a task.  Output contains only structural metrics and
stable error codes.
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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.adapters.llm.openai_client import OpenAIContractLlmClient
from app.core.config import Settings
from app.core.errors import WorkflowError
from app.db.models import CheckTask
from app.documents.parsers import DocxParser
from app.draft_review.checkpoints import SqlAlchemyExtractionCheckpointStore
from app.draft_review.extraction import (
    _checkpoint_payload_digest,
    _safe_failure_code,
    _split_text_structure_unit,
    _with_checkpoint_identity,
    text_recovery_blocks,
)
from app.draft_review.facts import (
    TEXT_EXTRACTION_VERSION,
    build_text_fact_payload,
    filter_text_fact_evidence,
    plan_text_document_batches,
    split_table_text_unit,
)
from app.services.downloader import DOCX_MIME, LocalFile

DEFAULT_SOURCE_TASK_ID = "tsk_01M15X5XTYWVJ6MMBY7B47VKNS"
DEFAULT_BATCH_ID = "batch_161ca9fd8a106e60d1fcc815"


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
    keys = {
        "failure_stage",
        "chain",
        "file_id",
        "batch_depth",
        "unit_count",
        "batch_id",
        "failure_code",
        "underlying_failure_code",
    }
    return {key: value[key] for key in keys if key in value}


def safe_response_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "finish_reason",
        "content_chars",
        "code_fence",
        "json_error_position",
    }
    return {key: value[key] for key in allowed if key in value}


def _decorate_initial_plan(
    plan: dict[str, Any],
    *,
    planned_batch_count: int,
) -> dict[str, Any]:
    plan["planned_batch_count"] = planned_batch_count
    plan["payload"].update(
        {
            "batch_depth": 0,
            "parent_batch_id": None,
            "planned_batch_count": planned_batch_count,
            "extraction_version": TEXT_EXTRACTION_VERSION,
        }
    )
    return plan


def _make_child_plan(
    document,
    parent: dict[str, Any],
    blocks: list,
    *,
    text_fact_limit: int,
) -> dict[str, Any]:
    context_units: list[dict[str, Any]] = []
    seen_context_ids: set[str] = set()
    context_map = parent.get("context_units_by_block_id", {})
    for block in blocks:
        items = context_map.get(block.block_id)
        if items is None:
            for parent_block_id, parent_items in context_map.items():
                if block.block_id.startswith(f"{parent_block_id}_"):
                    items = parent_items
                    break
        for item in items or []:
            context_id = str(item.get("context_id", ""))
            if context_id and context_id not in seen_context_ids:
                seen_context_ids.add(context_id)
                context_units.append(item)
    payload = build_text_fact_payload(
        document,
        blocks,
        batch_id="pending",
        context_units=context_units,
        max_items=text_fact_limit,
    )
    depth = int(parent.get("depth", 0)) + 1
    payload.update(
        {
            "batch_depth": depth,
            "parent_batch_id": parent["batch_id"],
            "planned_batch_count": parent.get("planned_batch_count", 0),
            "extraction_version": TEXT_EXTRACTION_VERSION,
        }
    )
    child = {
        "batch_id": "pending",
        "document_id": document.file_id,
        "file_sha256": document.sha256,
        "blocks": blocks,
        "unit_ids": [
            item["unit_id"] for item in payload.get("units", []) if "unit_id" in item
        ],
        "payload": payload,
        "chain": "text",
        "numeric_candidate_count": 0,
        "estimated_output_tokens": 0,
        "depth": depth,
        "parent_batch_id": parent["batch_id"],
        "planned_batch_count": parent.get("planned_batch_count", 0),
        "extraction_version": TEXT_EXTRACTION_VERSION,
        "context_units_by_block_id": {
            block.block_id: context_map.get(block.block_id, [])
            for block in blocks
            if context_map.get(block.block_id)
        },
        "text_fact_limit": text_fact_limit,
    }
    return _with_checkpoint_identity(
        child,
        document,
        variant=f"text_max_items_{text_fact_limit}",
    )


def reconstruct_text_batch(
    document,
    batch_id: str,
    settings: Settings,
) -> dict[str, Any] | None:
    """Rebuild one batch using the production text planner and recovery IDs."""

    initial = plan_text_document_batches(
        document,
        max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
        max_text_units=min(settings.LLM_EXTRACTION_MAX_TEXT_UNITS, 16),
        max_text_facts=min(settings.LLM_EXTRACTION_MAX_TEXT_FACTS, 12),
        estimated_output_token_limit=min(
            settings.LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS, 2000
        ),
    )
    initial = [
        _decorate_initial_plan(plan, planned_batch_count=len(initial))
        for plan in initial
    ]
    for plan in initial:
        _with_checkpoint_identity(plan, document)
    pending = list(initial)
    visited: set[str] = set()
    while pending:
        plan = pending.pop(0)
        if plan["batch_id"] in visited:
            continue
        visited.add(plan["batch_id"])
        if plan["batch_id"] == batch_id:
            return plan
        if int(plan.get("depth", 0)) >= 2:
            continue
        blocks = plan["blocks"]
        if len(blocks) > 1:
            child_groups = text_recovery_blocks(blocks)
        else:
            block = blocks[0]
            if block.table is not None:
                child_groups = [[child] for child in split_table_text_unit(block)]
            else:
                child_groups = [[child] for child in _split_text_structure_unit(block)]
        for child_group in child_groups:
            pending.append(
                _make_child_plan(
                    document,
                    plan,
                    child_group,
                    text_fact_limit=int(plan.get("text_fact_limit", 12)),
                )
            )
    return None


async def _source_task_file(session_factory, source_task_id: str, batch_id: str):
    async with session_factory() as session:
        task = (
            await session.execute(
                select(CheckTask)
                .options(selectinload(CheckTask.files))
                .where(CheckTask.id == source_task_id)
            )
        ).scalar_one_or_none()
    if task is None:
        raise WorkflowError("TEXT_SOURCE_TASK_NOT_FOUND", "来源任务不存在")
    failure = safe_task_details(task.error_details)
    if failure.get("batch_id") != batch_id:
        raise WorkflowError("TEXT_DIAGNOSTIC_BATCH_MISMATCH", "失败批次与诊断批次不一致")
    file_id = str(failure.get("file_id", ""))
    candidates = [
        item
        for item in task.files
        if str(item.role) == "REFERENCE" and (not file_id or item.id == file_id)
    ]
    if len(candidates) != 1:
        raise WorkflowError("TEXT_SOURCE_FILE_NOT_UNIQUE", "来源文本文件不唯一")
    return task, candidates[0], failure


async def diagnose(
    path: Path,
    *,
    source_task_id: str,
    batch_id: str,
    response_format: str,
    model_override: str | None,
) -> dict[str, Any]:
    raw = path.read_bytes()
    settings = Settings()
    settings.DATABASE_URL = host_database_url(settings.DATABASE_URL)
    settings.DOCX_PAGE_LOCATION_ENABLED = False
    settings.OCR_ENABLED = False
    settings.OCR_HTTP_RETRY_ATTEMPTS = 0
    settings.LLM_RESPONSE_FORMAT = "json_schema"
    settings.LLM_NATIVE_STRUCTURED_OUTPUT = True
    settings.LLM_HTTP_RETRY_ATTEMPTS = 0
    settings.LLM_STRUCTURE_RETRY_ATTEMPTS = 0
    engine = create_async_engine(host_database_url(settings.DATABASE_URL), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        _task, source_file, failure = await _source_task_file(
            session_factory, source_task_id, batch_id
        )
        local_sha = hashlib.sha256(raw).hexdigest()
        local_file = LocalFile(
            file_id=source_file.id,
            role="REFERENCE",
            file_name=path.name,
            safe_url="local-diagnostic://redacted",
            path=path,
            file_size=len(raw),
            sha256=local_sha,
            detected_mime_type=DOCX_MIME,
        )
        document = await DocxParser().parse(local_file)
        if source_file.sha256 and source_file.sha256 != local_sha:
            return {
                "status": "SAFE_STOP",
                "source_task_id": source_task_id,
                "file_id": source_file.id,
                "sha256": local_sha,
                "batch_id": batch_id,
                "llm_calls": 0,
                "failure_stage": "TEXT_SOURCE_FILE_VALIDATION",
                "failure_code": "TEXT_SOURCE_SHA_MISMATCH",
            }
        plan = reconstruct_text_batch(document, batch_id, settings)
        if plan is None:
            return {
                "status": "SAFE_STOP",
                "source_task_id": source_task_id,
                "file_id": source_file.id,
                "sha256": local_sha,
                "batch_id": batch_id,
                "llm_calls": 0,
                "failure_stage": "TEXT_BATCH_RECONSTRUCTION",
                "failure_code": "TEXT_BATCH_NOT_RECONSTRUCTED",
            }
        if failure.get("unit_count") not in (None, len(plan["blocks"])):
            return {
                "status": "SAFE_STOP",
                "source_task_id": source_task_id,
                "file_id": source_file.id,
                "sha256": local_sha,
                "batch_id": batch_id,
                "unit_count": len(plan["blocks"]),
                "llm_calls": 0,
                "failure_stage": "TEXT_BATCH_RECONSTRUCTION",
                "failure_code": "TEXT_BATCH_UNIT_COUNT_MISMATCH",
            }
        digest = _checkpoint_payload_digest(plan["payload"])
        store = SqlAlchemyExtractionCheckpointStore(session_factory)
        existing = await store.load(
            batch_id,
            task_id=source_task_id,
            file_sha256=local_sha,
            extraction_version=TEXT_EXTRACTION_VERSION,
            payload_digest=digest,
        )
        if existing is not None:
            return {
                "status": "CHECKPOINT_ALREADY_EXISTS",
                "source_task_id": source_task_id,
                "file_id": source_file.id,
                "sha256": local_sha,
                "batch_id": batch_id,
                "unit_count": len(plan["blocks"]),
                "llm_calls": 0,
                "checkpoint_written": False,
                "failure_stage": None,
                "failure_code": None,
            }
        result = await OpenAIContractLlmClient(
            settings,
            text_response_format_override=response_format,
            text_model_override=model_override,
        ).extract_text_facts(
            plan["payload"], allow_structure_correction=False
        )
        facts, discarded = filter_text_fact_evidence(
            document, plan["payload"], result.value
        )
        return {
            "status": "SUCCEEDED",
            "source_task_id": source_task_id,
            "file_id": source_file.id,
            "sha256": local_sha,
            "batch_id": batch_id,
            "unit_count": len(plan["blocks"]),
            "response_item_count": len(result.value.get("items", [])),
            "accepted_fact_count": len(facts),
            "discarded_fact_count": sum(discarded.values()),
            "discarded_fact_codes": discarded,
            "llm_calls": 1,
            "response_format": result.response_format,
            "configured_model": result.configured_model,
            "actual_model": result.actual_model,
            **safe_response_metadata(result.response_metadata),
            "checkpoint_written": False,
            "failure_stage": None,
            "failure_code": None,
        }
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        return {
            "status": "FAILED",
            "source_task_id": source_task_id,
            "batch_id": batch_id,
            "llm_calls": 0,
            "failure_stage": "TEXT_BATCH_DIAGNOSTIC",
            "failure_code": _safe_failure_code(exc),
            "response_format": response_format,
            "configured_model": model_override or settings.LLM_EXTRACTION_MODEL,
            **safe_response_metadata(
                {
                    "finish_reason": getattr(exc, "finish_reason", None),
                    "content_chars": getattr(exc, "content_chars", None),
                    "code_fence": getattr(exc, "code_fence", None),
                    "json_error_position": getattr(
                        exc, "json_error_position", None
                    ),
                }
            ),
        }
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--source-task-id", default=DEFAULT_SOURCE_TASK_ID)
    parser.add_argument("--batch-id", default=DEFAULT_BATCH_ID)
    parser.add_argument(
        "--response-format",
        choices=("json_object", "json_schema"),
        default="json_object",
    )
    parser.add_argument("--model", dest="model_override")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    path = args.path.resolve()
    if not path.is_file() or path.suffix.casefold() != ".docx":
        parser.error("diagnostic file must be an existing DOCX")
    result = asyncio.run(
        diagnose(
            path,
            source_task_id=args.source_task_id,
            batch_id=args.batch_id,
            response_format=args.response_format,
            model_override=args.model_override,
        )
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result["status"] in {"SUCCEEDED", "CHECKPOINT_ALREADY_EXISTS"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
