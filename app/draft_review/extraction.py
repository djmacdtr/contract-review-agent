from __future__ import annotations

import asyncio
import hashlib
import json
import operator
from collections import Counter
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.adapters.llm.base import ContractLlmClient, LlmResult
from app.adapters.llm.openai_client import LlmClientError
from app.adapters.llm.schemas import DocumentFactExtraction, FactCandidate
from app.core.config import Settings
from app.core.errors import WorkflowError
from app.documents.models import DocumentBlock, ParsedDocument
from app.draft_review.checkpoints import ExtractionCheckpoint, ExtractionCheckpointStore
from app.draft_review.facts import (
    NUMERIC_EXTRACTION_VERSION,
    TEXT_EXTRACTION_VERSION,
    TEXT_FACT_VALUE_TYPES,
    EvidenceValidationError,
    TextExtractionCandidate,
    build_document_overview_payload,
    build_fact_batch_payload,
    build_numeric_candidate_payload,
    build_text_fact_payload,
    expand_document_overview,
    expand_fact_batch,
    expand_numeric_candidate_response,
    expand_text_fact_response,
    filter_text_fact_evidence,
    merge_chunk_extractions,
    normalize_text,
    plan_document_batches,
    plan_numeric_document_batches,
    plan_simplified_document_batches,
    plan_text_candidate_batches,
    plan_text_document_batches,
    rehydrate_fact_evidence,
    rehydrate_numeric_fact_evidence,
    split_numeric_structure_unit,
    split_table_text_unit,
    stable_batch_id,
    stable_unit_id,
    validate_extraction_evidence,
)

DOCUMENT_EXTRACTION_CHECKPOINT_VERSION = "document-extraction-v1"


def numeric_recovery_blocks(
    blocks: list[DocumentBlock], failure_code: str
) -> list[list[DocumentBlock]]:
    """Return the deterministic numeric truncation recovery partition.

    This helper deliberately returns no groups for non-truncation failures;
    those failures retain the ordinary recovery policy.  Every returned
    child contains source structures and is sent with a newly-built payload
    and batch identity by the caller.
    """

    if failure_code != "LLM_OUTPUT_TRUNCATED":
        return []
    if len(blocks) > 3:
        return [blocks[index : index + 3] for index in range(0, len(blocks), 3)]
    if len(blocks) > 1:
        return [[block] for block in blocks]
    if len(blocks) == 1:
        children = split_numeric_structure_unit(blocks[0])
        if len(children) > 1:
            return [[child] for child in children]
    return []


class _MapState(TypedDict, total=False):
    plans: list[dict[str, Any]]
    plan: dict[str, Any]
    outcomes: Annotated[list[dict[str, Any]], operator.add]
    superseded_batch_ids: Annotated[list[str], operator.add]
    recovery_plans: list[dict[str, Any]]
    reduced: dict[str, dict[str, Any]]


def _error_code(error: BaseException) -> str:
    if isinstance(error, LlmClientError):
        if error.failure_code and (
            error.failure_code.startswith("FACT_")
            or error.failure_code.startswith("NUMERIC_")
        ):
            return error.failure_code
        return error.code
    if isinstance(error, EvidenceValidationError):
        return error.code
    return type(error).__name__


def _safe_failure_code(error: BaseException | None) -> str:
    """Return a stable subcode without serializing exception content."""

    current = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, LlmClientError):
            return current.failure_code or current.code
        if isinstance(current, EvidenceValidationError):
            return current.code
        if isinstance(current, WorkflowError):
            details = current.details
            if isinstance(details, dict) and isinstance(details.get("failure_code"), str):
                return str(details["failure_code"])
            if current.__cause__ is not None:
                current = current.__cause__
                continue
            return current.code
        if isinstance(current, TimeoutError):
            return "LLM_TIMEOUT"
        current = current.__cause__
    return type(error).__name__ if error is not None else "DYNAMIC_CHECK_INCOMPLETE"


def _failure_details(outcome: dict[str, Any]) -> dict[str, Any]:
    """Build the only failure payload allowed to cross the workflow boundary."""

    plan = outcome.get("plan") or {}
    file_id = plan.get("document_id")
    return {
        "failure_stage": "FACT_EXTRACTION",
        "chain": plan.get("chain", "unknown"),
        # ``file`` is retained for compatibility with existing structured logs;
        # ``file_id`` is the explicit diagnostic field used by new callers.
        "file": file_id,
        "file_id": file_id,
        "batch_depth": int(plan.get("depth", 0)),
        "unit_count": len(plan.get("unit_ids", [])),
        "batch_id": plan.get("batch_id"),
        "failure_code": outcome.get("failure_code")
        or _safe_failure_code(outcome.get("error")),
    }


