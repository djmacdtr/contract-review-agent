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
from app.adapters.llm.schemas import DocumentFactExtraction
from app.core.config import Settings
from app.core.errors import WorkflowError
from app.documents.models import DocumentBlock, ParsedDocument
from app.draft_review.checkpoints import ExtractionCheckpoint, ExtractionCheckpointStore
from app.draft_review.facts import (
    EvidenceValidationError,
    build_document_overview_payload,
    build_fact_batch_payload,
    expand_document_overview,
    expand_fact_batch,
    expand_numeric_candidate_response,
    expand_text_fact_response,
    merge_chunk_extractions,
    plan_document_batches,
    plan_simplified_document_batches,
    stable_batch_id,
    stable_unit_id,
)


class _MapState(TypedDict, total=False):
    plans: list[dict[str, Any]]
    plan: dict[str, Any]
    outcomes: Annotated[list[dict[str, Any]], operator.add]
    superseded_batch_ids: Annotated[list[str], operator.add]
    recovery_plans: list[dict[str, Any]]
    reduced: dict[str, dict[str, Any]]


def _error_code(error: BaseException) -> str:
    if isinstance(error, LlmClientError):
        return error.code
    if isinstance(error, EvidenceValidationError):
        return error.code
    return type(error).__name__


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
        file_id: max(2, (count * 20 + 99) // 100)
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
            "事实抽取初始计划已超过安全逻辑调用预算",
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
            raise WorkflowError("DYNAMIC_CHECK_INCOMPLETE", "事实抽取超过全任务逻辑调用上限")
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
            raise WorkflowError("DYNAMIC_CHECK_INCOMPLETE", "事实抽取恢复后超过逻辑调用上限")

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
