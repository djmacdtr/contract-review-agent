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
    merge_chunk_extractions,
    plan_document_batches,
    plan_numeric_document_batches,
    plan_simplified_document_batches,
    plan_text_candidate_batches,
    plan_text_document_batches,
    rehydrate_fact_evidence,
    rehydrate_numeric_fact_evidence,
    split_table_text_unit,
    stable_batch_id,
    stable_unit_id,
    validate_extraction_evidence,
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
        if error.failure_code and (
            error.failure_code.startswith("FACT_")
            or error.failure_code.startswith("NUMERIC_")
        ):
            return error.failure_code
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


async def extract_documents_with_independent_map_reduce(
    *,
    settings: Settings,
    documents: list[ParsedDocument],
    llm: ContractLlmClient,
    checkpoint_store: ExtractionCheckpointStore | None = None,
    task_id: str | None = None,
    source_task_id: str | None = None,
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

    async def materialize_checkpoint(checkpoint: ExtractionCheckpoint) -> None:
        """Copy an ancestor success into the current retry task."""

        if (
            checkpoint_store is not None
            and task_id
            and checkpoint.task_id
            and checkpoint.task_id != task_id
        ):
            await checkpoint_store.save(
                ExtractionCheckpoint(
                    task_id=task_id,
                    file_sha256=checkpoint.file_sha256,
                    extraction_version=checkpoint.extraction_version,
                    batch_id=checkpoint.batch_id,
                    payload_digest=checkpoint.payload_digest,
                    value=checkpoint.value,
                    status="SUCCEEDED",
                    model_name=checkpoint.model_name,
                    source_task_id=checkpoint.task_id,
                )
            )

    async def profile_once(
        document: ParsedDocument,
    ) -> tuple[str, DocumentFactExtraction, dict[str, Any]]:
        nonlocal logical_calls
        payload = build_document_overview_payload(document)
        batch_id = stable_batch_id(
            document.sha256,
            document.blocks,
            "profile-v2",
        )
        payload["batch_id"] = batch_id
        payload["extraction_version"] = "profile-v2"
        digest = _payload_digest(payload)
        if checkpoint_store is not None:
            checkpoint = await checkpoint_store.load(
                batch_id,
                task_id=task_id,
                file_sha256=document.sha256,
                extraction_version="profile-v2",
                payload_digest=digest,
                source_task_id=source_task_id,
            )
            if checkpoint is not None and checkpoint.value is not None:
                await materialize_checkpoint(checkpoint)
                profile_value = DocumentFactExtraction.model_validate(checkpoint.value)
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
            raise WorkflowError("DYNAMIC_CHECK_INCOMPLETE", "抽取超过全任务逻辑调用上限")
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

    profile_results = await asyncio.gather(
        *(profile_once(document) for document in documents)
    )
    profiles = {file_id: value for file_id, value, _meta in profile_results}
    profile_meta = {file_id: meta for file_id, _value, meta in profile_results}

    def make_child_plan(
        document: ParsedDocument,
        parent: dict[str, Any],
        blocks: list[DocumentBlock],
    ) -> dict[str, Any]:
        chain = parent["chain"]
        version = NUMERIC_EXTRACTION_VERSION if chain == "numeric" else TEXT_EXTRACTION_VERSION
        batch_id = stable_batch_id(document.sha256, blocks, version)
        context_map = parent.get("context_units_by_block_id", {})
        if chain == "numeric":
            payload = build_numeric_candidate_payload(document, blocks, batch_id=batch_id)
            count = len(payload["numeric_candidates"])
            estimate = min(settings.LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS, 2000)
        else:
            context_units: list[dict[str, Any]] = []
            seen_context_ids: set[str] = set()
            for block in blocks:
                for item in context_map.get(block.block_id, []):
                    context_id = str(item.get("context_id", ""))
                    if context_id and context_id not in seen_context_ids:
                        seen_context_ids.add(context_id)
                        context_units.append(item)
            payload = build_text_fact_payload(
                document,
                blocks,
                batch_id=batch_id,
                context_units=context_units,
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
        return {
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
                block.block_id: context_map.get(block.block_id, [])
                for block in blocks
                if context_map.get(block.block_id)
            },
        }

    initial_by_chain: dict[str, list[dict[str, Any]]] = {"numeric": [], "text": []}
    per_document_chain_count: dict[tuple[str, str], int] = {}
    for document in documents:
        if document.role == "TEMPLATE":
            continue
        numeric_plans = plan_numeric_document_batches(
            document,
            max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
            max_numeric_candidates=min(settings.LLM_EXTRACTION_MAX_NUMERIC_CANDIDATES, 24),
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
                estimated_output_token_limit=min(
                    settings.LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS, 2000
                ),
            )
        else:
            # Auxiliary documents are open-ended and showed saturation and
            # cross-unit quote failures at wider batches in the real runs.
            # Keep the public planning ceiling at 16, but use a safer effective
            # wave unit count for the
            # independent text chain; target candidates remain governed by
            # the separate candidate limit.
            effective_text_units = min(
                getattr(settings, "LLM_EXTRACTION_MAX_TEXT_UNITS", 16),
                1 if document.role != "TARGET" else 16,
            )
            text_plans = plan_text_document_batches(
                document,
                max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
                max_text_units=effective_text_units,
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
                initial_by_chain[chain].append(plan)

    async def load_checkpoint_outcome(
        plan: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Load and validate one successful checkpoint, if it is reusable."""

        payload = plan["payload"]
        digest = _payload_digest(payload)
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
                facts = _validated_checkpoint_facts(
                    documents_by_id[plan["document_id"]],
                    profiles[plan["document_id"]],
                    checkpoint.value,
                    plan["chain"],
                )
                if facts is not None:
                    await materialize_checkpoint(checkpoint)
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
                    }
        return None

    async def invoke_plan(plan: dict[str, Any]) -> dict[str, Any]:
        nonlocal logical_calls
        payload = plan["payload"]
        digest = _payload_digest(payload)
        checkpoint_outcome = await load_checkpoint_outcome(plan)
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
                "error_code": "DYNAMIC_CHECK_INCOMPLETE",
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
                    facts = expand_text_fact_response(payload, result.value)
                    facts = rehydrate_fact_evidence(
                        documents_by_id[plan["document_id"]], facts
                    )
                    _validate_fact_identity_set(facts)
            if checkpoint_store is not None:
                await checkpoint_store.save(
                    ExtractionCheckpoint(
                        task_id=task_id,
                        file_sha256=plan["file_sha256"],
                        extraction_version=plan["extraction_version"],
                        batch_id=plan["batch_id"],
                        payload_digest=digest,
                        value={"facts": [item.model_dump(mode="json") for item in facts]},
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
                "failure_code": getattr(exc, "failure_code", None),
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

        def recovery_groups(plan: dict[str, Any], failure_code: str) -> list[list[DocumentBlock]]:
            blocks = plan["blocks"]
            if len(blocks) > 1:
                midpoint = len(blocks) // 2
                return [blocks[:midpoint], blocks[midpoint:]]
            if chain in {"numeric", "text"} and failure_code in {
                "FACT_BATCH_SATURATED",
                "FACT_UNIT_NOT_FOUND",
                "FACT_QUOTE_NOT_GROUNDED",
                "FACT_IDENTITY_DUPLICATED",
                "FACT_IDENTITY_CONFLICT",
                "FACT_VALUE_NOT_GROUNDED",
            }:
                cell_units = split_table_text_unit(blocks[0])
                if len(cell_units) > 1:
                    return [[unit] for unit in cell_units]
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
                make_child_plan(document, plan, child_blocks)
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
                    raise WorkflowError("DYNAMIC_CHECK_INCOMPLETE", "Reduce 收到重复 batch_id")
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
                            make_child_plan(document, plan, child_blocks)
                            for child_blocks in child_groups
                        ],
                    ]
                    recovery_counts[plan["document_id"]] += 1
                    if (
                        recovery_counts[plan["document_id"]]
                        > recovery_budget[plan["document_id"]]
                    ):
                        raise WorkflowError(
                            "DYNAMIC_CHECK_INCOMPLETE", "事实抽取恢复预算已用尽"
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
                    raise WorkflowError(
                        "DYNAMIC_CHECK_INCOMPLETE", "连续两次不可恢复抽取失败，已熔断"
                    )
                raise WorkflowError(
                    "DYNAMIC_CHECK_INCOMPLETE", "最小事实分片仍未可靠完成"
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
                raise WorkflowError("DYNAMIC_CHECK_INCOMPLETE", "结构单元覆盖率不是 100%")
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
            raise WorkflowError("DYNAMIC_CHECK_INCOMPLETE", "首波不可恢复抽取成功率低于 90%")
        return by_document, {
            "planned": len(initial),
            "recovery": sum(recovery_counts.values()),
            "first_wave_success_rate": first_rate,
            "wave_count": wave_count,
            "recovery_counts": dict(recovery_counts),
        }

    numeric_results, numeric_meta = await run_chain("numeric")
    text_results, text_meta = await run_chain("text")
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
            "first_wave_success_rate": min(
                numeric_meta["first_wave_success_rate"], text_meta["first_wave_success_rate"]
            ),
        }
    return reduced, profile_meta