def _payload_digest(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    return digest


_TASK_LOCAL_PAYLOAD_KEYS = frozenset(
    {
        "batch_id",
        "block_id",
        "candidate_id",
        "context_id",
        "file_id",
        "parent_batch_id",
        "source_file_id",
        "unit_id",
    }
)

_DISPLAY_ONLY_PAYLOAD_KEYS = frozenset(
    {"page", "page_count", "physical_pages", "structure_id", "bbox", "source", "confidence"}
)


def _checkpoint_payload_value(value: Any) -> Any:
    """Remove task-local identities before hashing an extraction payload."""

    if isinstance(value, dict):
        return {
            key: _checkpoint_payload_value(item)
            for key, item in value.items()
            if key not in _TASK_LOCAL_PAYLOAD_KEYS
            and key not in _DISPLAY_ONLY_PAYLOAD_KEYS
        }
    if isinstance(value, list):
        return [_checkpoint_payload_value(item) for item in value]
    return value


def _checkpoint_payload_digest(payload: dict[str, Any]) -> str:
    return _payload_digest(_checkpoint_payload_value(payload))


def _checkpoint_unit_identity(block: DocumentBlock) -> dict[str, Any]:
    """Return a structure identity that excludes task-local block IDs."""

    location = block.location
    return {
        "order": block.order,
        "type": block.type,
        "paragraph_index": location.paragraph_index,
        "table_index": location.table_index,
        "row": location.row,
        "column": location.column,
        "text": normalize_text(block.normalized_text or block.raw_text),
    }


def _checkpoint_location_identity(location: Any) -> dict[str, Any]:
    values = getattr(location, "model_dump", lambda **_kwargs: dict(location))(
        mode="json", exclude_none=True
    )
    return {
        key: values[key]
        for key in ("paragraph_index", "table_index", "row", "column", "section")
        if key in values
    }


def _checkpoint_structure_identity(block: DocumentBlock) -> dict[str, Any]:
    identity = {
        "order": block.order,
        "type": block.type,
        "location": _checkpoint_location_identity(block.location),
        "text": normalize_text(block.normalized_text or block.raw_text),
    }
    if block.table is not None:
        identity["rows"] = [
            {
                "row": row.row,
                "cells": [
                    {
                        "location": _checkpoint_location_identity(cell.location),
                        "text": normalize_text(cell.normalized_text or cell.raw_text),
                    }
                    for cell in row.cells
                ],
            }
            for row in block.table.rows
        ]
    return identity


def _document_checkpoint_identity(
    document: ParsedDocument,
    text_candidates: list[TextExtractionCandidate] | None,
) -> dict[str, Any]:
    candidate_identity = []
    for candidate in text_candidates or []:
        candidate_identity.append(
            {
                "type": candidate.block.type,
                "location": _checkpoint_location_identity(candidate.block.location),
                "text": normalize_text(candidate.block.normalized_text or candidate.block.raw_text),
                "context": _checkpoint_payload_value(list(candidate.context_units)),
            }
        )
    candidate_identity.sort(
        key=lambda item: (
            json.dumps(item["location"], ensure_ascii=False, sort_keys=True),
            item["text"],
            item["type"],
        )
    )
    return {
        "file_sha256": document.sha256,
        "parser_name": document.parser_name,
        "role": document.role,
        "extraction_versions": [
            "profile-v2",
            "numeric-v2",
            TEXT_EXTRACTION_VERSION,
        ],
        "structure": [_checkpoint_structure_identity(block) for block in document.blocks],
        "text_candidate_scope": candidate_identity,
    }


def _document_checkpoint_identity_values(
    document: ParsedDocument,
    text_candidates: list[TextExtractionCandidate] | None,
) -> tuple[str, str]:
    identity = _document_checkpoint_identity(document, text_candidates)
    digest = _payload_digest(identity)
    return f"document_{digest}", digest


def _checkpoint_batch_id(
    document: ParsedDocument,
    blocks: list[DocumentBlock],
    extraction_version: str,
    variant: str | None = None,
) -> str:
    """Build a task-independent checkpoint identity for one logical shard."""

    return "batch_" + _payload_digest(
        {
            "file_sha256": document.sha256,
            "parser_version": document.parser_name,
            "extraction_version": extraction_version,
            "variant": variant,
            "units": sorted(
                (_checkpoint_unit_identity(block) for block in blocks),
                key=lambda item: (
                    item["paragraph_index"] is None,
                    item["paragraph_index"] or -1,
                    item["table_index"] is None,
                    item["table_index"] or -1,
                    item["row"] is None,
                    item["row"] or -1,
                    item["column"] is None,
                    item["column"] or -1,
                    item["type"],
                ),
            ),
        }
    )


def _with_checkpoint_identity(
    plan: dict[str, Any],
    document: ParsedDocument,
    *,
    variant: str | None = None,
) -> dict[str, Any]:
    """Replace planner IDs with task-independent IDs for independent extraction."""

    identity = _checkpoint_batch_id(
        document,
        plan["blocks"],
        plan["extraction_version"],
        variant=variant,
    )
    plan["batch_id"] = identity
    plan["payload"] = {
        **plan["payload"],
        "batch_id": identity,
        "extraction_version": plan["extraction_version"],
    }
    return plan


def _replace_file_id(value: str, current_file_id: str, source_file_id: str) -> str:
    if value == current_file_id:
        return source_file_id
    if value.startswith(f"{current_file_id}_"):
        return f"{source_file_id}{value[len(current_file_id):]}"
    return value


def _replace_task_ids(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _replace_task_ids(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_task_ids(item, replacements) for item in value]
    if isinstance(value, str):
        result = value
        for current, source in sorted(replacements.items(), key=lambda item: -len(item[0])):
            result = result.replace(current, source)
        return result
    return value


def _source_identity_document(
    document: ParsedDocument,
    blocks: list[DocumentBlock],
    source_file_id: str,
) -> ParsedDocument:
    source_blocks = [
        block.model_copy(
            update={
                "block_id": _replace_file_id(
                    block.block_id, document.file_id, source_file_id
                )
            }
        )
        for block in blocks
    ]
    return document.model_copy(update={"file_id": source_file_id, "blocks": source_blocks})


def _remap_checkpoint_facts(
    value: dict[str, Any],
    current_file_id: str,
) -> dict[str, Any]:
    facts = value.get("facts", [])
    if not isinstance(facts, list):
        return value
    return {
        **value,
        "facts": [
            {
                **item,
                "source_file_id": current_file_id,
            }
            if isinstance(item, dict)
            else item
            for item in facts
        ],
    }


def _remap_profile_file_id(
    extraction: DocumentFactExtraction,
    current_file_id: str,
) -> DocumentFactExtraction:
    return extraction.model_copy(
        update={
            "profile": extraction.profile.model_copy(
                update={"file_id": current_file_id}
            ),
            "facts": [
                fact.model_copy(update={"source_file_id": current_file_id})
                for fact in extraction.facts
            ],
        }
    )


def _validated_document_checkpoint(
    document: ParsedDocument,
    value: dict[str, Any],
    *,
    source_file_id: str | None = None,
) -> DocumentFactExtraction | None:
    """Validate and rebind one complete document checkpoint."""

    try:
        extraction = DocumentFactExtraction.model_validate(value)
        allowed_file_ids = {document.file_id}
        if source_file_id:
            allowed_file_ids.add(source_file_id)
        if extraction.profile.file_id not in allowed_file_ids or any(
            fact.source_file_id not in allowed_file_ids for fact in extraction.facts
        ):
            return None
        extraction = _remap_profile_file_id(extraction, document.file_id)
        _validate_fact_identity_set(extraction.facts)
        validate_extraction_evidence(document, extraction)
    except (EvidenceValidationError, TypeError, ValueError):
        return None
    return extraction


_STRUCTURE_SPLIT_CHARS = frozenset("。！？；!?;\n\r")
_CLAUSE_SPLIT_CHARS = frozenset("，,、")


def _split_text_fragments(text: str, separators: frozenset[str]) -> list[str]:
    fragments: list[str] = []
    start = 0
    for index, char in enumerate(text):
        if char not in separators:
            continue
        fragment = text[start : index + 1].strip()
        if fragment:
            fragments.append(fragment)
        start = index + 1
    tail = text[start:].strip()
    if tail:
        fragments.append(tail)
    return fragments if len(fragments) > 1 else []


def _split_text_structure_unit(block: DocumentBlock) -> list[DocumentBlock]:
    """Split a non-table unit at conservative sentence/clause boundaries."""

    fragments = _split_text_fragments(block.raw_text, _STRUCTURE_SPLIT_CHARS)
    if not fragments:
        fragments = _split_text_fragments(block.raw_text, _CLAUSE_SPLIT_CHARS)
    return [
        block.model_copy(
            update={
                "block_id": f"{block.block_id}_s{index:04d}",
                "raw_text": fragment,
                "normalized_text": normalize_text(fragment),
            }
        )
        for index, fragment in enumerate(fragments)
    ]


def _legacy_checkpoint_payload(
    plan: dict[str, Any],
    document: ParsedDocument,
    source_file_ids_by_file_id: dict[str, str],
) -> tuple[str, dict[str, Any]] | None:
    """Build the pre-stable-identity payload used by existing source rows."""

    source_file_id = source_file_ids_by_file_id.get(document.file_id)
    if not source_file_id:
        return None
    source_blocks_document = _source_identity_document(
        document, plan["blocks"], source_file_id
    )
    legacy_batch_id = stable_batch_id(
        document.sha256,
        source_blocks_document.blocks,
        plan["extraction_version"],
    )
    if plan["chain"] == "numeric":
        payload = build_numeric_candidate_payload(
            source_blocks_document,
            source_blocks_document.blocks,
            batch_id=legacy_batch_id,
        )
    else:
        context_units: list[dict[str, Any]] = []
        seen_context_ids: set[str] = set()
        context_map = plan.get("context_units_by_block_id", {})
        for block in plan["blocks"]:
            for item in context_map.get(block.block_id, []):
                transformed = _replace_task_ids(item, source_file_ids_by_file_id)
                context_id = str(transformed.get("context_id", ""))
                if context_id and context_id not in seen_context_ids:
                    seen_context_ids.add(context_id)
                    context_units.append(transformed)
        payload = build_text_fact_payload(
            source_blocks_document,
            source_blocks_document.blocks,
            batch_id=legacy_batch_id,
            context_units=context_units,
        )
    payload.update(
        {
            "batch_depth": int(plan.get("depth", 0)),
            "parent_batch_id": plan.get("legacy_parent_batch_id"),
            "planned_batch_count": plan.get("planned_batch_count", 0),
            "extraction_version": plan["extraction_version"],
        }
    )
    return legacy_batch_id, payload


def _validate_fact_identity_set(facts: list[FactCandidate]) -> None:
    """Reject conflicting values assigned to one fact identity."""

    values_by_identity: dict[tuple[object, ...], str] = {}
    for fact in facts:
        identity = (
            fact.field_key,
            fact.source_file_id,
            fact.value_type,
            tuple(sorted(fact.location.model_dump(mode="json").items())),
        )
        value = fact.raw_value
        previous = values_by_identity.get(identity)
        if previous is not None and previous != value:
            raise EvidenceValidationError(
                "conflicting duplicate fact identity",
                code="FACT_IDENTITY_CONFLICT",
            )
        values_by_identity[identity] = value


def _validated_checkpoint_facts(
    document: ParsedDocument,
    profile: DocumentFactExtraction,
    checkpoint_value: dict[str, Any],
    chain: str,
) -> list[FactCandidate] | None:
    """Return only checkpoint values that still pass current Reduce guards."""

    try:
        facts = [FactCandidate.model_validate(item) for item in checkpoint_value.get("facts", [])]
        if chain == "text":
            facts = [fact for fact in facts if fact.value_type in TEXT_FACT_VALUE_TYPES]
        facts = (
            rehydrate_numeric_fact_evidence(document, facts)
            if chain == "numeric"
            else rehydrate_fact_evidence(document, facts)
        )
        _validate_fact_identity_set(facts)
        validate_extraction_evidence(
            document,
            DocumentFactExtraction(
                profile=profile.profile,
                facts=facts,
                missing_field_keys=[],
            ),
        )
    except (TypeError, ValueError):
        return None
    return facts


def _child_plan(
    document: ParsedDocument,
    parent: dict[str, Any],
    blocks: list[DocumentBlock],
    settings: Settings,
) -> dict[str, Any]:
    batch_id = stable_batch_id(document.sha256, blocks)
    planned_batch_count = parent.get(
        "planned_batch_count", parent.get("payload", {}).get("planned_batch_count", 0)
    )
    payload = build_fact_batch_payload(
        document,
        blocks,
        batch_id=batch_id,
        max_facts=settings.LLM_EXTRACTION_MAX_FACTS,
        estimated_output_tokens=settings.LLM_EXTRACTION_ESTIMATED_OUTPUT_TOKENS,
    )
    payload.update(
        {
            "batch_depth": int(parent.get("depth", 0)) + 1,
            "parent_batch_id": parent["batch_id"],
            "planned_batch_count": planned_batch_count,
        }
    )
    return {
        "batch_id": batch_id,
        "document_id": document.file_id,
        "blocks": blocks,
        "unit_ids": [stable_unit_id(block) for block in blocks],
        "payload": payload,
        "numeric_candidate_count": len(payload.get("numeric_candidates", [])),
        "estimated_output_tokens": settings.LLM_EXTRACTION_ESTIMATED_OUTPUT_TOKENS,
        "depth": int(parent.get("depth", 0)) + 1,
        "parent_batch_id": parent["batch_id"],
        "planned_batch_count": planned_batch_count,
    }


def _can_split(error: BaseException, plan: dict[str, Any], settings: Settings) -> bool:
    if len(plan["blocks"]) <= 1:
        return False
    if int(plan.get("depth", 0)) >= settings.LLM_EXTRACTION_MAX_SPLIT_DEPTH:
        return False
    return isinstance(error, LlmClientError) and error.code in {
        "LLM_OUTPUT_TRUNCATED",
        "LLM_INVALID_JSON",
    }


async def _extract_profile(
    document: ParsedDocument,
    llm: ContractLlmClient,
    semaphore: asyncio.Semaphore,
) -> tuple[str, DocumentFactExtraction, dict[str, Any]]:
    payload = build_document_overview_payload(document)
    async with semaphore:
        result = await llm.extract_document_profile(payload)
    profile_value = expand_document_overview(payload, result.value)
    return (
        document.file_id,
        profile_value,
        {
            "configured_model": result.configured_model,
            "actual_model": result.actual_model,
            "duration_ms": result.duration_ms,
            "request_attempts": result.request_attempts,
            "structure_retries": result.structure_retries,
            "status": "SUCCEEDED",
        },
    )


async def extract_documents_with_map_reduce(
    *,
    settings: Settings,
    documents: list[ParsedDocument],
    llm: ContractLlmClient,
    checkpoint_store: ExtractionCheckpointStore | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Run one profile call per document and Send-based fact extraction Map–Reduce."""

    profile_semaphore = asyncio.Semaphore(settings.LLM_EXTRACTION_TASK_CONCURRENCY)
    profile_results = await asyncio.gather(
        *(_extract_profile(document, llm, profile_semaphore) for document in documents)
    )
    profiles = {file_id: value for file_id, value, _meta in profile_results}
    profile_meta = {file_id: meta for file_id, _value, meta in profile_results}

    initial_plans: list[dict[str, Any]] = []
    per_document_plan_count: dict[str, int] = {}
    for document in documents:
        if document.role == "TEMPLATE":
            continue
        plans = plan_document_batches(
            document,
            max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
            max_numeric_candidates=settings.LLM_EXTRACTION_MAX_NUMERIC_CANDIDATES,
            max_facts=settings.LLM_EXTRACTION_MAX_FACTS,
            max_output_tokens=settings.LLM_MAX_OUTPUT_TOKENS,
            estimated_output_token_limit=settings.LLM_EXTRACTION_ESTIMATED_OUTPUT_TOKENS,
        )
        per_document_plan_count[document.file_id] = len(plans)
        for plan in plans:
            plan["payload"].update(
                {
                    "batch_depth": 0,
                    "parent_batch_id": None,
                    "planned_batch_count": len(plans),
                }
            )
        initial_plans.extend(plans)

    if not initial_plans:
        return (
            {
                document.file_id: {
                    "value": profiles[document.file_id].model_dump(mode="json"),
                    **profile_meta[document.file_id],
                    "chunk_count": 0,
                    "planned_batch_count": 0,
                    "recovery_count": 0,
                    "split_count": 0,
                    "max_payload_chars": 0,
                    "numeric_candidate_total": 0,
                }
                for document in documents
            },
            profile_meta,
        )

    semaphore = asyncio.Semaphore(settings.LLM_EXTRACTION_TASK_CONCURRENCY)

    async def map_batch(state: _MapState) -> dict[str, Any]:
        plan = state["plan"]
        try:
            payload_digest = _payload_digest(plan["payload"])
            if checkpoint_store is not None:
                checkpoint = await checkpoint_store.load(plan["batch_id"])
                if checkpoint is not None and checkpoint.payload_digest != payload_digest:
                    raise WorkflowError(
                        "DYNAMIC_CHECK_INCOMPLETE",
                        f"事实批次 {plan['batch_id']} 的断点输入已发生变化",
                    )
                if (
                    checkpoint is not None
                    and checkpoint.status == "SUCCEEDED"
                    and checkpoint.value is not None
                ):
                    extraction = DocumentFactExtraction.model_validate(checkpoint.value)
                    return {
                        "outcomes": [
                            {
                                "status": "SUCCEEDED",
                                "batch_id": plan["batch_id"],
                                "document_id": plan["document_id"],
                                "plan": plan,
                                "extraction": extraction.model_dump(mode="json"),
                                "configured_model": None,
                                "actual_model": None,
                                "duration_ms": 0,
                                "request_attempts": 0,
                                "structure_retries": 0,
                                "checkpoint_reused": True,
                            }
                        ]
                    }
            async with semaphore:
                result = await llm.extract_fact_batch(plan["payload"])
            extraction = expand_fact_batch(plan["payload"], result.value)
            if checkpoint_store is not None:
                await checkpoint_store.save(
                    ExtractionCheckpoint(
                        batch_id=plan["batch_id"],
                        payload_digest=payload_digest,
                        status="SUCCEEDED",
                        value=extraction.model_dump(mode="json"),
                    )
                )
            outcome = {
                "status": "SUCCEEDED",
                "batch_id": plan["batch_id"],
                "document_id": plan["document_id"],
                "plan": plan,
                "extraction": extraction.model_dump(mode="json"),
                "configured_model": result.configured_model,
                "actual_model": result.actual_model,
                "duration_ms": result.duration_ms,
                "request_attempts": result.request_attempts,
                "structure_retries": result.structure_retries,
            }
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            outcome = {
                "status": "FAILED",
                "batch_id": plan["batch_id"],
                "document_id": plan["document_id"],
                "plan": plan,
                "error_code": _error_code(exc),
                "failure_code": getattr(exc, "failure_code", None),
                "error": exc,
                "request_attempts": getattr(exc, "request_attempts", 1) or 1,
                "structure_retries": getattr(exc, "structure_retries", 0),
            }
        return {"outcomes": [outcome]}

    def initial_route(state: _MapState) -> list[Send]:
        return [Send("map_batch", {"plan": plan}) for plan in state.get("plans", [])]

    def recovery_route(state: _MapState) -> list[Send] | str:
        recovery_plans = state.get("recovery_plans", [])
        if not recovery_plans:
            return END
        return [Send("map_batch", {"plan": plan}) for plan in recovery_plans]

    async def reduce_batches(state: _MapState) -> dict[str, Any]:
        outcomes = state.get("outcomes", [])
        batch_ids = [outcome["batch_id"] for outcome in outcomes]
        if len(batch_ids) != len(set(batch_ids)):
            raise WorkflowError(
                "DYNAMIC_CHECK_INCOMPLETE",
                "事实抽取 Reduce 收到重复 batch_id",
            )
        superseded = set(state.get("superseded_batch_ids", []))
        active_failures = [
            outcome
            for outcome in outcomes
            if outcome["batch_id"] not in superseded and outcome["status"] == "FAILED"
        ]
        if active_failures:
            recovery_plans: list[dict[str, Any]] = []
            for failure in active_failures:
                plan = failure["plan"]
                error = failure.get("error")
                if not _can_split(error, plan, settings):
                    raise WorkflowError(
                        "DYNAMIC_CHECK_INCOMPLETE",
                        f"文件 {plan['document_id']} 的事实抽取分片未能可靠完成",
                    ) from error
                midpoint = len(plan["blocks"]) // 2
                document = next(
                    document
                    for document in documents
                    if document.file_id == plan["document_id"]
                )
                recovery_plans.extend(
                    [
                        _child_plan(document, plan, plan["blocks"][:midpoint], settings),
                        _child_plan(document, plan, plan["blocks"][midpoint:], settings),
                    ]
                )
            recovery_budget: dict[str, int] = {}
            for file_id, count in per_document_plan_count.items():
                recovery_budget[file_id] = max(2, (count * 30 + 99) // 100)
            existing_recovery = {
                document_id: sum(
                    1
                    for outcome in outcomes
                    if outcome["document_id"] == document_id
                    and outcome["plan"].get("parent_batch_id")
                )
                for document_id in per_document_plan_count
            }
            for plan in recovery_plans:
                document_id = plan["document_id"]
                existing_recovery[document_id] += 1
                if existing_recovery[document_id] > recovery_budget[document_id]:
                    raise WorkflowError(
                        "DYNAMIC_CHECK_INCOMPLETE",
                        f"文件 {document_id} 的事实抽取恢复预算已用尽",
                    )
                total_for_document = sum(
                    1 for outcome in outcomes if outcome["document_id"] == document_id
                ) + sum(1 for item in recovery_plans if item["document_id"] == document_id)
                if total_for_document > settings.LLM_EXTRACTION_ABSOLUTE_MAX_REQUESTS_PER_DOCUMENT:
                    raise WorkflowError(
                        "DYNAMIC_CHECK_INCOMPLETE",
                        f"文件 {document_id} 的事实抽取达到绝对安全上限",
                    )
            return {
                "recovery_plans": recovery_plans,
                "superseded_batch_ids": [failure["batch_id"] for failure in active_failures],
            }

        active_successes = [
            outcome
            for outcome in outcomes
            if outcome["batch_id"] not in superseded and outcome["status"] == "SUCCEEDED"
        ]
        by_document: dict[str, list[dict[str, Any]]] = {}
        for outcome in active_successes:
            by_document.setdefault(outcome["document_id"], []).append(outcome)
        reduced: dict[str, dict[str, Any]] = {}
        for document in documents:
            if document.role == "TEMPLATE":
                continue
            expected = {
                stable_unit_id(block)
                for plan in initial_plans
                if plan["document_id"] == document.file_id
                for block in plan["blocks"]
            }
            actual: set[str] = set()
            parts: list[DocumentFactExtraction] = []
            for outcome in by_document.get(document.file_id, []):
                unit_ids = set(outcome["plan"]["unit_ids"])
                if actual.intersection(unit_ids):
                    raise WorkflowError(
                        "DYNAMIC_CHECK_INCOMPLETE",
                        f"文件 {document.file_id} 的事实分片存在结构单元冲突",
                    )
                actual.update(unit_ids)
                parts.append(DocumentFactExtraction.model_validate(outcome["extraction"]))
            if actual != expected:
                raise WorkflowError(
                    "DYNAMIC_CHECK_INCOMPLETE",
                    f"文件 {document.file_id} 的事实分片覆盖率不足",
                )
            merged = merge_chunk_extractions(document, parts)
            merged = merged.model_copy(update={"profile": profiles[document.file_id].profile})
            document_outcomes = by_document.get(document.file_id, [])
            reduced[document.file_id] = {
                "value": merged.model_dump(mode="json"),
                "configured_model": next(
                    (
                        item.get("configured_model")
                        for item in document_outcomes
                        if item.get("configured_model")
                    ),
                    None,
                ),
                "actual_model": next(
                    (
                        item.get("actual_model")
                        for item in document_outcomes
                        if item.get("actual_model")
                    ),
                    None,
                ),
                "duration_ms": sum(item.get("duration_ms", 0) for item in document_outcomes)
                + profile_meta[document.file_id].get("duration_ms", 0),
                "request_attempts": sum(
                    item.get("request_attempts", 0) for item in document_outcomes
                )
                + profile_meta[document.file_id].get("request_attempts", 0),
                "structure_retries": sum(
                    item.get("structure_retries", 0) for item in document_outcomes
                )
                + profile_meta[document.file_id].get("structure_retries", 0),
                "chunk_count": len(document_outcomes),
                "planned_batch_count": per_document_plan_count[document.file_id],
                "recovery_count": sum(
                    bool(item["plan"].get("parent_batch_id")) for item in document_outcomes
                ),
                "split_count": len(
                    {
                        item["batch_id"]
                        for item in outcomes
                        if item["document_id"] == document.file_id
                        and item["batch_id"] in superseded
                    }
                ),
                "max_payload_chars": max(
                    len(
                        json.dumps(
                            item["plan"]["payload"],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                    for item in document_outcomes
                ),
                "numeric_candidate_total": sum(
                    len(item["plan"]["payload"].get("numeric_candidates", []))
                    for item in document_outcomes
                ),
            }
        return {"reduced": reduced, "recovery_plans": []}

    graph = StateGraph(_MapState)
    graph.add_node("map_batch", map_batch)
    graph.add_node("reduce_batches", reduce_batches)
    graph.add_conditional_edges(START, initial_route)
    graph.add_edge("map_batch", "reduce_batches")
    graph.add_conditional_edges("reduce_batches", recovery_route)
    compiled = graph.compile()
    result = await compiled.ainvoke({"plans": initial_plans, "outcomes": []})
    reduced = result.get("reduced", {})
    for document in documents:
        if document.role == "TEMPLATE":
            reduced[document.file_id] = {
                "value": profiles[document.file_id].model_dump(mode="json"),
                **profile_meta[document.file_id],
                "chunk_count": 0,
                "planned_batch_count": 0,
                "recovery_count": 0,
                "split_count": 0,
                "max_payload_chars": 0,
                "numeric_candidate_total": 0,
            }
    return reduced, profile_meta


def _simplified_child_plan(
    document: ParsedDocument,
    parent: dict[str, Any],
    blocks: list[DocumentBlock],
    settings: Settings,
) -> dict[str, Any]:
    from app.draft_review.facts import (
        EXTRACTION_VERSION,
        build_numeric_candidate_payload,
        build_text_fact_payload,
        estimate_simplified_output_tokens,
    )

    batch_id = stable_batch_id(document.sha256, blocks, EXTRACTION_VERSION)
    numeric_payload = build_numeric_candidate_payload(document, blocks, batch_id=batch_id)
    text_payload = build_text_fact_payload(document, blocks, batch_id=batch_id)
    planned_count = parent.get("planned_batch_count", 0)
    for payload in (numeric_payload, text_payload):
        payload.update(
            {
                "planned_batch_count": planned_count,
                "batch_depth": int(parent.get("depth", 0)) + 1,
                "parent_batch_id": parent["batch_id"],
            }
        )
    return {
        "batch_id": batch_id,
        "document_id": document.file_id,
        "file_sha256": document.sha256,
        "blocks": blocks,
        "unit_ids": [stable_unit_id(block) for block in blocks],
        "numeric_payload": numeric_payload,
        "text_payload": text_payload,
        "payload": numeric_payload,
        "numeric_candidate_count": len(numeric_payload["numeric_candidates"]),
        "estimated_output_tokens": estimate_simplified_output_tokens(
            numeric_candidate_count=len(numeric_payload["numeric_candidates"]),
            max_text_facts=min(settings.LLM_EXTRACTION_MAX_TEXT_FACTS, len(blocks)),
        ),
        "depth": int(parent.get("depth", 0)) + 1,
        "parent_batch_id": parent["batch_id"],
        "extraction_version": EXTRACTION_VERSION,
    }


def _simplified_can_split(error_code: str, plan: dict[str, Any], settings: Settings) -> bool:
    return (
        len(plan["blocks"]) > 1
        and int(plan.get("depth", 0)) < settings.LLM_EXTRACTION_MAX_SPLIT_DEPTH
        and error_code
        in {
            "LLM_OUTPUT_TRUNCATED",
            "LLM_INVALID_JSON",
            "LLM_SCHEMA_INVALID",
            "LLM_EXTRACTION_EVIDENCE_INVALID",
            "FACT_BATCH_SATURATED",
            "FACT_QUOTE_NOT_GROUNDED",
            "FACT_UNIT_NOT_FOUND",
            "LLM_TIMEOUT",
            "LLM_UPSTREAM_ERROR",
        }
    )


async def extract_documents_with_wave_map_reduce(
    *,
    settings: Settings,
    documents: list[ParsedDocument],
    llm: ContractLlmClient,
    checkpoint_store: ExtractionCheckpointStore | None = None,
    task_id: str | None = None,
    source_task_id: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Wave-controlled LangGraph Map–Reduce for the split extraction protocol.

    A wave is a bounded set of independent ``Send`` tasks.  Successful paired
    results are reduced and checkpointed before the controller schedules the
    next wave.  This deliberately keeps the old coupled extractor available to
    compatibility fixtures while making the production client use this path.
    """

    profile_semaphore = asyncio.Semaphore(settings.LLM_EXTRACTION_TASK_CONCURRENCY)
    profile_results = await asyncio.gather(
        *(_extract_profile(document, llm, profile_semaphore) for document in documents)
    )
    profiles = {file_id: value for file_id, value, _meta in profile_results}
    profile_meta = {file_id: meta for file_id, _value, meta in profile_results}

    initial_plans: list[dict[str, Any]] = []
    per_document_plan_count: dict[str, int] = {}
    documents_by_id = {document.file_id: document for document in documents}
    for document in documents:
        if document.role == "TEMPLATE":
            continue
        plans = plan_simplified_document_batches(
            document,
            max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
            max_numeric_candidates=settings.LLM_EXTRACTION_MAX_NUMERIC_CANDIDATES
            if settings.LLM_EXTRACTION_MAX_NUMERIC_CANDIDATES <= 24
            else 24,
            estimated_output_token_limit=min(
                settings.LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS, 2000
            ),
        )
        per_document_plan_count[document.file_id] = len(plans)
        for plan in plans:
            for payload in (plan["numeric_payload"], plan["text_payload"]):
                payload.update(
                    {
                        "planned_batch_count": len(plans),
                        "batch_depth": 0,
                        "parent_batch_id": None,
                    }
                )
        initial_plans.extend(plans)

    if not initial_plans:
        return (
            {
                document.file_id: {
                    "value": profiles[document.file_id].model_dump(mode="json"),
                    **profile_meta[document.file_id],
                    "chunk_count": 0,
                    "planned_batch_count": 0,
                    "recovery_count": 0,
                    "wave_count": 0,
                }
                for document in documents
            },
            profile_meta,
        )

    recovery_budget = {
        file_id: max(2, (count * 30 + 99) // 100)
        for file_id, count in per_document_plan_count.items()
    }
    planned_calls = len(initial_plans) * 2
    target_cap = settings.LLM_EXTRACTION_MAX_LOGICAL_CALLS_TARGET
    total_cap = settings.LLM_EXTRACTION_MAX_LOGICAL_CALLS_TOTAL
    if any(
        document.role == "TARGET" and planned_calls > target_cap
        for document in documents
    ) or planned_calls > total_cap:
        raise WorkflowError(
            "DYNAMIC_CHECK_INCOMPLETE",
            "事实抽取初始计划调用预算已耗尽",
            details={"failure_code": "EXTRACTION_CALL_BUDGET_EXHAUSTED"},
        )

    semaphore = asyncio.Semaphore(settings.LLM_EXTRACTION_TASK_CONCURRENCY)
    outcomes_by_batch: dict[str, dict[str, Any]] = {}
    superseded: set[str] = set()
    recovery_counts: Counter[str] = Counter()
    logical_calls = 0
    wave_count = 0

    format_evidence_streak = 0

    async def invoke(method_name: str, payload: dict[str, Any]) -> LlmResult:
        method = getattr(llm, method_name)
        async with semaphore:
            try:
                return await method(payload)
            except TypeError as exc:
                # Small offline adapters may not expose keyword options; this
                # fallback is only for the compatibility protocol.
                if "allow_structure_correction" not in str(exc):
                    raise
                return await method(payload)

    async def map_pair(state: dict[str, Any]) -> dict[str, Any]:
        plan = state["plan"]
        digest = _payload_digest(
            {"numeric": plan["numeric_payload"], "text": plan["text_payload"]}
        )
        try:
            if checkpoint_store is not None:
                checkpoint = await checkpoint_store.load(
                    plan["batch_id"],
                    task_id=task_id,
                    file_sha256=plan["file_sha256"],
                    extraction_version=plan["extraction_version"],
                    payload_digest=digest,
                    source_task_id=source_task_id,
                )
                if checkpoint is not None and checkpoint.value is not None:
                    return {
                        "outcomes": [{
                            "status": "SUCCEEDED",
                            "batch_id": plan["batch_id"],
                            "document_id": plan["document_id"],
                            "plan": plan,
                            "extraction": checkpoint.value,
                            "checkpoint_reused": True,
                            "request_attempts": 0,
                            "configured_model": checkpoint.model_name,
                            "actual_model": None,
                            "duration_ms": 0,
                            "structure_retries": 0,
                        }]
                    }
            numeric_result, text_result = await asyncio.gather(
                invoke("extract_numeric_candidates", plan["numeric_payload"]),
                invoke("extract_text_facts", plan["text_payload"]),
            )
            numeric_facts, _ = expand_numeric_candidate_response(
                plan["numeric_payload"], numeric_result.value
            )
            text_facts = expand_text_fact_response(plan["text_payload"], text_result.value)
            extraction = DocumentFactExtraction(
                profile=profiles[plan["document_id"]].profile,
                facts=[*numeric_facts, *text_facts],
            )
            outcome = {
                "status": "SUCCEEDED",
                "batch_id": plan["batch_id"],
                "document_id": plan["document_id"],
                "plan": plan,
                "extraction": extraction.model_dump(mode="json"),
                "checkpoint_reused": False,
                "request_attempts": numeric_result.request_attempts + text_result.request_attempts,
                "configured_model": numeric_result.configured_model,
                "actual_model": numeric_result.actual_model,
                "duration_ms": numeric_result.duration_ms + text_result.duration_ms,
                "structure_retries": (
                    numeric_result.structure_retries + text_result.structure_retries
                ),
            }
            if checkpoint_store is not None:
                await checkpoint_store.save(
                    ExtractionCheckpoint(
                        task_id=task_id,
                        file_sha256=plan["file_sha256"],
                        extraction_version=plan["extraction_version"],
                        batch_id=plan["batch_id"],
                        payload_digest=digest,
                        value=outcome["extraction"],
                        status="SUCCEEDED",
                        model_name=outcome["configured_model"],
                    )
                )
            return {"outcomes": [outcome]}
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return {
                "outcomes": [{
                    "status": "FAILED",
                    "batch_id": plan["batch_id"],
                    "document_id": plan["document_id"],
                    "plan": plan,
                    "error_code": _error_code(exc),
                    "request_attempts": getattr(exc, "request_attempts", 1) or 1,
                    "structure_retries": getattr(exc, "structure_retries", 0),
                }]
            }

    async def run_wave(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
        class WaveState(TypedDict, total=False):
            plans: list[dict[str, Any]]
            plan: dict[str, Any]
            outcomes: Annotated[list[dict[str, Any]], operator.add]

        def route(state: dict[str, Any]) -> list[Send]:
            return [Send("map_pair", {"plan": plan}) for plan in state.get("plans", [])]

        async def reduce_wave(_state: dict[str, Any]) -> dict[str, Any]:
            return {}

        graph = StateGraph(WaveState)
        graph.add_node("map_pair", map_pair)
        graph.add_node("reduce_wave", reduce_wave)
        graph.add_conditional_edges(START, route)
        graph.add_edge("map_pair", "reduce_wave")
        graph.add_edge("reduce_wave", END)
        result = await graph.compile().ainvoke({"plans": plans, "outcomes": []})
        return list(result.get("outcomes", []))

    pending = list(initial_plans)
    first_wave_outcomes: list[dict[str, Any]] = []
    while pending:
        wave_count += 1
        wave = pending[: settings.LLM_EXTRACTION_WAVE_SIZE]
        pending = pending[settings.LLM_EXTRACTION_WAVE_SIZE :]
        if logical_calls + len(wave) * 2 > total_cap:
            raise WorkflowError(
                "DYNAMIC_CHECK_INCOMPLETE",
                "事实抽取调用预算已耗尽",
                details={"failure_code": "EXTRACTION_CALL_BUDGET_EXHAUSTED"},
            )
        wave_outcomes = await run_wave(wave)
        logical_calls += sum(
            0 if outcome.get("checkpoint_reused") else 2 for outcome in wave_outcomes
        )
        for outcome in wave_outcomes:
            batch_id = outcome["batch_id"]
            if batch_id in outcomes_by_batch:
                raise WorkflowError("DYNAMIC_CHECK_INCOMPLETE", "Reduce 收到重复 batch_id")
            outcomes_by_batch[batch_id] = outcome
        if wave_count == 1:
            first_wave_outcomes = wave_outcomes
            success_count = sum(item["status"] == "SUCCEEDED" for item in wave_outcomes)
            if wave_outcomes and success_count / len(wave_outcomes) < 0.9:
                raise WorkflowError("DYNAMIC_CHECK_INCOMPLETE", "首波事实抽取成功率低于 90%")

        failed = [item for item in wave_outcomes if item["status"] == "FAILED"]
        for outcome in wave_outcomes:
            if outcome["status"] == "SUCCEEDED":
                format_evidence_streak = 0
                continue
            code = outcome["error_code"]
            if code in {
                "LLM_INVALID_JSON",
                "LLM_SCHEMA_INVALID",
                "LLM_EXTRACTION_EVIDENCE_INVALID",
            }:
                format_evidence_streak += 1
            else:
                format_evidence_streak = 0
            if format_evidence_streak >= 2:
                raise WorkflowError("DYNAMIC_CHECK_INCOMPLETE", "连续两次格式或证据失败，已熔断")
        children: list[dict[str, Any]] = []
        for failure in failed:
            code = failure["error_code"]
            plan = failure["plan"]
            if not _simplified_can_split(code, plan, settings):
                raise WorkflowError("DYNAMIC_CHECK_INCOMPLETE", "最小事实分片仍未可靠完成")
            midpoint = len(plan["blocks"]) // 2
            if midpoint <= 0:
                raise WorkflowError("DYNAMIC_CHECK_INCOMPLETE", "事实分片无法继续二分")
            document = documents_by_id[plan["document_id"]]
            parts = [
                _simplified_child_plan(document, plan, plan["blocks"][:midpoint], settings),
                _simplified_child_plan(document, plan, plan["blocks"][midpoint:], settings),
            ]
            for child in parts:
                recovery_counts[child["document_id"]] += 1
                if recovery_counts[child["document_id"]] > recovery_budget[child["document_id"]]:
                    raise WorkflowError("DYNAMIC_CHECK_INCOMPLETE", "事实抽取恢复预算已用尽")
            superseded.add(plan["batch_id"])
            children.extend(parts)
        pending.extend(children)
        if logical_calls + len(pending) * 2 > total_cap:
            raise WorkflowError(
                "DYNAMIC_CHECK_INCOMPLETE",
                "事实抽取恢复调用预算已耗尽",
                details={"failure_code": "EXTRACTION_CALL_BUDGET_EXHAUSTED"},
            )

    active = [
        item for batch_id, item in outcomes_by_batch.items()
        if batch_id not in superseded and item["status"] == "SUCCEEDED"
    ]
    by_document: dict[str, list[dict[str, Any]]] = {}
    for outcome in active:
        by_document.setdefault(outcome["document_id"], []).append(outcome)
    reduced: dict[str, dict[str, Any]] = {}
    for document in documents:
        if document.role == "TEMPLATE":
            reduced[document.file_id] = {
                "value": profiles[document.file_id].model_dump(mode="json"),
                **profile_meta[document.file_id],
                "chunk_count": 0,
                "planned_batch_count": 0,
                "recovery_count": 0,
                "wave_count": wave_count,
            }
            continue
        expected = {
            unit_id
            for plan in initial_plans
            if plan["document_id"] == document.file_id
            for unit_id in plan["unit_ids"]
        }
        actual: set[str] = set()
        parts: list[DocumentFactExtraction] = []
        for outcome in by_document.get(document.file_id, []):
            unit_ids = set(outcome["plan"]["unit_ids"])
            if actual.intersection(unit_ids):
                raise WorkflowError("DYNAMIC_CHECK_INCOMPLETE", "Reduce 检测到结构单元身份冲突")
            actual.update(unit_ids)
            parts.append(DocumentFactExtraction.model_validate(outcome["extraction"]))
        if actual != expected:
            raise WorkflowError("DYNAMIC_CHECK_INCOMPLETE", "结构单元覆盖率不是 100%")
        merged = merge_chunk_extractions(document, parts)
        merged = merged.model_copy(update={"profile": profiles[document.file_id].profile})
        reduced[document.file_id] = {
            "value": merged.model_dump(mode="json"),
            **profile_meta[document.file_id],
            "chunk_count": len(parts),
            "planned_batch_count": per_document_plan_count[document.file_id],
            "recovery_count": recovery_counts[document.file_id],
            "wave_count": wave_count,
            "logical_calls": logical_calls,
            "first_wave_success_rate": (
                sum(item["status"] == "SUCCEEDED" for item in first_wave_outcomes)
                / len(first_wave_outcomes)
                if first_wave_outcomes
                else 1.0
            ),
        }
    return reduced, profile_meta


async def extract_documents_with_independent_map_reduce(
    *,
    settings: Settings,
    documents: list[ParsedDocument],
    llm: ContractLlmClient,
    checkpoint_store: ExtractionCheckpointStore | None = None,
    task_id: str | None = None,
    source_task_id: str | None = None,
    source_file_ids_by_file_id: dict[str, str] | None = None,
    text_candidates_by_document: dict[str, list[TextExtractionCandidate]] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Run profile, numeric and text extraction as independent Map–Reduce chains.

    The text chain is intentionally not paired with numeric extraction.  A
    text evidence failure can therefore split and recover its own batch while
    successful numeric checkpoints remain reusable.  Each wave is still a
    LangGraph ``Send`` fan-out, bounded by the controller and reduced before
    the next wave is scheduled.
    """

    documents_by_id = {document.file_id: document for document in documents}
    semaphore = asyncio.Semaphore(settings.LLM_EXTRACTION_TASK_CONCURRENCY)
    logical_calls = 0
    document_logical_calls: Counter[str] = Counter()
    wave_count = 0

    async def materialize_checkpoint(
        checkpoint: ExtractionCheckpoint,
        *,
        plan: dict[str, Any],
        value: dict[str, Any],
        payload_digest: str,
    ) -> None:
        """Copy an ancestor success into the current retry task."""

        if (
            checkpoint_store is not None
            and task_id
            and checkpoint.task_id
            and checkpoint.task_id != task_id
        ):
            existing = await checkpoint_store.load(
                plan["batch_id"],
                task_id=task_id,
                file_sha256=plan["file_sha256"],
                extraction_version=plan["extraction_version"],
                payload_digest=payload_digest,
            )
            if existing is not None:
                # A failed attempt may have left a success under the same
                # primary key but with a different payload revision.  Keep
                # that record intact and continue using the validated source
                # checkpoint for this invocation.
                return
            await checkpoint_store.save(
                ExtractionCheckpoint(
                    task_id=task_id,
                    file_sha256=plan["file_sha256"],
                    extraction_version=plan["extraction_version"],
                    batch_id=plan["batch_id"],
                    payload_digest=payload_digest,
                    value=value,
                    status="SUCCEEDED",
                    model_name=checkpoint.model_name,
                    source_task_id=checkpoint.task_id,
                )
            )

    async def load_document_checkpoint(
        document: ParsedDocument,
    ) -> tuple[DocumentFactExtraction, dict[str, Any]] | None:
        if checkpoint_store is None:
            return None
        text_candidates = (text_candidates_by_document or {}).get(document.file_id)
        batch_id, digest = _document_checkpoint_identity_values(document, text_candidates)
        try:
            checkpoint = await checkpoint_store.load(
                batch_id,
                task_id=task_id,
                file_sha256=document.sha256,
                extraction_version=DOCUMENT_EXTRACTION_CHECKPOINT_VERSION,
                payload_digest=digest,
                source_task_id=source_task_id,
            )
        except asyncio.CancelledError:
            raise
        except BaseException:
            # The established shard path remains responsible for surfacing a
            # checkpoint-store failure with its existing safe diagnostics.
            return None
        if checkpoint is None or checkpoint.value is None:
            return None
        source_file_id = (source_file_ids_by_file_id or {}).get(document.file_id)
        extraction = _validated_document_checkpoint(
            document,
            checkpoint.value,
            source_file_id=source_file_id,
        )
        if extraction is None:
            return None
        await materialize_checkpoint(
            checkpoint,
            plan={
                "batch_id": batch_id,
                "file_sha256": document.sha256,
                "extraction_version": DOCUMENT_EXTRACTION_CHECKPOINT_VERSION,
            },
            payload_digest=digest,
            value=extraction.model_dump(mode="json"),
        )
        return extraction, {
            "status": "SUCCEEDED",
            "checkpoint_reused": True,
            "document_checkpoint_reused": True,
            "configured_model": checkpoint.model_name,
            "actual_model": None,
            "duration_ms": 0,
            "request_attempts": 0,
            "structure_retries": 0,
            "batch_id": batch_id,
            "extraction_version": DOCUMENT_EXTRACTION_CHECKPOINT_VERSION,
        }

    async def save_document_checkpoint(
        document: ParsedDocument,
        extraction: DocumentFactExtraction,
        model_name: str | None,
    ) -> None:
        if checkpoint_store is None or not task_id:
            return
        batch_id, digest = _document_checkpoint_identity_values(
            document,
            (text_candidates_by_document or {}).get(document.file_id),
        )
        await checkpoint_store.save(
            ExtractionCheckpoint(
                task_id=task_id,
                file_sha256=document.sha256,
                extraction_version=DOCUMENT_EXTRACTION_CHECKPOINT_VERSION,
                batch_id=batch_id,
                payload_digest=digest,
                value=extraction.model_dump(mode="json"),
                status="SUCCEEDED",
                model_name=model_name,
                source_task_id=source_task_id,
            )
        )

    async def _profile_once(
        document: ParsedDocument,
    ) -> tuple[str, DocumentFactExtraction, dict[str, Any]]:
        nonlocal logical_calls
        payload = build_document_overview_payload(document)
        batch_id = _checkpoint_batch_id(document, document.blocks, "profile-v2")
        payload["batch_id"] = batch_id
        payload["extraction_version"] = "profile-v2"
        digest = _checkpoint_payload_digest(payload)
        if checkpoint_store is not None:
            checkpoint = await checkpoint_store.load(
                batch_id,
                task_id=task_id,
                file_sha256=document.sha256,
                extraction_version="profile-v2",
                payload_digest=digest,
                source_task_id=source_task_id,
            )
            source_file_id = (source_file_ids_by_file_id or {}).get(document.file_id)
            if checkpoint is None and source_task_id and source_file_id:
                legacy_document = _source_identity_document(
                    document, document.blocks, source_file_id
                )
                legacy_payload = build_document_overview_payload(legacy_document)
                legacy_payload["batch_id"] = stable_batch_id(
                    document.sha256,
                    legacy_document.blocks,
                    "profile-v2",
                )
                legacy_payload["extraction_version"] = "profile-v2"
                checkpoint = await checkpoint_store.load(
                    legacy_payload["batch_id"],
                    task_id=task_id,
                    file_sha256=document.sha256,
                    extraction_version="profile-v2",
                    payload_digest=_payload_digest(legacy_payload),
                    source_task_id=source_task_id,
                )
            if checkpoint is not None and checkpoint.value is not None:
                profile_value = _remap_profile_file_id(
                    DocumentFactExtraction.model_validate(checkpoint.value),
                    document.file_id,
                )
                profile_plan = {
                    "batch_id": batch_id,
                    "file_sha256": document.sha256,
                    "extraction_version": "profile-v2",
                }
                await materialize_checkpoint(
                    checkpoint,
                    plan=profile_plan,
                    payload_digest=digest,
                    value=profile_value.model_dump(mode="json"),
                )
                return document.file_id, profile_value, {
                    "status": "SUCCEEDED",
                    "checkpoint_reused": True,
                    "configured_model": checkpoint.model_name,
                    "actual_model": None,
                    "duration_ms": 0,
                    "request_attempts": 0,
                    "structure_retries": 0,
                    "batch_id": batch_id,
                    "extraction_version": "profile-v2",
                }
        if (
            logical_calls >= settings.LLM_EXTRACTION_MAX_LOGICAL_CALLS_TOTAL
            or document_logical_calls[document.file_id]
            >= settings.LLM_EXTRACTION_ABSOLUTE_MAX_REQUESTS_PER_DOCUMENT
        ):
            raise WorkflowError(
                "DYNAMIC_CHECK_INCOMPLETE",
                "抽取调用预算已耗尽",
                details={"failure_code": "EXTRACTION_CALL_BUDGET_EXHAUSTED"},
            )
        logical_calls += 1
        document_logical_calls[document.file_id] += 1
        async with semaphore:
            result = await llm.extract_document_profile(payload)
        profile_value = expand_document_overview(payload, result.value)
        if checkpoint_store is not None:
            await checkpoint_store.save(
                ExtractionCheckpoint(
                    task_id=task_id,
                    file_sha256=document.sha256,
                    extraction_version="profile-v2",
                    batch_id=batch_id,
                    payload_digest=digest,
                    value=profile_value.model_dump(mode="json"),
                    status="SUCCEEDED",
                    model_name=result.configured_model,
                    source_task_id=source_task_id,
                )
            )
        return document.file_id, profile_value, {
            "status": "SUCCEEDED",
            "checkpoint_reused": False,
            "configured_model": result.configured_model,
            "actual_model": result.actual_model,
            "duration_ms": result.duration_ms,
            "request_attempts": result.request_attempts,
            "structure_retries": result.structure_retries,
            "batch_id": batch_id,
            "extraction_version": "profile-v2",
        }

    async def profile_once(
        document: ParsedDocument,
    ) -> tuple[str, DocumentFactExtraction, dict[str, Any]]:
        try:
            return await _profile_once(document)
        except asyncio.CancelledError:
            raise
        except WorkflowError as exc:
            if exc.details:
                raise
            raise WorkflowError(
                exc.code,
                exc.safe_message,
                retryable=exc.retryable,
                details={
                    "failure_stage": "FACT_EXTRACTION",
                    "chain": "profile",
                    "file": document.file_id,
                    "file_id": document.file_id,
                    "batch_depth": 0,
                    "unit_count": len(document.blocks),
                    "failure_code": _safe_failure_code(exc),
                },
            ) from exc
        except BaseException as exc:
            raise WorkflowError(
                "DYNAMIC_CHECK_INCOMPLETE",
                "文档概览抽取未能可靠完成",
                details={
                    "failure_stage": "FACT_EXTRACTION",
                    "chain": "profile",
                    "file": document.file_id,
                    "file_id": document.file_id,
                    "batch_depth": 0,
                    "unit_count": len(document.blocks),
                    "failure_code": _safe_failure_code(exc),
                },
            ) from exc

    document_checkpoint_results = await asyncio.gather(
        *(load_document_checkpoint(document) for document in documents)
    )
    document_snapshots: dict[str, DocumentFactExtraction] = {}
    document_snapshot_meta: dict[str, dict[str, Any]] = {}
    for document, result in zip(documents, document_checkpoint_results, strict=True):
        if result is not None:
            document_snapshots[document.file_id], document_snapshot_meta[document.file_id] = result

    documents_without_snapshot = [
        document for document in documents if document.file_id not in document_snapshots
    ]
    default_text_fact_limit = min(
        getattr(settings, "LLM_EXTRACTION_MAX_TEXT_FACTS", 12), 12
    )

    def build_initial_plans() -> tuple[
        dict[str, list[dict[str, Any]]], dict[tuple[str, str], int]
    ]:
        initial_by_chain: dict[str, list[dict[str, Any]]] = {"numeric": [], "text": []}
        per_document_chain_count: dict[tuple[str, str], int] = {}
        for document in documents:
            if document.role == "TEMPLATE" or document.file_id in document_snapshots:
                continue
            numeric_plans = plan_numeric_document_batches(
                document,
                max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
                max_numeric_candidates=min(settings.LLM_EXTRACTION_MAX_NUMERIC_CANDIDATES, 24),
                max_numeric_units=settings.LLM_EXTRACTION_MAX_NUMERIC_UNITS,
                estimated_output_token_limit=min(
                    settings.LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS, 2000
                ),
            )
            if text_candidates_by_document is not None and document.role == "TARGET":
                text_plans = plan_text_candidate_batches(
                    document,
                    text_candidates_by_document.get(document.file_id, []),
                    max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
                    max_candidates=min(
                        getattr(settings, "LLM_EXTRACTION_MAX_TEXT_CANDIDATES", 8), 4
                    ),
                    max_text_facts=default_text_fact_limit,
                    estimated_output_token_limit=min(
                        settings.LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS, 2000
                    ),
                )
            else:
                # Auxiliary documents are open-ended and showed saturation and
                # cross-unit quote failures at wider batches in the real runs.
                effective_text_units = min(
                    getattr(settings, "LLM_EXTRACTION_MAX_TEXT_UNITS", 16),
                    1 if document.role != "TARGET" else 16,
                )
                text_plans = plan_text_document_batches(
                    document,
                    max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
                    max_text_units=effective_text_units,
                    max_text_facts=min(
                        getattr(settings, "LLM_EXTRACTION_MAX_TEXT_FACTS", 12), 12
                    ),
                    estimated_output_token_limit=min(
                        settings.LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS, 2000
                    ),
                )
            for chain, plans in (("numeric", numeric_plans), ("text", text_plans)):
                per_document_chain_count[(document.file_id, chain)] = len(plans)
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
                    if chain == "text":
                        plan["text_fact_limit"] = default_text_fact_limit
                    _with_checkpoint_identity(plan, document)
                    source_file_id = (source_file_ids_by_file_id or {}).get(document.file_id)
                    if source_file_id:
                        legacy_document = _source_identity_document(
                            document, plan["blocks"], source_file_id
                        )
                        plan["legacy_batch_id"] = stable_batch_id(
                            document.sha256,
                            legacy_document.blocks,
                            plan["extraction_version"],
                        )
                initial_by_chain[chain].extend(plans)
        return initial_by_chain, per_document_chain_count

    initial_by_chain, per_document_chain_count = build_initial_plans()

    async def strict_checkpoint_hit(plan: dict[str, Any]) -> bool:
        if checkpoint_store is None:
            return False
        checkpoint = await checkpoint_store.load(
            plan["batch_id"],
            task_id=task_id,
            file_sha256=plan["file_sha256"],
            extraction_version=plan["extraction_version"],
            payload_digest=_checkpoint_payload_digest(plan["payload"]),
            source_task_id=source_task_id,
        )
        return checkpoint is not None and checkpoint.status == "SUCCEEDED"

    async def preflight_call_budget() -> None:
        """Count exact cache misses before any profile or fact LLM call."""

        profile_misses: dict[str, int] = {}
        if checkpoint_store is None:
            profile_misses = {
                document.file_id: 1 for document in documents_without_snapshot
            }
        else:
            for document in documents_without_snapshot:
                payload = build_document_overview_payload(document)
                payload.update(
                    {
                        "batch_id": _checkpoint_batch_id(
                            document, document.blocks, "profile-v2"
                        ),
                        "extraction_version": "profile-v2",
                    }
                )
                hit = await checkpoint_store.load(
                    payload["batch_id"],
                    task_id=task_id,
                    file_sha256=document.sha256,
                    extraction_version="profile-v2",
                    payload_digest=_checkpoint_payload_digest(payload),
                    source_task_id=source_task_id,
                )
                profile_misses[document.file_id] = int(hit is None)

        numeric_miss_plans = [
            plan
            for plan in initial_by_chain["numeric"]
            if not await strict_checkpoint_hit(plan)
        ]
        text_miss_plans = [
            plan
            for plan in initial_by_chain["text"]
            if not await strict_checkpoint_hit(plan)
        ]
        miss_by_document: dict[str, dict[str, int]] = {
            document.file_id: {
                "profile": profile_misses.get(document.file_id, 0),
                "numeric": 0,
                "text": 0,
            }
            for document in documents
        }
        for plan in numeric_miss_plans:
            miss_by_document[plan["document_id"]]["numeric"] += 1
        for plan in text_miss_plans:
            miss_by_document[plan["document_id"]]["text"] += 1
        total_misses = sum(sum(item.values()) for item in miss_by_document.values())
        max_document_misses = max(
            (sum(item.values()) for item in miss_by_document.values()),
            default=0,
        )
        if (
            total_misses > settings.LLM_EXTRACTION_MAX_LOGICAL_CALLS_TOTAL
            or max_document_misses
            > settings.LLM_EXTRACTION_ABSOLUTE_MAX_REQUESTS_PER_DOCUMENT
        ):
            details = {
                "failure_stage": "FACT_EXTRACTION",
                "chain": "mixed",
                "file": None,
                "file_id": None,
                "batch_depth": 0,
                "unit_count": 0,
                "batch_id": None,
                "failure_code": "EXTRACTION_CALL_BUDGET_EXHAUSTED",
                "numeric_cache_miss_count": len(numeric_miss_plans),
                "text_cache_miss_count": len(text_miss_plans),
                "profile_cache_miss_count": sum(profile_misses.values()),
                "total_cache_miss_count": total_misses,
                "max_document_cache_miss_count": max_document_misses,
                "total_call_budget": settings.LLM_EXTRACTION_MAX_LOGICAL_CALLS_TOTAL,
                "document_call_budget": (
                    settings.LLM_EXTRACTION_ABSOLUTE_MAX_REQUESTS_PER_DOCUMENT
                ),
            }
            raise WorkflowError(
                "DYNAMIC_CHECK_INCOMPLETE",
                "事实抽取预检发现调用预算不足",
                details=details,
            )

    await preflight_call_budget()

    profile_results = await asyncio.gather(
        *(profile_once(document) for document in documents_without_snapshot)
    )
    profiles = {
        **document_snapshots,
        **{file_id: value for file_id, value, _meta in profile_results},
    }
    profile_meta = {
        **document_snapshot_meta,
        **{file_id: meta for file_id, _value, meta in profile_results},
    }

    def make_child_plan(
        document: ParsedDocument,
        parent: dict[str, Any],
        blocks: list[DocumentBlock],
        *,
        text_fact_limit: int | None = None,
    ) -> dict[str, Any]:
        chain = parent["chain"]
        version = NUMERIC_EXTRACTION_VERSION if chain == "numeric" else TEXT_EXTRACTION_VERSION
        batch_id = stable_batch_id(document.sha256, blocks, version)
        context_map = parent.get("context_units_by_block_id", {})

        def context_for(block: DocumentBlock) -> list[dict[str, Any]]:
            if block.block_id in context_map:
                return context_map[block.block_id]
            for parent_block_id, items in context_map.items():
                if block.block_id.startswith(f"{parent_block_id}_"):
                    return items
            return []

        if chain == "numeric":
            payload = build_numeric_candidate_payload(document, blocks, batch_id=batch_id)
            count = len(payload["numeric_candidates"])
            estimate = min(settings.LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS, 2000)
        else:
            text_fact_limit = text_fact_limit or int(
                parent.get("text_fact_limit", default_text_fact_limit)
            )
            context_units: list[dict[str, Any]] = []
            seen_context_ids: set[str] = set()
            for block in blocks:
                for item in context_for(block):
                    context_id = str(item.get("context_id", ""))
                    if context_id and context_id not in seen_context_ids:
                        seen_context_ids.add(context_id)
                        context_units.append(item)
            payload = build_text_fact_payload(
                document,
                blocks,
                batch_id=batch_id,
                context_units=context_units,
                max_items=text_fact_limit,
            )
            count = 0
            estimate = min(settings.LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS, 2000)
        payload.update(
            {
                "batch_depth": int(parent.get("depth", 0)) + 1,
                "parent_batch_id": parent["batch_id"],
                "planned_batch_count": parent.get("planned_batch_count", 0),
                "extraction_version": version,
            }
        )
        child = {
            "batch_id": batch_id,
            "document_id": document.file_id,
            "file_sha256": document.sha256,
            "blocks": blocks,
            "unit_ids": [stable_unit_id(block) for block in blocks],
            "payload": payload,
            "chain": chain,
            "numeric_candidate_count": count,
            "estimated_output_tokens": estimate,
            "depth": int(parent.get("depth", 0)) + 1,
            "parent_batch_id": parent["batch_id"],
            "planned_batch_count": parent.get("planned_batch_count", 0),
            "extraction_version": version,
            "context_units_by_block_id": {
                block.block_id: context_for(block)
                for block in blocks
                if context_for(block)
            },
        }
        if chain == "text":
            child["text_fact_limit"] = text_fact_limit
        child = _with_checkpoint_identity(
            child,
            document,
            variant=(
                f"text_max_items_{text_fact_limit}"
                if chain == "text"
                else None
            ),
        )
        source_file_id = (source_file_ids_by_file_id or {}).get(document.file_id)
        if source_file_id:
            legacy_document = _source_identity_document(document, blocks, source_file_id)
            child["legacy_batch_id"] = stable_batch_id(
                document.sha256,
                legacy_document.blocks,
                version,
            )
            child["legacy_parent_batch_id"] = parent.get("legacy_batch_id")
        return child

    async def load_checkpoint_outcome(
        plan: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Load and validate one successful checkpoint, if it is reusable."""

        payload = plan["payload"]
        digest = _checkpoint_payload_digest(payload)
        if checkpoint_store is not None:
            checkpoint = await checkpoint_store.load(
                plan["batch_id"],
                task_id=task_id,
                file_sha256=plan["file_sha256"],
                extraction_version=plan["extraction_version"],
                payload_digest=digest,
                source_task_id=source_task_id,
            )
            document = documents_by_id[plan["document_id"]]
            if checkpoint is None and source_task_id and source_file_ids_by_file_id:
                legacy = _legacy_checkpoint_payload(
                    plan,
                    document,
                    source_file_ids_by_file_id,
                )
                if legacy is not None:
                    legacy_batch_id, legacy_payload = legacy
                    checkpoint = await checkpoint_store.load(
                        legacy_batch_id,
                        task_id=task_id,
                        file_sha256=plan["file_sha256"],
                        extraction_version=plan["extraction_version"],
                        payload_digest=_payload_digest(legacy_payload),
                        source_task_id=source_task_id,
                    )
            if checkpoint is not None and checkpoint.value is not None:
                checkpoint_value = _remap_checkpoint_facts(
                    checkpoint.value,
                    document.file_id,
                )
                facts = _validated_checkpoint_facts(
                    document,
                    profiles[plan["document_id"]],
                    checkpoint_value,
                    plan["chain"],
                )
                if facts is not None:
                    discarded_fact_count = int(
                        checkpoint_value.get("discarded_fact_count", 0)
                    )
                    discarded_fact_codes = checkpoint_value.get(
                        "discarded_fact_codes", {}
                    )
                    if not isinstance(discarded_fact_codes, dict):
                        discarded_fact_codes = {}
                    await materialize_checkpoint(
                        checkpoint,
                        plan=plan,
                        payload_digest=digest,
                        value={
                            "facts": [
                                item.model_dump(mode="json") for item in facts
                            ],
                            "discarded_fact_count": discarded_fact_count,
                            "discarded_fact_codes": discarded_fact_codes,
                        },
                    )
                    return {
                        "status": "SUCCEEDED",
                        "batch_id": plan["batch_id"],
                        "document_id": plan["document_id"],
                        "chain": plan["chain"],
                        "plan": plan,
                        "facts": facts,
                        "checkpoint_reused": True,
                        "configured_model": checkpoint.model_name,
                        "actual_model": None,
                        "duration_ms": 0,
                        "request_attempts": 0,
                        "structure_retries": 0,
                        "discarded_fact_count": discarded_fact_count,
                        "discarded_fact_codes": discarded_fact_codes,
                    }
        return None

    async def invoke_plan(plan: dict[str, Any]) -> dict[str, Any]:
        nonlocal logical_calls
        payload = plan["payload"]
        digest = _checkpoint_payload_digest(payload)
        try:
            checkpoint_outcome = await load_checkpoint_outcome(plan)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            # Checkpoint reads and the required current-document evidence
            # validation are part of the map unit.  Keep failures in the same
            # safe outcome shape as an LLM failure so the controller never
            # collapses the bottom code into a generic workflow error.
            return {
                "status": "FAILED",
                "batch_id": plan["batch_id"],
                "document_id": plan["document_id"],
                "chain": plan["chain"],
                "plan": plan,
                "error_code": _error_code(exc),
                "failure_code": _safe_failure_code(exc),
                "error": exc,
                "checkpoint_reused": False,
                "request_attempts": 0,
                "structure_retries": 0,
            }
        if checkpoint_outcome is not None:
            return checkpoint_outcome
        if (
            logical_calls >= settings.LLM_EXTRACTION_MAX_LOGICAL_CALLS_TOTAL
            or document_logical_calls[plan["document_id"]]
            >= settings.LLM_EXTRACTION_ABSOLUTE_MAX_REQUESTS_PER_DOCUMENT
        ):
            return {
                "status": "FAILED",
                "batch_id": plan["batch_id"],
                "document_id": plan["document_id"],
                "chain": plan["chain"],
                "plan": plan,
                "error_code": "EXTRACTION_CALL_BUDGET_EXHAUSTED",
                "failure_code": "EXTRACTION_CALL_BUDGET_EXHAUSTED",
                "checkpoint_reused": False,
            }
        try:
            logical_calls += 1
            document_logical_calls[plan["document_id"]] += 1
            async with semaphore:
                if plan["chain"] == "numeric":
                    result = await llm.extract_numeric_candidates(payload)
                    facts, _classified = expand_numeric_candidate_response(payload, result.value)
                    facts = rehydrate_numeric_fact_evidence(
                        documents_by_id[plan["document_id"]], facts
                    )
                    _validate_fact_identity_set(facts)
                else:
                    try:
                        result = await llm.extract_text_facts(
                            payload,
                            allow_structure_correction=len(plan["blocks"]) == 1,
                        )
                    except TypeError as exc:
                        if "allow_structure_correction" not in str(exc):
                            raise
                        result = await llm.extract_text_facts(payload)
                    facts, discarded_fact_codes = filter_text_fact_evidence(
                        documents_by_id[plan["document_id"]],
                        payload,
                        result.value,
                    )
                    _validate_fact_identity_set(facts)
                    discarded_fact_count = sum(discarded_fact_codes.values())
                if plan["chain"] == "numeric":
                    discarded_fact_codes = {}
                    discarded_fact_count = 0
            if checkpoint_store is not None:
                await checkpoint_store.save(
                    ExtractionCheckpoint(
                        task_id=task_id,
                        file_sha256=plan["file_sha256"],
                        extraction_version=plan["extraction_version"],
                        batch_id=plan["batch_id"],
                        payload_digest=digest,
                        value={
                            "facts": [item.model_dump(mode="json") for item in facts],
                            "discarded_fact_count": discarded_fact_count,
                            "discarded_fact_codes": discarded_fact_codes,
                        },
                        status="SUCCEEDED",
                        model_name=result.configured_model,
                        source_task_id=source_task_id,
                    )
                )
            return {
                "status": "SUCCEEDED",
                "batch_id": plan["batch_id"],
                "document_id": plan["document_id"],
                "chain": plan["chain"],
                "plan": plan,
                "facts": facts,
                "checkpoint_reused": False,
                "configured_model": result.configured_model,
                "actual_model": result.actual_model,
                "duration_ms": result.duration_ms,
                "request_attempts": result.request_attempts,
                "structure_retries": result.structure_retries,
                "discarded_fact_count": discarded_fact_count,
                "discarded_fact_codes": discarded_fact_codes,
            }
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            # A transport retry is an HTTP metric, not another logical batch
            # invocation.  The counter was incremented once before dispatch.
            return {
                "status": "FAILED",
                "batch_id": plan["batch_id"],
                "document_id": plan["document_id"],
                "chain": plan["chain"],
                "plan": plan,
                "error_code": _error_code(exc),
                "failure_code": _safe_failure_code(exc),
                "error": exc,
                "checkpoint_reused": False,
                "request_attempts": getattr(exc, "request_attempts", 1) or 1,
                "structure_retries": getattr(exc, "structure_retries", 0),
            }

    async def run_wave(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
        class IndependentWaveState(TypedDict, total=False):
            plans: list[dict[str, Any]]
            plan: dict[str, Any]
            outcomes: Annotated[list[dict[str, Any]], operator.add]

        async def map_task(state: IndependentWaveState) -> dict[str, Any]:
            return {"outcomes": [await invoke_plan(state["plan"])]}

        def route(state: dict[str, Any]) -> list[Send]:
            return [Send("map_task", {"plan": plan}) for plan in state.get("plans", [])]

        graph = StateGraph(IndependentWaveState)
        graph.add_node("map_task", map_task)
        graph.add_edge("map_task", END)
        graph.add_conditional_edges(START, route)
        result = await graph.compile().ainvoke({"plans": plans, "outcomes": []})
        return list(result.get("outcomes", []))

    async def run_chain(chain: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
        nonlocal wave_count
        initial = initial_by_chain[chain]
        if not initial:
            return {}, {"planned": 0, "recovery": 0, "first_wave_success_rate": 1.0}
        recovery_budget = {
            document_id: max(2, (count * 30 + 99) // 100)
            for (document_id, plan_chain), count in per_document_chain_count.items()
            if plan_chain == chain
        }
        pending: list[dict[str, Any]] = []
        all_outcomes: dict[str, dict[str, Any]] = {}
        superseded: set[str] = set()
        recovery_counts: Counter[str] = Counter()
        first_wave: list[dict[str, Any]] = []
        nonrecoverable_streak = 0
        terminal_failure: dict[str, Any] | None = None

        def failure_details(
            plan: dict[str, Any] | None = None,
            failure_code: str = "DYNAMIC_CHECK_INCOMPLETE",
        ) -> dict[str, Any]:
            if terminal_failure is not None:
                return _failure_details(terminal_failure)
            fallback = plan or (initial[0] if initial else {})
            return _failure_details(
                {"plan": fallback, "failure_code": failure_code}
            )

        def recovery_groups(
            plan: dict[str, Any], failure_code: str
        ) -> list[tuple[list[DocumentBlock], int | None]]:
            blocks = plan["blocks"]
            if chain == "numeric":
                numeric_groups = numeric_recovery_blocks(blocks, failure_code)
                if numeric_groups:
                    return [(group, None) for group in numeric_groups]
            if len(blocks) > 1:
                return [([block], None) for block in blocks]
            if chain == "text" and failure_code in {
                "FACT_BATCH_SATURATED",
                "FACT_UNIT_NOT_FOUND",
                "FACT_QUOTE_NOT_GROUNDED",
                "FACT_IDENTITY_DUPLICATED",
                "FACT_IDENTITY_CONFLICT",
                "FACT_VALUE_NOT_GROUNDED",
                "LLM_OUTPUT_TRUNCATED",
            }:
                cell_units = split_table_text_unit(blocks[0])
                if len(cell_units) > 1:
                    return [([unit], None) for unit in cell_units]
            if chain == "text" and failure_code in {
                "LLM_OUTPUT_TRUNCATED",
                "FACT_BATCH_SATURATED",
            }:
                text_blocks = _split_text_structure_unit(blocks[0])
                if len(text_blocks) > 1:
                    return [([unit], None) for unit in text_blocks]
                current_limit = int(
                    plan.get(
                        "text_fact_limit",
                        plan.get("payload", {})
                        .get("requirements", {})
                        .get("max_items", default_text_fact_limit),
                    )
                )
                next_limit = {12: 6, 6: 3}.get(current_limit)
                if next_limit is not None:
                    return [([blocks[0]], next_limit)]
            return []

        # A prior task may have failed a table-row parent after its column
        # children were already completed.  Reuse the complete child set
        # before dispatching the known-failing parent again.  This is
        # especially important for auxiliary tables: the parent row is the
        # planning unit, but the column children are the durable recovery
        # units.  Partial child availability falls back to the normal parent
        # path so coverage is never silently reduced.
        for plan in initial:
            precovery_groups = (
                [[unit] for unit in split_table_text_unit(plan["blocks"][0])]
                if chain == "text" and len(plan["blocks"]) == 1
                else []
            )
            if checkpoint_store is None or len(precovery_groups) <= 1:
                pending.append(plan)
                continue
            document = documents_by_id[plan["document_id"]]
            child_plans = [
                make_child_plan(
                    document,
                    plan,
                    child_blocks,
                    text_fact_limit=plan.get("text_fact_limit"),
                )
                for child_blocks in precovery_groups
            ]
            child_outcomes = [
                await load_checkpoint_outcome(child_plan) for child_plan in child_plans
            ]
            if all(outcome is not None for outcome in child_outcomes):
                for outcome in child_outcomes:
                    assert outcome is not None
                    all_outcomes[outcome["batch_id"]] = outcome
                all_outcomes[plan["batch_id"]] = {
                    "status": "SUPERSEDED",
                    "batch_id": plan["batch_id"],
                    "document_id": plan["document_id"],
                    "chain": plan["chain"],
                    "plan": plan,
                    "facts": [],
                    "checkpoint_reused": False,
                }
                superseded.add(plan["batch_id"])
                recovery_counts[plan["document_id"]] += 1
                continue
            pending.append(plan)

        while pending:
            wave_count += 1
            wave = pending[: settings.LLM_EXTRACTION_WAVE_SIZE]
            pending = pending[settings.LLM_EXTRACTION_WAVE_SIZE :]
            wave_outcomes = await run_wave(wave)
            if not first_wave:
                first_wave = wave_outcomes
            children: list[dict[str, Any]] = []
            for outcome in wave_outcomes:
                batch_id = outcome["batch_id"]
                if batch_id in all_outcomes:
                    raise WorkflowError(
                        "DYNAMIC_CHECK_INCOMPLETE",
                        "Reduce 收到重复 batch_id",
                        details=failure_details(
                            outcome["plan"], "CHECKPOINT_BATCH_ID_DUPLICATE"
                        ),
                    )
                all_outcomes[batch_id] = outcome
                if outcome["status"] == "SUCCEEDED":
                    nonrecoverable_streak = 0
                    continue
                code = outcome.get("error_code", "LLM_EXTRACTION_FAILED")
                failure_code = outcome.get("failure_code") or code
                plan = outcome["plan"]
                child_groups = recovery_groups(plan, failure_code)
                recoverable = (
                    bool(child_groups)
                    and int(plan.get("depth", 0)) < settings.LLM_EXTRACTION_MAX_SPLIT_DEPTH
                    and failure_code
                    in {
                        "FACT_BATCH_SATURATED",
                        "FACT_UNIT_NOT_FOUND",
                        "FACT_QUOTE_NOT_GROUNDED",
                        "FACT_IDENTITY_DUPLICATED",
                        "FACT_IDENTITY_CONFLICT",
                        "FACT_VALUE_NOT_GROUNDED",
                        "LLM_OUTPUT_TRUNCATED",
                        "LLM_INVALID_JSON",
                        "LLM_SCHEMA_INVALID",
                        "LLM_EXTRACTION_EVIDENCE_INVALID",
                        "LLM_TIMEOUT",
                        "LLM_UPSTREAM_ERROR",
                    }
                )
                if recoverable:
                    document = documents_by_id[plan["document_id"]]
                    children = [
                        *children,
                        *[
                            make_child_plan(
                                document,
                                plan,
                                child_blocks,
                                text_fact_limit=text_fact_limit,
                            )
                            for child_blocks, text_fact_limit in child_groups
                        ],
                    ]
                    recovery_counts[plan["document_id"]] += 1
                    if (
                        recovery_counts[plan["document_id"]]
                        > recovery_budget[plan["document_id"]]
                    ):
                        raise WorkflowError(
                            "DYNAMIC_CHECK_INCOMPLETE",
                            "事实抽取恢复预算已用尽",
                            details=_failure_details(outcome),
                        )
                    superseded.add(plan["batch_id"])
                    continue
                if failure_code in {
                    "LLM_INVALID_JSON",
                    "LLM_SCHEMA_INVALID",
                    "FACT_UNIT_NOT_FOUND",
                    "FACT_QUOTE_NOT_GROUNDED",
                    "LLM_EXTRACTION_EVIDENCE_INVALID",
                }:
                    nonrecoverable_streak += 1
                else:
                    nonrecoverable_streak = 0
                if nonrecoverable_streak >= 2:
                    terminal_failure = outcome
                    raise WorkflowError(
                        "DYNAMIC_CHECK_INCOMPLETE",
                        "连续两次不可恢复抽取失败，已熔断",
                        details=_failure_details(outcome),
                    )
                terminal_failure = outcome
                raise WorkflowError(
                    "DYNAMIC_CHECK_INCOMPLETE",
                    "最小事实分片仍未可靠完成",
                    details=_failure_details(outcome),
                ) from outcome.get("error")
            pending.extend(children)
        active = [
            outcome
            for batch_id, outcome in all_outcomes.items()
            if batch_id not in superseded and outcome["status"] == "SUCCEEDED"
        ]
        by_document: dict[str, list[dict[str, Any]]] = {}
        for outcome in active:
            by_document.setdefault(outcome["document_id"], []).append(outcome)
        for (file_id, plan_chain), _plans_count in per_document_chain_count.items():
            if plan_chain != chain:
                continue
            document = documents_by_id[file_id]
            expected: set[str] = {
                unit_id
                for plan in initial
                if plan["document_id"] == document.file_id
                for unit_id in plan["unit_ids"]
            }
            # A recovered table row may be partitioned into column units when
            # merged-cell text makes the row quote ambiguous.  Replace the
            # superseded row identity with its child identities before the
            # final coverage check; ordinary paragraph/row bisection keeps the
            # same unit IDs and therefore needs no special treatment.
            for batch_id in superseded:
                parent = all_outcomes.get(batch_id)
                if parent is None or parent["document_id"] != document.file_id:
                    continue
                expected.difference_update(parent["plan"]["unit_ids"])
                expected.update(
                    unit_id
                    for child in all_outcomes.values()
                    if child["document_id"] == document.file_id
                    and child["plan"].get("parent_batch_id") == batch_id
                    for unit_id in child["plan"]["unit_ids"]
                )
            actual: set[str] = set()
            for outcome in by_document.get(document.file_id, []):
                unit_ids = set(outcome["plan"]["unit_ids"])
                if actual.intersection(unit_ids):
                    raise WorkflowError("DYNAMIC_CHECK_INCOMPLETE", "Reduce 检测到结构单元身份冲突")
                actual.update(unit_ids)
            if actual != expected:
                raise WorkflowError(
                    "DYNAMIC_CHECK_INCOMPLETE",
                    "结构单元覆盖率不是 100%",
                    details=failure_details(
                        plan=next(
                            (
                                item
                                for item in initial
                                if item["document_id"] == document.file_id
                            ),
                            None,
                        ),
                        failure_code="STRUCTURE_UNIT_COVERAGE_INCOMPLETE",
                    ),
                )
        recoverable_codes = {
            "FACT_BATCH_SATURATED",
            "FACT_UNIT_NOT_FOUND",
            "FACT_QUOTE_NOT_GROUNDED",
            "FACT_IDENTITY_DUPLICATED",
            "FACT_IDENTITY_CONFLICT",
            "FACT_VALUE_NOT_GROUNDED",
            "LLM_OUTPUT_TRUNCATED",
            "LLM_INVALID_JSON",
            "LLM_SCHEMA_INVALID",
            "LLM_EXTRACTION_EVIDENCE_INVALID",
            "LLM_TIMEOUT",
            "LLM_UPSTREAM_ERROR",
        }

        def is_recoverable_first_wave(item: dict[str, Any]) -> bool:
            failure_code = item.get("failure_code") or item.get("error_code")
            return (
                item.get("status") == "FAILED"
                and bool(recovery_groups(item["plan"], failure_code))
                and int(item["plan"].get("depth", 0)) < settings.LLM_EXTRACTION_MAX_SPLIT_DEPTH
                and failure_code in recoverable_codes
            )

        denominator = sum(not is_recoverable_first_wave(item) for item in first_wave)
        first_rate = (
            sum(item["status"] == "SUCCEEDED" for item in first_wave) / denominator
            if denominator
            else 1.0
        )
        if first_rate < 0.9:
            raise WorkflowError(
                "DYNAMIC_CHECK_INCOMPLETE",
                "首波不可恢复抽取成功率低于 90%",
                details=failure_details(failure_code="FIRST_WAVE_SUCCESS_RATE_LOW"),
            )
        return by_document, {
            "planned": len(initial),
            "recovery": sum(recovery_counts.values()),
            "first_wave_success_rate": first_rate,
            "wave_count": wave_count,
            "recovery_counts": dict(recovery_counts),
            "discarded_fact_count": sum(
                int(item.get("discarded_fact_count", 0))
                for item in all_outcomes.values()
                if item.get("status") == "SUCCEEDED"
            ),
        }

    numeric_results, numeric_meta = await run_chain("numeric")
    text_results, text_meta = await run_chain("text")
    reduced: dict[str, dict[str, Any]] = {}
    for document in documents:
        if document.file_id in document_snapshots:
            reduced[document.file_id] = {
                "value": document_snapshots[document.file_id].model_dump(mode="json"),
                **document_snapshot_meta[document.file_id],
                "chunk_count": 0,
                "planned_batch_count": 0,
                "numeric_batch_count": 0,
                "text_batch_count": 0,
                "recovery_count": 0,
                "wave_count": 0,
                "logical_calls": 0,
                "first_wave_success_rate": 1.0,
            }
            continue
        if document.role == "TEMPLATE":
            await save_document_checkpoint(
                document,
                profiles[document.file_id],
                profile_meta[document.file_id].get("configured_model"),
            )
            reduced[document.file_id] = {
                "value": profiles[document.file_id].model_dump(mode="json"),
                **profile_meta[document.file_id],
                "chunk_count": 0,
                "planned_batch_count": 0,
                "recovery_count": 0,
                "wave_count": wave_count,
            }
            continue
        parts: list[DocumentFactExtraction] = []
        for result_map in (numeric_results, text_results):
            for outcome in result_map.get(document.file_id, []):
                parts.append(
                    DocumentFactExtraction(
                        profile=profiles[document.file_id].profile,
                        facts=outcome["facts"],
                        missing_field_keys=[],
                    )
                )
        if not parts:
            raise WorkflowError("DYNAMIC_CHECK_INCOMPLETE", "Reduce 未收到有效事实")
        merged = merge_chunk_extractions(document, parts)
        merged = merged.model_copy(update={"profile": profiles[document.file_id].profile})
        validate_extraction_evidence(document, merged)
        _validate_fact_identity_set(merged.facts)
        await save_document_checkpoint(
            document,
            merged,
            profile_meta[document.file_id].get("configured_model"),
        )
        document_planned = numeric_meta["planned"] + text_meta["planned"]
        document_recovery = numeric_meta["recovery_counts"].get(document.file_id, 0) + text_meta[
            "recovery_counts"
        ].get(document.file_id, 0)
        reduced[document.file_id] = {
            "value": merged.model_dump(mode="json"),
            **profile_meta[document.file_id],
            "configured_model": next(
                (
                    item.get("configured_model")
                    for result_map in (numeric_results, text_results)
                    for item in result_map.get(document.file_id, [])
                    if item.get("configured_model")
                ),
                profile_meta[document.file_id].get("configured_model"),
            ),
            "actual_model": profile_meta[document.file_id].get("actual_model"),
            "chunk_count": len(parts),
            "planned_batch_count": document_planned,
            "numeric_batch_count": sum(
                1 for item in numeric_results.get(document.file_id, [])
            ),
            "text_batch_count": sum(1 for item in text_results.get(document.file_id, [])),
            "recovery_count": document_recovery,
            "wave_count": wave_count,
            "logical_calls": logical_calls,
            "discarded_fact_count": numeric_meta.get("discarded_fact_count", 0)
            + text_meta.get("discarded_fact_count", 0),
            "first_wave_success_rate": min(
                numeric_meta["first_wave_success_rate"], text_meta["first_wave_success_rate"]
            ),
        }
    return reduced, profile_meta
