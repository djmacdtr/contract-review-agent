from __future__ import annotations

import asyncio
from typing import Any, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.adapters.document_parser.textin_parser import TextInDocumentParser
from app.adapters.llm.base import ContractLlmClient, LlmResult
from app.adapters.llm.openai_client import (
    LlmClientError,
    OpenAIContractLlmClient,
    _safe_validation_summary,
)
from app.adapters.llm.schemas import (
    AdviceResponse,
    DocumentFactExtraction,
    FactCandidate,
    FactMappingResponse,
    FactMappingReview,
    FactReview,
    SemanticConceptPlan,
    SemanticPlanResponse,
    SemanticValidationSpec,
)
from app.core.config import Settings
from app.core.enums import TaskStage, TaskType
from app.core.errors import WorkflowError
from app.documents.models import DocumentBlock, ParsedDocument, ProcessingWarning
from app.documents.page_locations import apply_docx_page_location_sidecars
from app.documents.parsers import ParserRegistry
from app.documents.router import DocumentParsingRouter
from app.draft_review.checkpoints import ExtractionCheckpoint
from app.draft_review.extraction import (
    extract_documents_with_independent_map_reduce,
    extract_documents_with_map_reduce,
)
from app.draft_review.facts import (
    FACT_REVIEW_CHECKPOINT_VERSION,
    MAX_NUMERIC_CANDIDATES_PER_CHUNK,
    EvidenceValidationError,
    build_fact_index,
    build_fact_matrix,
    build_fact_review_batches,
    build_template_text_candidates,
    chunk_document,
    compact_extraction_payload,
    extraction_payload_chars,
    fact_conflict_diff_items,
    fact_index_payload,
    fact_matrix_result_items,
    location_key,
    mapping_proposal_key,
    merge_chunk_extractions,
    merge_fact_review_batches,
    project_semantic_plan,
    qualified_fact_refs,
    review_payload_digest,
    stable_fact_id,
    stable_review_batch_id,
    target_fact_catalog,
    validate_mapping_review_coverage,
    validate_semantic_plan,
)
from app.draft_review.numeric_rules import evaluate_validation_spec, referenced_fact_refs
from app.draft_review.template_checks import TemplateReviewResult, analyze_template
from app.results.advice import (
    advice_payload,
    ensure_fallback_risk_advices,
    merge_model_advice,
)
from app.results.passed_checks import build_comparison_passed_checks
from app.results.risk_model import build_risk_items, build_statistics
from app.schemas.results import RESULT_SCHEMA_VERSION
from app.services.downloader import DOCX_MIME, LocalFile, SafeFileDownloadService
from app.services.temp_files import TaskWorkspace
from app.workflows.mock_graphs import ProgressCallback
from app.workflows.types import WorkflowOutput

DRAFT_REVIEW_WORKFLOW_VERSION = "0.7.0"
DRAFT_REVIEW_RULES_VERSION = "0.6.0"
logger = structlog.get_logger(__name__)


def _compact_semantic_fact(fact: dict[str, Any]) -> dict[str, Any]:
    """Keep semantic planning inputs bounded without removing identity/value."""

    return {
        key: fact[key]
        for key in (
            "fact_id",
            "source_file_id",
            "field_key",
            "concept_id",
            "display_name",
            "value_type",
            "raw_value",
            "location",
            "confidence",
        )
        if key in fact
    }


def _compact_mapping_fact(fact: dict[str, Any]) -> dict[str, Any]:
    """Keep mapping context to accepted identity/value fields only."""

    return {
        key: fact[key]
        for key in (
            "target_fact_id",
            "source_file_id",
            "field_key",
            "concept_id",
            "display_name",
            "value_type",
            "raw_value",
            "normalized_hint",
            "location",
            "confidence",
        )
        if key in fact
    }


def _build_semantic_plan_payloads(
    payload: dict[str, Any],
    *,
    fact_batch_size: int,
) -> list[dict[str, Any]]:
    """Split semantic planning by target facts while retaining linked sources.

    The target facts are the planning focus.  Reference facts remain available
    in each batch because a mapped target fact may need a cross-document rule.
    Only accepted mappings whose target is in the current focus are repeated.
    This bounds generated plan size without changing the public extraction
    result or allowing a batch to reference an omitted fact.
    """

    primary_file_id = str(payload["file_id"])
    documents = payload.get("documents", [])
    primary_document = next(
        (
            document
            for document in documents
            if isinstance(document, dict) and document.get("file_id") == primary_file_id
        ),
        None,
    )
    if primary_document is None:
        return [payload]
    target_facts = [
        _compact_semantic_fact(fact)
        for fact in primary_document.get("facts", [])
        if isinstance(fact, dict)
    ]
    if not target_facts:
        return [payload]
    if fact_batch_size < 1:
        raise ValueError("semantic fact batch size must be positive")

    accepted_mappings = [
        mapping
        for mapping in payload.get("accepted_mappings", [])
        if isinstance(mapping, dict)
    ]
    batches: list[dict[str, Any]] = []
    for start in range(0, len(target_facts), fact_batch_size):
        target_batch = target_facts[start : start + fact_batch_size]
        target_ids = {
            str(fact.get("fact_id"))
            for fact in target_batch
            if fact.get("fact_id")
        }
        batch_mappings = [
            mapping
            for mapping in accepted_mappings
            if mapping.get("target", {}).get("fact_id") in target_ids
        ]
        linked_reference_ids = {
            mapping.get("reference", {}).get("fact_id")
            for mapping in batch_mappings
            if mapping.get("reference", {}).get("fact_id")
        }
        batch_documents: list[dict[str, Any]] = []
        for document in documents:
            if not isinstance(document, dict):
                continue
            facts = (
                target_batch
                if document.get("file_id") == primary_file_id
                else [
                    _compact_semantic_fact(fact)
                    for fact in document.get("facts", [])
                    if isinstance(fact, dict)
                    and fact.get("fact_id") in linked_reference_ids
                ]
            )
            batch_documents.append(
                {
                    key: value
                    for key, value in {
                        "file_id": document.get("file_id"),
                        "role": document.get("role"),
                        "profile": document.get("profile"),
                        "facts": facts,
                    }.items()
                    if value is not None
                }
            )
        batches.append(
            {
                **payload,
                "documents": batch_documents,
                "accepted_mappings": batch_mappings,
                "semantic_requirements": {
                    **payload.get("semantic_requirements", {}),
                    "planning_scope": {
                        "target_fact_count": len(target_batch),
                        "target_fact_batch_start": start + 1,
                        "target_fact_batch_end": start + len(target_batch),
                        "only_emit_plans_for_target_batch": True,
                    },
                },
            }
        )
    return batches


def _merge_semantic_plan_parts(
    parts: list[SemanticPlanResponse],
    *,
    primary_file_id: str,
) -> SemanticPlanResponse:
    """Reduce split semantic plans with strict identity/conflict checks."""

    if not parts:
        raise EvidenceValidationError("semantic plan returned no parts")
    if any(part.file_id != primary_file_id for part in parts):
        raise EvidenceValidationError("semantic plan file_id does not match primary document")

    concepts: dict[str, SemanticConceptPlan] = {}
    specs: dict[str, SemanticValidationSpec] = {}
    for part in parts:
        for concept in part.semantic_concepts:
            existing = concepts.get(concept.concept_id)
            if existing is None:
                concepts[concept.concept_id] = concept
                continue
            if (
                existing.display_name != concept.display_name
                or existing.value_type != concept.value_type
            ):
                raise EvidenceValidationError(
                    "semantic concept identity conflict",
                    code="FACT_IDENTITY_CONFLICT",
                )
            fact_refs = [item.model_dump(mode="json") for item in existing.fact_refs]
            fact_ref_keys = {
                (item.fact_id, item.source_file_id) for item in existing.fact_refs
            }
            for item in concept.fact_refs:
                if (item.fact_id, item.source_file_id) not in fact_ref_keys:
                    fact_refs.append(item.model_dump(mode="json"))
                    fact_ref_keys.add((item.fact_id, item.source_file_id))
            evidence_refs = [
                item.model_dump(mode="json") for item in existing.evidence_refs
            ]
            evidence_keys = {
                (item.source_file_id, location_key(item.location))
                for item in existing.evidence_refs
            }
            for item in concept.evidence_refs:
                key = (item.source_file_id, location_key(item.location))
                if key not in evidence_keys:
                    evidence_refs.append(item.model_dump(mode="json"))
                    evidence_keys.add(key)
            aliases = list(dict.fromkeys([*existing.aliases, *concept.aliases]))
            concepts[concept.concept_id] = SemanticConceptPlan.model_validate(
                {
                    **existing.model_dump(mode="json"),
                    "aliases": aliases,
                    "fact_refs": fact_refs,
                    "evidence_refs": evidence_refs,
                    "confidence": max(existing.confidence, concept.confidence),
                }
            )
        for spec in part.validation_specs:
            existing = specs.get(spec.validation_id)
            if existing is None:
                specs[spec.validation_id] = spec
                continue
            if existing.expression != spec.expression:
                raise EvidenceValidationError(
                    "semantic validation identity conflict",
                    code="FACT_IDENTITY_CONFLICT",
                )
            evidence_refs = [
                item.model_dump(mode="json") for item in existing.evidence_refs
            ]
            evidence_keys = {
                (item.source_file_id, location_key(item.location))
                for item in existing.evidence_refs
            }
            for item in spec.evidence_refs:
                key = (item.source_file_id, location_key(item.location))
                if key not in evidence_keys:
                    evidence_refs.append(item.model_dump(mode="json"))
                    evidence_keys.add(key)
            specs[spec.validation_id] = SemanticValidationSpec.model_validate(
                {
                    **existing.model_dump(mode="json"),
                    "evidence_refs": evidence_refs,
                    "confidence": max(existing.confidence, spec.confidence),
                }
            )
    return SemanticPlanResponse(
        file_id=primary_file_id,
        semantic_concepts=list(concepts.values()),
        validation_specs=list(specs.values()),
    )


def _dynamic_failure_code(exc: BaseException) -> str:
    if isinstance(exc, EvidenceValidationError):
        return exc.code
    if isinstance(exc, LlmClientError):
        if exc.failure_code:
            return exc.failure_code
        if exc.code == "LLM_INVALID_JSON":
            return "LLM_RESPONSE_JSON_INVALID"
        if exc.code == "LLM_RESPONSE_INVALID":
            return "LLM_RESPONSE_ENVELOPE_INVALID"
        if exc.code == "LLM_SCHEMA_INVALID":
            return "LLM_RESPONSE_SCHEMA_INVALID"
        if exc.code in {
            "LLM_TIMEOUT",
            "LLM_NETWORK_ERROR",
            "LLM_UPSTREAM_ERROR",
            "LLM_RATE_LIMITED",
        }:
            return "LLM_UPSTREAM_FAILED"
        return exc.code
    if isinstance(exc, ValidationError):
        return "LLM_RESPONSE_SCHEMA_INVALID"
    if isinstance(exc, TimeoutError):
        return "LLM_UPSTREAM_FAILED"
    return "LLM_UPSTREAM_FAILED"


def _safe_validation_items(exc: BaseException) -> list[dict[str, Any]]:
    if isinstance(exc, LlmClientError):
        summary: Any = exc.validation_summary
    elif isinstance(exc, ValidationError):
        summary = _safe_validation_summary(exc)
    else:
        return []
    if isinstance(summary, dict):
        summary = summary.get("items")
    if not isinstance(summary, list):
        return []
    return [
        {
            "path": item["path"],
            "error_type": item["error_type"],
            "count": item["count"],
        }
        for item in summary
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and isinstance(item.get("error_type"), str)
        and isinstance(item.get("count"), int)
        and not isinstance(item.get("count"), bool)
    ]


def _mapping_failure_code(exc: BaseException) -> str:
    """Return the deepest stable mapping error code without exposing content."""

    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, LlmClientError):
            if current.failure_code:
                return current.failure_code
            if isinstance(
                current.__cause__,
                (
                    LlmClientError,
                    EvidenceValidationError,
                    ValidationError,
                    WorkflowError,
                ),
            ):
                current = current.__cause__
                continue
            return {
                "LLM_SCHEMA_INVALID": "LLM_RESPONSE_SCHEMA_INVALID",
                "LLM_RESPONSE_INVALID": "LLM_RESPONSE_ENVELOPE_INVALID",
            }.get(current.code, current.code)
        if isinstance(current, EvidenceValidationError):
            return current.code
        if isinstance(current, ValidationError):
            return "LLM_RESPONSE_SCHEMA_INVALID"
        if isinstance(current, WorkflowError):
            details = current.details
            if isinstance(details, dict) and isinstance(details.get("failure_code"), str):
                return str(details["failure_code"])
            current = current.__cause__
            continue
        if isinstance(current, TimeoutError):
            return "LLM_TIMEOUT"
        current = current.__cause__
    return type(exc).__name__


def _mapping_failure_details(
    document: ParsedDocument,
    payload: dict[str, Any],
    exc: BaseException,
) -> dict[str, Any]:
    """Build the safe diagnostic boundary for one reference mapping request."""

    return {
        "failure_stage": "FACT_MAPPING",
        "chain": "mapping",
        "file": document.file_id,
        "file_id": document.file_id,
        "batch_depth": 0,
        "unit_count": len(payload.get("reference_facts", [])),
        "failure_code": _mapping_failure_code(exc),
        "request_attempts": max(int(getattr(exc, "request_attempts", 0) or 0), 0),
        "structure_retries": max(int(getattr(exc, "structure_retries", 0) or 0), 0),
    }


class DraftReviewState(TypedDict, total=False):
    task_id: str
    files: list[dict[str, Any]]
    options: dict[str, Any]
    local_files: list[LocalFile]
    parsed_documents: list[ParsedDocument]
    template_review: TemplateReviewResult
    llm_extractions: dict[str, dict[str, Any]]
    llm_reviews: dict[str, dict[str, Any]]
    llm_mappings: dict[str, dict[str, Any]]
    llm_mapping_reviews: dict[str, dict[str, Any]]
    llm_semantic_plans: dict[str, dict[str, Any]]
    page_location_sidecars: dict[str, Any]
    result: dict[str, Any]


class DraftReviewWorkflowExecutor:
    """Real download/parse slice; deterministic review and LLM nodes follow later."""

    def __init__(
        self,
        settings: Settings,
        *,
        downloader: SafeFileDownloadService | None = None,
        parsers: ParserRegistry | None = None,
        document_router: DocumentParsingRouter | None = None,
        llm: ContractLlmClient | None = None,
        checkpoint_store: Any | None = None,
    ) -> None:
        self.settings = settings
        self.downloader = downloader or SafeFileDownloadService(settings)
        local_parsers = parsers or ParserRegistry(
            pdf_min_text_chars_per_page=settings.PDF_MIN_TEXT_CHARS_PER_PAGE
        )
        self.parsers = document_router or DocumentParsingRouter(
            local=local_parsers,
            external=TextInDocumentParser(settings)
            if settings.OCR_ENABLED or settings.DOCX_PAGE_LOCATION_ENABLED
            else None,
            docx_page_location_enabled=settings.DOCX_PAGE_LOCATION_ENABLED,
        )
        if llm is not None:
            self.llm = llm
        elif settings.llm_configured:
            self.llm = OpenAIContractLlmClient(settings)
        else:
            self.llm = None
        self.checkpoint_store = checkpoint_store

    def _build_graph(self, workspace: TaskWorkspace, callback: ProgressCallback):
        graph = StateGraph(DraftReviewState)

        async def download_files(state: DraftReviewState) -> dict[str, Any]:
            await callback(TaskStage.DOWNLOADING, 10, "正在受控下载起草检查文件")
            return {"local_files": await self.downloader.prepare(state["files"], workspace)}

        async def parse_documents(state: DraftReviewState) -> dict[str, Any]:
            await callback(TaskStage.PARSING, 35, "正在逐份解析目标、模板和辅助资料")
            parsed = await self.parsers.parse_draft_review(state["local_files"])
            sidecars = getattr(self.parsers, "page_location_sidecars", {})
            if self.settings.DOCX_PAGE_LOCATION_ENABLED:
                missing = [
                    file.file_id
                    for file in state["local_files"]
                    if file.detected_mime_type == DOCX_MIME and file.file_id not in sidecars
                ]
                if missing:
                    raise WorkflowError(
                        "DOCX_PAGE_LOCATION_INCOMPLETE",
                        "DOCX 真实页码解析或映射未能可靠完成",
                        details={
                            "failure_stage": "PUBLIC_EVIDENCE_MAPPING",
                            "failure_code": "SIDECAR_MISSING",
                            "page_count": None,
                            "external_detail_page_count": 0,
                            "external_detail_count": 0,
                            "local_structure_count": 0,
                            "external_structure_count": 0,
                            "candidate_mapping_count": 0,
                            "unmapped_location_count": len(missing),
                            "missing_file_count": len(missing),
                        },
                    )
            return {
                "parsed_documents": parsed,
                "page_location_sidecars": sidecars,
            }

        async def compare_template(state: DraftReviewState) -> dict[str, Any]:
            await callback(TaskStage.TEMPLATE_COMPARE, 65, "正在对齐模板固定条款和允许填写区域")
            by_role = {document.role: document for document in state["parsed_documents"]}
            if "TARGET" not in by_role or "TEMPLATE" not in by_role:
                raise WorkflowError("COMPARISON_FAILED", "起草检查缺少目标合同或模板")
            options = state.get("options", {})
            review = analyze_template(
                by_role["TEMPLATE"],
                by_role["TARGET"],
                ignore_formatting=options.get("ignore_formatting", True),
                ignore_headers_footers=options.get("ignore_headers_footers", True),
                check_blank_fields=options.get("check_blank_fields", True),
                ocr_low_confidence_threshold=self.settings.OCR_LOW_CONFIDENCE_THRESHOLD,
                page_missing_min_equivalent=self.settings.PAGE_MISSING_MIN_EQUIVALENT,
                page_missing_min_anchor_similarity=(
                    self.settings.PAGE_MISSING_MIN_ANCHOR_SIMILARITY
                ),
                page_missing_min_structure_units=(
                    self.settings.PAGE_MISSING_MIN_STRUCTURE_UNITS
                ),
            )
            if not review.diagnostics.comparison.reliable:
                raise WorkflowError("COMPARISON_UNRELIABLE", "目标合同与模板的对齐覆盖率不足")
            return {"template_review": review}

        async def extract_facts(state: DraftReviewState) -> dict[str, Any]:
            if self.llm is None:
                return {"llm_extractions": {}}
            await callback(TaskStage.FACT_EXTRACTION, 75, "正在逐份抽取合同事实并保留证据")

            if all(
                hasattr(self.llm, method)
                for method in (
                    "extract_document_profile",
                    "extract_numeric_candidates",
                    "extract_text_facts",
                )
            ):
                try:
                    source_file_ids = state.get("options", {}).get(
                        "_checkpoint_source_file_ids", {}
                    )
                    source_file_ids_by_file_id = {
                        document.file_id: str(source_file_ids[str(index)])
                        for index, document in enumerate(state["parsed_documents"])
                        if isinstance(source_file_ids, dict)
                        and source_file_ids.get(str(index))
                    }
                    extractions, _profile_meta = await (
                        extract_documents_with_independent_map_reduce(
                            settings=self.settings,
                            documents=state["parsed_documents"],
                            llm=self.llm,
                            checkpoint_store=self.checkpoint_store,
                            task_id=state.get("task_id"),
                            source_task_id=state.get("options", {}).get("source_task_id"),
                            source_file_ids_by_file_id=source_file_ids_by_file_id,
                            text_candidates_by_document={
                                document.file_id: build_template_text_candidates(
                                    state["template_review"], document
                                )
                                for document in state["parsed_documents"]
                                if document.role == "TARGET"
                            },
                        )
                    )
                    # The remainder of this node (fact review and its
                    # deterministic merge) is shared with the compatibility
                    # path below.
                    state = {**state, "llm_extractions": extractions}
                    reviews: dict[str, dict[str, Any]] = {}
                    review_method = (
                        getattr(self.llm, "review_facts", None)
                        if self.settings.LLM_FACT_REVIEW_ENABLED
                        else None
                    )
                    if review_method is not None:
                        source_task_id = state.get("options", {}).get("source_task_id")
                        task_id = state.get("task_id")

                        async def review_one(
                            document: ParsedDocument,
                            review_payload: dict[str, Any],
                        ) -> tuple[dict[str, Any], FactReview, LlmResult]:
                            batch_id = stable_review_batch_id(document, review_payload)
                            payload_digest = review_payload_digest(review_payload)
                            checkpoint = None
                            if self.checkpoint_store is not None:
                                checkpoint = await self.checkpoint_store.load(
                                    batch_id,
                                    task_id=task_id,
                                    source_task_id=source_task_id,
                                    file_sha256=document.sha256,
                                    extraction_version=FACT_REVIEW_CHECKPOINT_VERSION,
                                    payload_digest=payload_digest,
                                )
                            if checkpoint is not None and checkpoint.value is not None:
                                review = FactReview.model_validate(checkpoint.value)
                                if (
                                    task_id
                                    and checkpoint.task_id
                                    and checkpoint.task_id != task_id
                                ):
                                    await self.checkpoint_store.save(
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
                                return (
                                    review_payload,
                                    review,
                                    LlmResult(
                                        value=review.model_dump(mode="json"),
                                        configured_model=checkpoint.model_name or "CHECKPOINT",
                                        actual_model=None,
                                        mock=False,
                                    ),
                                )
                            result = await review_method(review_payload)
                            review = FactReview.model_validate(result.value)
                            if self.checkpoint_store is not None:
                                await self.checkpoint_store.save(
                                    ExtractionCheckpoint(
                                        task_id=task_id,
                                        file_sha256=document.sha256,
                                        extraction_version=FACT_REVIEW_CHECKPOINT_VERSION,
                                        batch_id=batch_id,
                                        payload_digest=payload_digest,
                                        value=review.model_dump(mode="json"),
                                        status="SUCCEEDED",
                                        model_name=result.configured_model,
                                        source_task_id=source_task_id,
                                    )
                                )
                            return review_payload, review, result

                        async def review_all(
                            document: ParsedDocument,
                            review_payloads: list[dict[str, Any]],
                        ) -> list[tuple[dict[str, Any], FactReview, LlmResult]]:
                            semaphore = asyncio.Semaphore(
                                self.settings.LLM_EXTRACTION_TASK_CONCURRENCY
                            )

                            async def bounded_review(
                                review_payload: dict[str, Any],
                            ) -> tuple[dict[str, Any], FactReview, LlmResult]:
                                async with semaphore:
                                    return await review_one(document, review_payload)

                            tasks = [
                                asyncio.create_task(bounded_review(review_payload))
                                for review_payload in review_payloads
                            ]
                            try:
                                return list(await asyncio.gather(*tasks))
                            except BaseException:
                                for task in tasks:
                                    if not task.done():
                                        task.cancel()
                                await asyncio.gather(*tasks, return_exceptions=True)
                                raise

                        for document in state["parsed_documents"]:
                            if document.role == "TEMPLATE":
                                continue
                            merged = DocumentFactExtraction.model_validate(
                                extractions[document.file_id]["value"]
                            )
                            review_payloads = build_fact_review_batches(
                                document,
                                merged,
                                max_chars=self.settings.LLM_REVIEW_BATCH_MAX_CHARS,
                                context_blocks=self.settings.LLM_REVIEW_CONTEXT_BLOCKS,
                            )
                            reviewed_batches: list[tuple[dict[str, Any], FactReview]] = []
                            configured_models: set[str] = set()
                            actual_models: set[str] = set()
                            review_duration_ms = 0
                            review_request_attempts = 0
                            review_structure_retries = 0
                            review_results = await review_all(document, review_payloads)
                            for review_payload, review, review_result in review_results:
                                reviewed_batches.append((review_payload, review))
                                if review_result.configured_model:
                                    configured_models.add(review_result.configured_model)
                                if review_result.actual_model:
                                    actual_models.add(review_result.actual_model)
                                review_duration_ms += review_result.duration_ms
                                review_request_attempts += review_result.request_attempts
                                review_structure_retries += review_result.structure_retries
                            if len(configured_models) > 1 or len(actual_models) > 1:
                                raise EvidenceValidationError(
                                    "review model identity changed between batches"
                                )
                            review = merge_fact_review_batches(
                                document,
                                merged,
                                reviewed_batches,
                            )
                            reviews[document.file_id] = {
                                "value": review.model_dump(mode="json"),
                                "configured_model": next(iter(configured_models), None),
                                "actual_model": next(iter(actual_models), None),
                                "duration_ms": review_duration_ms,
                                "request_attempts": review_request_attempts,
                                "structure_retries": review_structure_retries,
                                "batch_count": len(review_payloads),
                            }
                    return {"llm_extractions": extractions, "llm_reviews": reviews}
                except (WorkflowError, EvidenceValidationError, LlmClientError):
                    raise
            elif all(
                hasattr(self.llm, method)
                for method in ("extract_document_profile", "extract_fact_batch")
            ):
                try:
                    extractions, _profile_meta = await extract_documents_with_map_reduce(
                        settings=self.settings,
                        documents=state["parsed_documents"],
                        llm=self.llm,
                    )
                    reviews: dict[str, dict[str, Any]] = {}
                    review_method = (
                        getattr(self.llm, "review_facts", None)
                        if self.settings.LLM_FACT_REVIEW_ENABLED
                        else None
                    )
                    if review_method is not None:
                        for document in state["parsed_documents"]:
                            if document.role == "TEMPLATE":
                                continue
                            merged = DocumentFactExtraction.model_validate(
                                extractions[document.file_id]["value"]
                            )
                            review_payloads = build_fact_review_batches(
                                document,
                                merged,
                                max_chars=self.settings.LLM_REVIEW_BATCH_MAX_CHARS,
                                context_blocks=self.settings.LLM_REVIEW_CONTEXT_BLOCKS,
                            )
                            reviewed_batches: list[tuple[dict[str, Any], FactReview]] = []
                            configured_models: set[str] = set()
                            actual_models: set[str] = set()
                            review_duration_ms = 0
                            review_request_attempts = 0
                            review_structure_retries = 0
                            for review_payload in review_payloads:
                                review_result = await review_method(review_payload)
                                review = FactReview.model_validate(review_result.value)
                                reviewed_batches.append((review_payload, review))
                                if review_result.configured_model:
                                    configured_models.add(review_result.configured_model)
                                if review_result.actual_model:
                                    actual_models.add(review_result.actual_model)
                                review_duration_ms += review_result.duration_ms
                                review_request_attempts += review_result.request_attempts
                                review_structure_retries += review_result.structure_retries
                            if len(configured_models) > 1 or len(actual_models) > 1:
                                raise EvidenceValidationError(
                                    "review model identity changed between batches"
                                )
                            review = merge_fact_review_batches(
                                document,
                                merged,
                                reviewed_batches,
                            )
                            reviews[document.file_id] = {
                                "value": review.model_dump(mode="json"),
                                "configured_model": next(iter(configured_models), None),
                                "actual_model": next(iter(actual_models), None),
                                "duration_ms": review_duration_ms,
                                "request_attempts": review_request_attempts,
                                "structure_retries": review_structure_retries,
                                "batch_count": len(review_payloads),
                            }
                    return {"llm_extractions": extractions, "llm_reviews": reviews}
                except (
                    LlmClientError,
                    EvidenceValidationError,
                    ValidationError,
                    TimeoutError,
                    WorkflowError,
                ) as exc:
                    logger.error(
                        "draft_review_dynamic_check_failed",
                        task_id=state["task_id"],
                        stage="FACT_EXTRACTION",
                        error_category=_dynamic_failure_code(exc),
                        error_code=getattr(exc, "code", None),
                    )
                    if isinstance(exc, WorkflowError) and exc.code == "DYNAMIC_CHECK_INCOMPLETE":
                        raise
                    raise WorkflowError(
                        "DYNAMIC_CHECK_INCOMPLETE",
                        "动态事实抽取未能可靠完成",
                    ) from exc

            extractions: dict[str, dict[str, Any]] = {}
            reviews: dict[str, dict[str, Any]] = {}
            for document in state["parsed_documents"]:
                if document.role == "TEMPLATE":
                    continue
                try:
                    chunk_results = []
                    configured_model = None
                    actual_model = None
                    duration_ms = 0
                    request_attempts = 0
                    structure_retries = 0
                    current_chunk_index = 0
                    split_count = 0
                    max_split_depth = 0
                    max_payload_chars = 0
                    numeric_candidate_total = 0
                    current_split_depth = 0
                    failure_counts: dict[str, int] = {}
                    chunks = chunk_document(
                        document,
                        self.settings.LLM_CHUNK_MAX_CHARS,
                        max_numeric_candidates=MAX_NUMERIC_CANDIDATES_PER_CHUNK,
                        max_payload_chars=self.settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
                    )
                    pending: list[tuple[list[DocumentBlock], int, int]] = [
                        (blocks, 0, chunk_index)
                        for chunk_index, blocks in enumerate(chunks, start=1)
                    ]
                    next_batch_index = len(pending) + 1
                    while pending:
                        blocks, split_depth, batch_index = pending.pop(0)
                        current_chunk_index = batch_index
                        current_split_depth = split_depth
                        payload = compact_extraction_payload(document, blocks)
                        payload_chars = extraction_payload_chars(payload)
                        max_payload_chars = max(max_payload_chars, payload_chars)
                        numeric_candidate_total += payload["numeric_candidate_metrics"].get(
                            "candidate_unique", 0
                        )
                        if (
                            request_attempts + 2
                            > self.settings.LLM_EXTRACTION_MAX_REQUESTS_PER_DOCUMENT
                        ):
                            raise LlmClientError(
                                "LLM_REQUEST_REJECTED",
                                "事实抽取调用预算已用尽",
                            )
                        try:
                            extraction = await self.llm.extract_facts(payload)
                        except LlmClientError as exc:
                            request_attempts += max(getattr(exc, "request_attempts", 0), 1)
                            structure_retries += getattr(exc, "structure_retries", 0)
                            if (
                                exc.code == "LLM_INVALID_JSON"
                                and len(blocks) > 1
                                and split_depth < self.settings.LLM_EXTRACTION_MAX_SPLIT_DEPTH
                                and request_attempts + 4
                                <= self.settings.LLM_EXTRACTION_MAX_REQUESTS_PER_DOCUMENT
                            ):
                                midpoint = len(blocks) // 2
                                left = blocks[:midpoint]
                                right = blocks[midpoint:]
                                pending[0:0] = [
                                    (left, split_depth + 1, next_batch_index),
                                    (right, split_depth + 1, next_batch_index + 1),
                                ]
                                next_batch_index += 2
                                split_count += 1
                                max_split_depth = max(max_split_depth, split_depth + 1)
                                continue
                            raise
                        chunk_results.append(
                            DocumentFactExtraction.model_validate(extraction.value)
                        )
                        configured_model = extraction.configured_model
                        actual_model = extraction.actual_model or actual_model
                        duration_ms += extraction.duration_ms
                        request_attempts += extraction.request_attempts
                        structure_retries += extraction.structure_retries
                    merged = merge_chunk_extractions(document, chunk_results)
                    extractions[document.file_id] = {
                        "value": merged.model_dump(mode="json"),
                        "configured_model": configured_model,
                        "actual_model": actual_model,
                        "duration_ms": duration_ms,
                        "request_attempts": request_attempts,
                        "structure_retries": structure_retries,
                        "chunk_count": len(chunk_results),
                        "split_count": split_count,
                        "max_split_depth": max_split_depth,
                        "max_payload_chars": max_payload_chars,
                        "numeric_candidate_total": numeric_candidate_total,
                    }
                    review_method = (
                        getattr(self.llm, "review_facts", None)
                        if self.settings.LLM_FACT_REVIEW_ENABLED
                        else None
                    )
                    if review_method is not None:
                        review_payloads = build_fact_review_batches(
                            document,
                            merged,
                            max_chars=self.settings.LLM_REVIEW_BATCH_MAX_CHARS,
                            context_blocks=self.settings.LLM_REVIEW_CONTEXT_BLOCKS,
                        )
                        reviewed_batches: list[tuple[dict[str, Any], FactReview]] = []
                        configured_models: set[str] = set()
                        actual_models: set[str] = set()
                        review_duration_ms = 0
                        review_request_attempts = 0
                        review_structure_retries = 0
                        for review_payload in review_payloads:
                            review_result = await review_method(review_payload)
                            review = FactReview.model_validate(review_result.value)
                            reviewed_batches.append((review_payload, review))
                            if review_result.configured_model:
                                configured_models.add(review_result.configured_model)
                            if review_result.actual_model:
                                actual_models.add(review_result.actual_model)
                            review_duration_ms += review_result.duration_ms
                            review_request_attempts += review_result.request_attempts
                            review_structure_retries += review_result.structure_retries
                        if len(configured_models) > 1 or len(actual_models) > 1:
                            raise EvidenceValidationError(
                                "review model identity changed between batches"
                            )
                        review = merge_fact_review_batches(
                            document,
                            merged,
                            reviewed_batches,
                        )
                        reviews[document.file_id] = {
                            "value": review.model_dump(mode="json"),
                            "configured_model": next(iter(configured_models), None),
                            "actual_model": next(iter(actual_models), None),
                            "duration_ms": review_duration_ms,
                            "request_attempts": review_request_attempts,
                            "structure_retries": review_structure_retries,
                            "batch_count": len(review_payloads),
                        }
                except (
                    LlmClientError,
                    EvidenceValidationError,
                    ValidationError,
                    TimeoutError,
                ) as exc:
                    error_category = _dynamic_failure_code(exc)
                    failure_counts[error_category] = failure_counts.get(error_category, 0) + 1
                    log_fields: dict[str, Any] = {
                        "task_id": state["task_id"],
                        "stage": "FACT_EXTRACTION",
                        "document_role": document.role,
                        "chunk_index": current_chunk_index,
                        "split_depth": current_split_depth,
                        "error_category": error_category,
                        "affected_count": 1,
                        "failure_counts": failure_counts,
                        "request_attempts": request_attempts,
                        "structure_retries": structure_retries,
                        "split_count": split_count,
                        "max_payload_chars": max_payload_chars,
                        "numeric_candidate_total": numeric_candidate_total,
                    }
                    if isinstance(exc, LlmClientError):
                        log_fields["error_code"] = exc.code
                    validation_items = _safe_validation_items(exc)
                    if error_category == "LLM_RESPONSE_SCHEMA_INVALID":
                        log_fields["validation_summary_status"] = (
                            "PRESENT" if validation_items else "MISSING"
                        )
                        if validation_items:
                            log_fields["validation_summary"] = validation_items
                    logger.error("draft_review_dynamic_check_failed", **log_fields)
                    raise WorkflowError(
                        "DYNAMIC_CHECK_INCOMPLETE",
                        f"文件 {document.file_name} 的动态事实检查未能可靠完成",
                    ) from exc
            return {"llm_extractions": extractions, "llm_reviews": reviews}

        async def map_cross_document_facts(state: DraftReviewState) -> dict[str, Any]:
            mappings: dict[str, dict[str, Any]] = {}
            mapping_reviews: dict[str, dict[str, Any]] = {}
            if self.llm is None or not hasattr(self.llm, "map_facts"):
                return {"llm_mappings": mappings, "llm_mapping_reviews": mapping_reviews}
            mapping_review_enabled = bool(
                self.settings.LLM_MAPPING_REVIEW_ENABLED
                and hasattr(self.llm, "review_mappings")
            )
            target_document = next(
                (document for document in state["parsed_documents"] if document.role == "TARGET"),
                None,
            )
            if target_document is None:
                return {"llm_mappings": mappings, "llm_mapping_reviews": mapping_reviews}
            target_value = state.get("llm_extractions", {}).get(target_document.file_id, {}).get(
                "value"
            )
            if not target_value:
                return {"llm_mappings": mappings, "llm_mapping_reviews": mapping_reviews}
            target_extraction = DocumentFactExtraction.model_validate(target_value)
            target_review_value = state.get("llm_reviews", {}).get(
                target_document.file_id, {}
            ).get("value")
            target_review = (
                FactReview.model_validate(target_review_value) if target_review_value else None
            )
            target_meta = state.get("llm_extractions", {}).get(target_document.file_id, {})
            target_review_meta = state.get("llm_reviews", {}).get(target_document.file_id, {})
            target_accepted_refs = qualified_fact_refs(
                target_extraction,
                target_review,
                self.settings.LLM_CONSENSUS_MIN_CONFIDENCE,
                extraction_model=target_meta.get("actual_model")
                or target_meta.get("configured_model"),
                review_model=target_review_meta.get("actual_model")
                or target_review_meta.get("configured_model"),
                document=target_document,
                require_independent_model=self.settings.LLM_REQUIRE_INDEPENDENT_MODEL,
                require_review=self.settings.LLM_FACT_REVIEW_ENABLED,
            )
            all_target_catalog = target_fact_catalog(target_extraction)
            catalog = [
                item
                for fact, item in zip(target_extraction.facts, all_target_catalog, strict=True)
                if (stable_fact_id(fact), fact.source_file_id) in target_accepted_refs
            ]
            catalog = [_compact_mapping_fact(item) for item in catalog]
            target_ids = {item["target_fact_id"] for item in catalog}
            await callback(TaskStage.CROSS_VALIDATE, 80, "正在逐份映射目标合同与辅助资料事实")
            for document in state["parsed_documents"]:
                if document.role != "REFERENCE":
                    continue
                reference_value = state.get("llm_extractions", {}).get(document.file_id, {}).get(
                    "value"
                )
                if not reference_value:
                    continue
                reference_extraction = DocumentFactExtraction.model_validate(reference_value)
                reference_review_value = state.get("llm_reviews", {}).get(
                    document.file_id, {}
                ).get("value")
                reference_review = (
                    FactReview.model_validate(reference_review_value)
                    if reference_review_value
                    else None
                )
                reference_meta = state.get("llm_extractions", {}).get(document.file_id, {})
                reference_review_meta = state.get("llm_reviews", {}).get(document.file_id, {})
                reference_accepted_refs = qualified_fact_refs(
                    reference_extraction,
                    reference_review,
                    self.settings.LLM_CONSENSUS_MIN_CONFIDENCE,
                    extraction_model=reference_meta.get("actual_model")
                    or reference_meta.get("configured_model"),
                    review_model=reference_review_meta.get("actual_model")
                    or reference_review_meta.get("configured_model"),
                    document=document,
                    require_independent_model=self.settings.LLM_REQUIRE_INDEPENDENT_MODEL,
                    require_review=self.settings.LLM_FACT_REVIEW_ENABLED,
                )
                accepted_reference_facts = [
                    fact
                    for fact in reference_extraction.facts
                    if (stable_fact_id(fact), fact.source_file_id) in reference_accepted_refs
                ]
                reference_index = {
                    (fact.field_key, fact.source_file_id, location_key(fact.location)): fact
                    for fact in accepted_reference_facts
                }
                payload = {
                    "reference_file_id": document.file_id,
                    "reference_profile": reference_extraction.profile.model_dump(mode="json"),
                    "target_facts": catalog,
                    "reference_facts": [
                        _compact_mapping_fact(fact.model_dump(mode="json"))
                        for fact in accepted_reference_facts
                    ],
                }
                try:
                    if not catalog or not accepted_reference_facts:
                        empty_mapping = FactMappingResponse(
                            reference_file_id=document.file_id,
                            mappings=[],
                            missing_requirements=[],
                        )
                        mappings[document.file_id] = {
                            "value": empty_mapping.model_dump(mode="json"),
                            "status": "SKIPPED_NO_QUALIFIED_FACTS",
                        }
                        if mapping_review_enabled:
                            empty_review = FactMappingReview(
                                reference_file_id=document.file_id,
                                decisions=[],
                                missing_requirement_decisions=[],
                                confidence=0.0,
                                evidence_complete=True,
                            )
                            mapping_reviews[document.file_id] = {
                                "value": empty_review.model_dump(mode="json"),
                                "status": "SKIPPED_NO_QUALIFIED_FACTS",
                            }
                        continue
                    mapping_result = await self.llm.map_facts(payload)
                    mapping = FactMappingResponse.model_validate(mapping_result.value)
                    if mapping.reference_file_id != document.file_id:
                        raise EvidenceValidationError(
                            "mapping reference file does not match",
                            code="FACT_MAPPING_REFERENCE_FILE_MISMATCH",
                        )
                    proposed_keys = set()
                    for proposal in mapping.mappings:
                        key = (
                            proposal.target_fact_id,
                            proposal.reference_field_key,
                            proposal.source_file_id,
                            location_key(proposal.reference_location),
                        )
                        if proposal.target_fact_id not in target_ids:
                            raise EvidenceValidationError(
                                "mapping target fact does not exist",
                                code="FACT_MAPPING_TARGET_NOT_FOUND",
                            )
                        if proposal.source_file_id != document.file_id:
                            raise EvidenceValidationError(
                                "mapping source file does not match",
                                code="FACT_MAPPING_SOURCE_FILE_MISMATCH",
                            )
                        if (
                            proposal.reference_field_key,
                            proposal.source_file_id,
                            location_key(proposal.reference_location),
                        ) not in reference_index:
                            raise EvidenceValidationError(
                                "mapping reference fact does not exist",
                                code="FACT_MAPPING_REFERENCE_FACT_NOT_FOUND",
                            )
                        if key in proposed_keys:
                            raise EvidenceValidationError(
                                "mapping contains duplicate proposal",
                                code="FACT_IDENTITY_DUPLICATED",
                            )
                        proposed_keys.add(key)
                    requirement_ids = {
                        requirement.target_fact_id for requirement in mapping.missing_requirements
                    }
                    if not requirement_ids <= target_ids:
                        raise EvidenceValidationError(
                            "missing requirement target does not exist",
                            code="FACT_MAPPING_TARGET_NOT_FOUND",
                        )
                    mappings[document.file_id] = {
                        "value": mapping.model_dump(mode="json"),
                        "configured_model": mapping_result.configured_model,
                        "actual_model": mapping_result.actual_model,
                        "duration_ms": mapping_result.duration_ms,
                        "request_attempts": mapping_result.request_attempts,
                        "structure_retries": mapping_result.structure_retries,
                    }
                    if not mapping_review_enabled:
                        continue
                    catalog_by_id = {
                        item["target_fact_id"]: item for item in catalog
                    }
                    review_target_ids = {
                        proposal.target_fact_id for proposal in mapping.mappings
                    }
                    review_target_ids.update(
                        requirement.target_fact_id
                        for requirement in mapping.missing_requirements
                    )
                    review_reference_keys = {
                        (
                            proposal.reference_field_key,
                            proposal.source_file_id,
                            location_key(proposal.reference_location),
                        )
                        for proposal in mapping.mappings
                    }
                    review_payload = {
                        "reference_file_id": document.file_id,
                        "reference_profile": payload["reference_profile"],
                        "target_facts": [
                            catalog_by_id[target_fact_id]
                            for target_fact_id in sorted(review_target_ids)
                        ],
                        "reference_facts": [
                            _compact_mapping_fact(
                                reference_index[key].model_dump(mode="json")
                            )
                            for key in sorted(review_reference_keys, key=repr)
                        ],
                        "proposed_mapping": mapping.model_dump(mode="json"),
                    }
                    review_result = await self.llm.review_mappings(review_payload)
                    review = FactMappingReview.model_validate(review_result.value)
                    validate_mapping_review_coverage(
                        review=review,
                        mapping=mapping,
                        reference_file_id=document.file_id,
                    )
                    mapping_model = (
                        mapping_result.actual_model or mapping_result.configured_model
                    )
                    mapping_review_model = (
                        review_result.actual_model or review_result.configured_model
                    )
                    if (
                        self.settings.LLM_REQUIRE_INDEPENDENT_MODEL
                        and (
                            not mapping_model
                            or not mapping_review_model
                            or mapping_model == mapping_review_model
                        )
                    ):
                        raise EvidenceValidationError(
                            "mapping and mapping review did not use independent models",
                            code="MAPPING_MODEL_NOT_INDEPENDENT",
                        )
                    mapping_reviews[document.file_id] = {
                        "value": review.model_dump(mode="json"),
                        "configured_model": review_result.configured_model,
                        "actual_model": review_result.actual_model,
                        "duration_ms": review_result.duration_ms,
                        "request_attempts": review_result.request_attempts,
                        "structure_retries": review_result.structure_retries,
                    }
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    details = _mapping_failure_details(document, payload, exc)
                    logger.error(
                        "draft_review_mapping_failed",
                        task_id=state["task_id"],
                        failure_stage=details["failure_stage"],
                        chain=details["chain"],
                        file_id=details["file_id"],
                        batch_depth=details["batch_depth"],
                        unit_count=details["unit_count"],
                        failure_code=details["failure_code"],
                        request_attempts=details["request_attempts"],
                        structure_retries=details["structure_retries"],
                    )
                    raise WorkflowError(
                        "DYNAMIC_CHECK_INCOMPLETE",
                        f"文件 {document.file_name} 的跨资料事实映射未能可靠完成",
                        details=details,
                    ) from exc
            return {"llm_mappings": mappings, "llm_mapping_reviews": mapping_reviews}

        async def plan_semantics(state: DraftReviewState) -> dict[str, Any]:
            if self.llm is None or not hasattr(self.llm, "plan_semantics"):
                return {"llm_semantic_plans": {}}
            dynamic_documents = [
                document for document in state["parsed_documents"] if document.role != "TEMPLATE"
            ]
            if not dynamic_documents:
                return {"llm_semantic_plans": {}}
            primary = next(
                (document for document in dynamic_documents if document.role == "TARGET"),
                dynamic_documents[0],
            )
            documents_by_file = {document.file_id: document for document in dynamic_documents}
            extractions_by_file: dict[str, DocumentFactExtraction] = {}
            reviews_by_file: dict[str, FactReview | None] = {}
            fact_index_source: dict[str, DocumentFactExtraction] = {}
            document_payloads: list[dict[str, Any]] = []
            for document in dynamic_documents:
                extracted_value = state.get("llm_extractions", {}).get(document.file_id, {}).get(
                    "value"
                )
                if not extracted_value:
                    raise WorkflowError(
                        "DYNAMIC_CHECK_INCOMPLETE",
                        f"文件 {document.file_name} 的事实结果不可用于语义规划",
                    )
                extraction = DocumentFactExtraction.model_validate(extracted_value)
                extractions_by_file[document.file_id] = extraction
                fact_index_source[document.file_id] = extraction
                review_value = state.get("llm_reviews", {}).get(document.file_id, {}).get("value")
                reviews_by_file[document.file_id] = (
                    FactReview.model_validate(review_value) if review_value else None
                )
            fact_index = build_fact_index(fact_index_source)
            accepted_refs: set[tuple[str, str]] = set()
            for file_id, extraction in extractions_by_file.items():
                accepted_refs.update(
                    qualified_fact_refs(
                        extraction,
                        reviews_by_file.get(file_id),
                        self.settings.LLM_CONSENSUS_MIN_CONFIDENCE,
                        extraction_model=(
                            state.get("llm_extractions", {})
                            .get(file_id, {})
                            .get("actual_model")
                            or state.get("llm_extractions", {})
                            .get(file_id, {})
                            .get("configured_model")
                        ),
                        review_model=(
                            state.get("llm_reviews", {})
                            .get(file_id, {})
                            .get("actual_model")
                            or state.get("llm_reviews", {})
                            .get(file_id, {})
                            .get("configured_model")
                        ),
                        document=documents_by_file[file_id],
                        require_independent_model=self.settings.LLM_REQUIRE_INDEPENDENT_MODEL,
                        require_review=self.settings.LLM_FACT_REVIEW_ENABLED,
                    )
                )
            for document in dynamic_documents:
                extraction = extractions_by_file[document.file_id]
                document_payloads.append(
                    {
                        "file_id": document.file_id,
                        "role": document.role,
                        "profile": extraction.profile.model_dump(mode="json"),
                        "facts": [
                            payload
                            for payload in fact_index_payload(
                                fact_index,
                                accepted_refs={
                                    ref
                                    for ref in accepted_refs
                                    if ref[1] == document.file_id
                                },
                            )
                        ],
                    }
                )
            target_catalog = {
                item["target_fact_id"]: item
                for item in target_fact_catalog(extractions_by_file[primary.file_id])
            }
            accepted_mappings: list[dict[str, Any]] = []
            for reference_file_id, mapping_entry in state.get("llm_mappings", {}).items():
                mapping_value = mapping_entry.get("value") or {}
                review_value = state.get("llm_mapping_reviews", {}).get(
                    reference_file_id, {}
                ).get("value")
                if not review_value:
                    continue
                mapping = FactMappingResponse.model_validate(mapping_value)
                mapping_review = FactMappingReview.model_validate(review_value)
                review_decisions = {
                    (
                        decision.target_fact_id,
                        decision.reference_field_key,
                        decision.source_file_id,
                        location_key(decision.reference_location),
                    ): decision
                    for decision in mapping_review.decisions
                }
                for proposal in mapping.mappings:
                    target_catalog_item = target_catalog.get(proposal.target_fact_id)
                    reference_extraction = extractions_by_file.get(reference_file_id)
                    if target_catalog_item is None or reference_extraction is None:
                        continue
                    reference_fact = next(
                        (
                            fact
                            for fact in reference_extraction.facts
                            if fact.field_key == proposal.reference_field_key
                            and location_key(fact.location)
                            == location_key(proposal.reference_location)
                        ),
                        None,
                    )
                    if reference_fact is None:
                        continue
                    target_fact = FactCandidate.model_validate(
                        {
                            key: value
                            for key, value in target_catalog_item.items()
                            if key != "target_fact_id"
                        }
                    )
                    decision_key = (
                        proposal.target_fact_id,
                        proposal.reference_field_key,
                        proposal.source_file_id,
                        location_key(proposal.reference_location),
                    )
                    decision = review_decisions.get(decision_key)
                    if (
                        proposal.decision == "MATCH"
                        and decision is not None
                        and decision.decision == "ACCEPT"
                        and proposal.confidence >= self.settings.LLM_CONSENSUS_MIN_CONFIDENCE
                        and decision.confidence >= self.settings.LLM_CONSENSUS_MIN_CONFIDENCE
                        and mapping_review.confidence
                        >= self.settings.LLM_CONSENSUS_MIN_CONFIDENCE
                        and mapping_review.evidence_complete
                    ):
                        target_ref = (stable_fact_id(target_fact), target_fact.source_file_id)
                        reference_ref = (
                            stable_fact_id(reference_fact),
                            reference_fact.source_file_id,
                        )
                        if target_ref in accepted_refs and reference_ref in accepted_refs:
                            accepted_mappings.append(
                                {
                                    "target": {
                                        "fact_id": target_ref[0],
                                        "source_file_id": target_ref[1],
                                    },
                                    "reference": {
                                        "fact_id": reference_ref[0],
                                        "source_file_id": reference_ref[1],
                                    },
                                    "decision": "ACCEPT",
                                }
                            )
            await callback(TaskStage.FACT_EXTRACTION, 82, "正在生成动态语义概念和数值校验计划")
            payload = {
                "file_id": primary.file_id,
                "role": primary.role,
                "documents": document_payloads,
                "accepted_mappings": accepted_mappings,
                "verification_mode": (
                    "SAME_MODEL_DIAGNOSTIC"
                    if self.settings.LLM_SAME_MODEL_DIAGNOSTIC
                    else "INDEPENDENT_REVIEW"
                ),
                "semantic_requirements": {
                    "open_ended_concepts": True,
                    "only_accepted_facts": True,
                    "numeric_ast_only": True,
                },
            }
            try:
                semantic_payloads = _build_semantic_plan_payloads(
                    payload,
                    fact_batch_size=self.settings.LLM_SEMANTIC_FACT_BATCH_SIZE,
                )
                semantic_semaphore = asyncio.Semaphore(
                    self.settings.LLM_EXTRACTION_TASK_CONCURRENCY
                )

                async def run_semantic_batch(
                    semantic_payload: dict[str, Any],
                ) -> tuple[SemanticPlanResponse, Any]:
                    async with semantic_semaphore:
                        semantic_result = await self.llm.plan_semantics(semantic_payload)
                    plan_part = SemanticPlanResponse.model_validate(semantic_result.value)
                    validate_semantic_plan(
                        primary_file_id=primary.file_id,
                        documents_by_file=documents_by_file,
                        plan=plan_part,
                        fact_index=fact_index,
                        accepted_refs=accepted_refs,
                    )
                    return plan_part, semantic_result

                semantic_tasks = [
                    asyncio.create_task(run_semantic_batch(semantic_payload))
                    for semantic_payload in semantic_payloads
                ]
                try:
                    semantic_parts = await asyncio.gather(*semantic_tasks)
                except BaseException:
                    for task in semantic_tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*semantic_tasks, return_exceptions=True)
                    raise
                plan_parts = [part for part, _result in semantic_parts]
                semantic_results = [result for _part, result in semantic_parts]
                plan = _merge_semantic_plan_parts(
                    plan_parts,
                    primary_file_id=primary.file_id,
                )
                configured_models = {
                    result.configured_model
                    for result in semantic_results
                    if result.configured_model
                }
                actual_models = {
                    result.actual_model
                    for result in semantic_results
                    if result.actual_model
                }
                if len(configured_models) > 1 or len(actual_models) > 1:
                    raise EvidenceValidationError(
                        "semantic planning model identity changed between batches"
                    )
                projected_concepts, projected_specs = project_semantic_plan(plan)
                updated_extractions = dict(state.get("llm_extractions", {}))
                existing = DocumentFactExtraction.model_validate(
                    updated_extractions[primary.file_id]["value"]
                )
                updated_extractions[primary.file_id] = {
                    **updated_extractions[primary.file_id],
                        "value": existing.model_copy(
                            update={
                            "semantic_concepts": projected_concepts,
                            "validation_specs": projected_specs,
                            }
                        ).model_dump(mode="json"),
                }
                plans = {
                    primary.file_id: {
                        "value": plan.model_dump(mode="json"),
                        "configured_model": next(iter(configured_models), None),
                        "actual_model": next(iter(actual_models), None),
                        "duration_ms": sum(
                            result.duration_ms for result in semantic_results
                        ),
                        "request_attempts": sum(
                            result.request_attempts for result in semantic_results
                        ),
                        "structure_retries": sum(
                            result.structure_retries for result in semantic_results
                        ),
                        "batch_count": len(semantic_payloads),
                        "verification_mode": (
                            "SAME_MODEL_DIAGNOSTIC"
                            if self.settings.LLM_SAME_MODEL_DIAGNOSTIC
                            else "INDEPENDENT_REVIEW"
                        ),
                        "internal_plan": plan,
                        "fact_index": fact_index,
                        "accepted_refs": accepted_refs,
                    }
                }
                return {
                    "llm_extractions": updated_extractions,
                    "llm_semantic_plans": plans,
                }
            except (LlmClientError, EvidenceValidationError, ValidationError, TimeoutError) as exc:
                raise WorkflowError(
                    "DYNAMIC_CHECK_INCOMPLETE",
                    "动态语义规划未能可靠完成",
                ) from exc

        async def build_result(state: DraftReviewState) -> dict[str, Any]:
            await callback(TaskStage.RULE_CHECKING, 85, "正在汇总模板差异和必填检查")
            result = self._build_result(
                state["task_id"],
                state["files"],
                state["parsed_documents"],
                state["template_review"],
                state.get("llm_extractions", {}),
                state.get("llm_reviews", {}),
                state.get("llm_mappings", {}),
                state.get("llm_mapping_reviews", {}),
                state.get("llm_semantic_plans", {}),
                state.get("options", {}),
            )
            return {"result": result}

        async def generate_advice(state: DraftReviewState) -> dict[str, Any]:
            result = state["result"]
            if self.llm is None or not hasattr(self.llm, "generate_advice"):
                return {}
            await callback(TaskStage.GENERATING_ADVICE, 92, "正在根据已有证据生成建议")
            try:
                generated = await self.llm.generate_advice(advice_payload(result))
                advice = AdviceResponse.model_validate(generated.value)
                if result.get("metadata", {}).get("review_mode") == "NOT_RUN":
                    advice = advice.model_copy(
                        update={
                            "limitations": list(
                                dict.fromkeys(
                                    [
                                        *advice.limitations,
                                        "跨资料对应由模型识别，并经过原文证据和程序规则校验，不构成法律判断",
                                    ]
                                )
                            )
                        }
                    )
                merge_model_advice(result, advice)
                result["metadata"].setdefault("model_runs", []).append(
                    {
                        "purpose": "RISK_ADVICE",
                        "configured_model": generated.configured_model,
                        "actual_model": generated.actual_model,
                        "duration_ms": generated.duration_ms,
                        "request_attempts": generated.request_attempts,
                        "structure_retries": generated.structure_retries,
                        "status": "SUCCEEDED",
                    }
                )
            except Exception:
                # Advice is supplemental and must never invalidate deterministic results.
                result.setdefault("warnings", []).append(
                    {
                        "code": "LLM_ADVICE_UNAVAILABLE",
                        "message": "模型建议未完成，已保留确定性分析建议。",
                        "requires_manual_review": False,
                    }
                )
                ensure_fallback_risk_advices(result)
            return {"result": result}

        async def persist_result(state: DraftReviewState) -> dict[str, Any]:
            await callback(TaskStage.PERSISTING_RESULT, 97, "正在保存多文档解析结果")
            result = state["result"]
            apply_docx_page_location_sidecars(
                result, state.get("page_location_sidecars", {})
            )
            return {"result": result}

        graph.add_node("download_files", download_files)
        graph.add_node("parse_documents", parse_documents)
        graph.add_node("compare_template", compare_template)
        graph.add_node("extract_facts", extract_facts)
        graph.add_node("map_cross_document_facts", map_cross_document_facts)
        graph.add_node("plan_semantics", plan_semantics)
        graph.add_node("build_result", build_result)
        graph.add_node("generate_advice", generate_advice)
        graph.add_node("persist_result", persist_result)
        graph.add_edge(START, "download_files")
        graph.add_edge("download_files", "parse_documents")
        graph.add_edge("parse_documents", "compare_template")
        graph.add_edge("compare_template", "extract_facts")
        graph.add_edge("extract_facts", "map_cross_document_facts")
        if self.settings.LLM_SEMANTIC_PLAN_ENABLED:
            graph.add_edge("map_cross_document_facts", "plan_semantics")
            graph.add_edge("plan_semantics", "build_result")
        else:
            # Delivery mode publishes only independently reviewed mappings and
            # deterministic fact comparisons. Semantic planning remains
            # available behind its internal configuration switch.
            graph.add_edge("map_cross_document_facts", "build_result")
        graph.add_edge("build_result", "generate_advice")
        graph.add_edge("generate_advice", "persist_result")
        graph.add_edge("persist_result", END)
        return graph.compile()

    @staticmethod
    def _parse_status(document: ParsedDocument) -> str:
        return (
            "WARNING"
            if any(warning.requires_manual_review for warning in document.warnings)
            else "SUCCEEDED"
        )

    @staticmethod
    def _content_structure(document: ParsedDocument) -> dict[str, Any]:
        return {
            "block_count": len(document.blocks),
            "table_count": sum(block.table is not None for block in document.blocks),
            "sample_locations": [
                block.location.model_dump(mode="json", exclude_none=True)
                for block in document.blocks[:5]
            ],
        }

    def _build_result(
        self,
        task_id: str,
        input_files: list[dict[str, Any]],
        documents: list[ParsedDocument],
        template_review: TemplateReviewResult,
        llm_extractions: dict[str, dict[str, Any]],
        llm_reviews: dict[str, dict[str, Any]] | None = None,
        llm_mappings: dict[str, dict[str, Any]] | None = None,
        llm_mapping_reviews: dict[str, dict[str, Any]] | None = None,
        llm_semantic_plans: dict[str, dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not template_review.diagnostics.comparison.reliable:
            raise WorkflowError(
                "COMPARISON_UNRELIABLE",
                "目标合同与模板的对齐覆盖率不足，未生成正式报告",
            )
        input_by_id = {item["file_id"]: item for item in input_files}
        files: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        llm_reviews = llm_reviews or {}
        llm_mappings = llm_mappings or {}
        llm_mapping_reviews = llm_mapping_reviews or {}
        llm_semantic_plans = llm_semantic_plans or {}
        options = options or {}
        review_enforced = bool(
            self.llm is not None
            and self.settings.LLM_FACT_REVIEW_ENABLED
            and hasattr(self.llm, "review_facts")
        )
        mapping_enforced = self.llm is not None and hasattr(self.llm, "map_facts")
        mapping_review_enforced = bool(
            self.llm is not None
            and self.settings.LLM_MAPPING_REVIEW_ENABLED
            and hasattr(self.llm, "review_mappings")
        )
        dynamic_documents = [
            document for document in documents if document.role != "TEMPLATE"
        ]
        if self.llm is not None and any(
            not llm_extractions.get(document.file_id, {}).get("value")
            for document in dynamic_documents
        ):
            raise WorkflowError(
                "DYNAMIC_CHECK_INCOMPLETE",
                "已启用的动态事实检查未覆盖全部业务文件，未生成正式报告",
            )
        if review_enforced and any(
            not llm_reviews.get(document.file_id, {}).get("value")
            for document in dynamic_documents
        ):
            raise WorkflowError(
                "DYNAMIC_CHECK_INCOMPLETE",
                "已启用的独立事实评审未覆盖全部业务文件，未生成正式报告",
            )
        reference_documents = [
            document for document in documents if document.role == "REFERENCE"
        ]
        if mapping_enforced and any(
            not llm_mappings.get(document.file_id, {}).get("value")
            for document in reference_documents
        ):
            raise WorkflowError(
                "DYNAMIC_CHECK_INCOMPLETE",
                "已启用的跨资料事实映射未覆盖全部参考文件，未生成正式报告",
            )
        if mapping_review_enforced and any(
            not llm_mapping_reviews.get(document.file_id, {}).get("value")
            for document in reference_documents
        ):
            raise WorkflowError(
                "DYNAMIC_CHECK_INCOMPLETE",
                "已启用的跨资料映射评审未覆盖全部参考文件，未生成正式报告",
            )
        extractions_by_file: dict[str, DocumentFactExtraction] = {}
        consensus_fields: set[tuple[str, str, tuple[object, ...]]] = set()
        strict_review_files: set[str] = set()
        validation_specs = []
        accepted_spec_ids: set[str] = set()
        semantic_plan_spec_ids = {
            spec["validation_id"]
            for plan in llm_semantic_plans.values()
            for spec in (plan.get("value") or {}).get("validation_specs", [])
            if isinstance(spec, dict) and spec.get("validation_id")
        }
        mapping_records: list[dict[str, Any]] = []
        required_missing: set[tuple[str, str]] = set()
        uncertain_reference_file_ids: set[str] = set()
        model_runs: list[dict[str, Any]] = []
        diagnostic_review_items: list[dict[str, Any]] = []
        diagnostic_mode = bool(
            self.settings.LLM_SAME_MODEL_DIAGNOSTIC and self.llm is not None
        )
        successful_extractions = 0
        for document in documents:
            document_warnings = [warning.model_dump(mode="json") for warning in document.warnings]
            for warning in document_warnings:
                warning["file_id"] = warning.get("file_id") or document.file_id
                warnings.append(warning)
            extraction = llm_extractions.get(document.file_id, {})
            extracted_value = extraction.get("value") or {}
            profile = extracted_value.get("profile") or {
                "document_kind": "UNKNOWN",
                "title": None,
                "confidence": 0.0,
                "generated_by": "NOT_RUN",
                "evidence_locations": [],
            }
            if extracted_value:
                profile = {**profile, "generated_by": "LLM"}
                successful_extractions += 1
                extractions_by_file[document.file_id] = DocumentFactExtraction.model_validate(
                    extracted_value
                )
                extraction_model = extraction.get("actual_model") or extraction.get(
                    "configured_model"
                )
                review = llm_reviews.get(document.file_id, {})
                review_value = review.get("value") or {}
                review_model = review.get("actual_model") or review.get("configured_model")
                strict_review = bool(review_value) and not diagnostic_mode and bool(
                    extraction_model and review_model and extraction_model != review_model
                )
                if strict_review:
                    strict_review_files.add(document.file_id)
                    review_obj = FactReview.model_validate(review_value)
                    decisions = {
                        (
                            decision.field_key,
                            decision.source_file_id,
                            location_key(decision.location),
                        ): decision
                        for decision in review_obj.decisions
                    }
                    for fact in extractions_by_file[document.file_id].facts:
                        key = (fact.field_key, fact.source_file_id, location_key(fact.location))
                        decision = decisions.get(key)
                        if (
                            decision
                            and decision.decision == "ACCEPT"
                            and decision.confidence >= self.settings.LLM_CONSENSUS_MIN_CONFIDENCE
                            and fact.confidence >= self.settings.LLM_CONSENSUS_MIN_CONFIDENCE
                            and review_obj.confidence >= self.settings.LLM_CONSENSUS_MIN_CONFIDENCE
                            and review_obj.evidence_complete
                        ):
                            consensus_fields.add(key)
                    extraction_specs = {
                        spec.validation_id: spec
                        for spec in extractions_by_file[document.file_id].validation_specs
                    }
                    for reviewed_spec in review_obj.validation_specs:
                        extracted_spec = extraction_specs.get(reviewed_spec.validation_id)
                        if extracted_spec is None:
                            continue
                        reviewed_value = reviewed_spec.model_dump(
                            mode="json", exclude={"confidence"}
                        )
                        extracted_value = extracted_spec.model_dump(
                            mode="json", exclude={"confidence"}
                        )
                        if (
                            reviewed_value == extracted_value
                            and reviewed_spec.confidence
                            >= self.settings.LLM_CONSENSUS_MIN_CONFIDENCE
                            and extracted_spec.confidence
                            >= self.settings.LLM_CONSENSUS_MIN_CONFIDENCE
                            and review_obj.confidence
                            >= self.settings.LLM_CONSENSUS_MIN_CONFIDENCE
                            and review_obj.evidence_complete
                        ):
                            accepted_spec_ids.add(reviewed_spec.validation_id)
                if not hasattr(self.llm, "review_facts"):
                    consensus_fields.update(
                        (fact.field_key, fact.source_file_id, location_key(fact.location))
                        for fact in extractions_by_file[document.file_id].facts
                    )
                if self.settings.LLM_SEMANTIC_PLAN_ENABLED:
                    validation_specs.extend(
                        extractions_by_file[document.file_id].validation_specs
                    )
                model_runs.append(
                    {
                        "file_id": document.file_id,
                        "purpose": "FACT_EXTRACTION",
                        "configured_model": extraction.get("configured_model"),
                        "actual_model": extraction.get("actual_model"),
                        "duration_ms": extraction.get("duration_ms", 0),
                        "request_attempts": extraction.get("request_attempts", 0),
                        "structure_retries": extraction.get("structure_retries", 0),
                        "chunk_count": extraction.get("chunk_count", 0),
                        "status": "SUCCEEDED",
                    }
                )
                if review_value:
                    model_runs.append(
                        {
                            "file_id": document.file_id,
                            "purpose": "FACT_REVIEW",
                            "configured_model": review.get("configured_model"),
                            "actual_model": review.get("actual_model"),
                            "duration_ms": review.get("duration_ms", 0),
                            "request_attempts": review.get("request_attempts", 0),
                            "structure_retries": review.get("structure_retries", 0),
                            "batch_count": review.get("batch_count", 0),
                            "status": "SUCCEEDED",
                        }
                    )
                semantic_plan = llm_semantic_plans.get(document.file_id, {})
                if semantic_plan.get("value"):
                    model_runs.append(
                        {
                            "file_id": document.file_id,
                            "purpose": "SEMANTIC_PLAN",
                            "configured_model": semantic_plan.get("configured_model"),
                            "actual_model": semantic_plan.get("actual_model"),
                            "duration_ms": semantic_plan.get("duration_ms", 0),
                            "request_attempts": semantic_plan.get("request_attempts", 0),
                            "structure_retries": semantic_plan.get("structure_retries", 0),
                            "status": "SUCCEEDED",
                        }
                    )
            files.append(
                {
                    "file_id": document.file_id,
                    "role": document.role,
                    "file_name": document.file_name,
                    "safe_url": input_by_id[document.file_id]["safe_url"],
                    "sha256": document.sha256,
                    "page_count": document.page_count,
                    "parser_name": document.parser_name,
                    "parse_status": self._parse_status(document),
                    "parse_warnings": document_warnings,
                    "parser_metadata": document.parser_metadata,
                    "document_profile": profile,
                    "content_structure": self._content_structure(document),
                }
            )
        fact_index = build_fact_index(extractions_by_file)
        accepted_refs: set[tuple[str, str]] = set()
        for file_id, extraction in extractions_by_file.items():
            review_value = llm_reviews.get(file_id, {}).get("value")
            review_obj = FactReview.model_validate(review_value) if review_value else None
            accepted_refs.update(
                qualified_fact_refs(
                    extraction,
                    review_obj,
                    self.settings.LLM_CONSENSUS_MIN_CONFIDENCE,
                    extraction_model=llm_extractions.get(file_id, {}).get("actual_model")
                    or llm_extractions.get(file_id, {}).get("configured_model"),
                    review_model=llm_reviews.get(file_id, {}).get("actual_model")
                    or llm_reviews.get(file_id, {}).get("configured_model"),
                    document=next(
                        document
                        for document in documents
                        if document.file_id == file_id
                    ),
                    require_independent_model=self.settings.LLM_REQUIRE_INDEPENDENT_MODEL,
                    require_review=self.settings.LLM_FACT_REVIEW_ENABLED,
                )
            )
        consensus_fields = {
            (
                entry.fact.field_key,
                entry.fact.source_file_id,
                location_key(entry.fact.location),
            )
            for ref, entry in fact_index.items()
            if ref in accepted_refs
        }
        qualified_fact_values = {
            ref: entry.fact for ref, entry in fact_index.items() if ref in accepted_refs
        }
        target_document = next(
            (document for document in documents if document.role == "TARGET"), None
        )
        accepted_target_fact_ids: set[str] = set()
        if target_document is not None and target_document.file_id in extractions_by_file:
            target_extraction = extractions_by_file[target_document.file_id]
            for fact, catalog_item in zip(
                target_extraction.facts,
                target_fact_catalog(target_extraction),
                strict=True,
            ):
                if (stable_fact_id(fact), fact.source_file_id) in accepted_refs:
                    accepted_target_fact_ids.add(catalog_item["target_fact_id"])
        accepted_reference_keys_by_file: dict[
            str, set[tuple[str, str, tuple[object, ...]]]
        ] = {}
        for document in documents:
            if document.role != "REFERENCE":
                continue
            extraction = extractions_by_file.get(document.file_id)
            accepted_reference_keys_by_file[document.file_id] = {
                (fact.source_file_id, fact.field_key, location_key(fact.location))
                for fact in (extraction.facts if extraction is not None else [])
                if (stable_fact_id(fact), fact.source_file_id) in accepted_refs
            }
        mapping_skipped_reference_file_ids: set[str] = set()
        if mapping_enforced:
            for document in documents:
                if document.role != "REFERENCE":
                    continue
                mapping = llm_mappings.get(document.file_id, {})
                mapping_value = mapping.get("value") or {}
                mapping_review = llm_mapping_reviews.get(document.file_id, {})
                mapping_review_value = mapping_review.get("value") or {}
                if mapping.get("status") == "SKIPPED_NO_QUALIFIED_FACTS":
                    mapping_skipped_reference_file_ids.add(document.file_id)
                    uncertain_reference_file_ids.add(document.file_id)
                    continue
                if not mapping_value:
                    uncertain_reference_file_ids.add(document.file_id)
                    warning = ProcessingWarning(
                        code=str(mapping.get("error", "LLM_MAPPING_UNAVAILABLE")),
                        message="跨资料事实映射未完成，需要人工复核。",
                        requires_manual_review=True,
                        file_id=document.file_id,
                    )
                    warnings.append(warning.model_dump(mode="json"))
                    continue
                mapping_obj = FactMappingResponse.model_validate(mapping_value)
                mapping_model = mapping.get("actual_model") or mapping.get("configured_model")
                mapping_review_model = mapping_review.get("actual_model") or mapping_review.get(
                    "configured_model"
                )
                strict_mapping_review = (
                    bool(mapping_review_value)
                    and not diagnostic_mode
                    and bool(
                        mapping_model
                        and mapping_review_model
                        and mapping_model != mapping_review_model
                    )
                )
                if not mapping_review_enforced:
                    accepted_reference_keys = accepted_reference_keys_by_file.get(
                        document.file_id, set()
                    )
                    for proposal in mapping_obj.mappings:
                        reference_key = (
                            proposal.source_file_id,
                            proposal.reference_field_key,
                            location_key(proposal.reference_location),
                        )
                        if (
                            proposal.target_fact_id not in accepted_target_fact_ids
                            or reference_key not in accepted_reference_keys
                        ):
                            raise WorkflowError(
                                "DYNAMIC_CHECK_INCOMPLETE",
                                "映射引用了无法回查的事实，未生成正式报告",
                            )
                        if proposal.confidence < self.settings.LLM_CONSENSUS_MIN_CONFIDENCE:
                            raise WorkflowError(
                                "DYNAMIC_CHECK_INCOMPLETE",
                                "映射结果置信度不足，未生成正式报告",
                                details={"failure_code": "MAPPING_CONFIDENCE_INVALID"},
                            )
                        if proposal.decision == "MATCH":
                            mapping_records.append(
                                {
                                    **proposal.model_dump(mode="json"),
                                    "status": "ACCEPT",
                                }
                            )
                        else:
                            mapping_records.append(
                                {
                                    **proposal.model_dump(mode="json"),
                                    "status": "UNCERTAIN",
                                }
                            )
                    for requirement in mapping_obj.missing_requirements:
                        if requirement.target_fact_id not in accepted_target_fact_ids:
                            raise WorkflowError(
                                "DYNAMIC_CHECK_INCOMPLETE",
                                "缺失要求引用了无法回查的目标事实，未生成正式报告",
                            )
                        if requirement.confidence < self.settings.LLM_CONSENSUS_MIN_CONFIDENCE:
                            raise WorkflowError(
                                "DYNAMIC_CHECK_INCOMPLETE",
                                "缺失要求置信度不足，未生成正式报告",
                                details={"failure_code": "MAPPING_CONFIDENCE_INVALID"},
                            )
                        required_missing.add(
                            (requirement.target_fact_id, document.file_id)
                        )
                elif strict_mapping_review:
                    review_obj = FactMappingReview.model_validate(mapping_review_value)
                    try:
                        validate_mapping_review_coverage(
                            mapping=mapping_obj,
                            review=review_obj,
                            reference_file_id=document.file_id,
                        )
                    except EvidenceValidationError as exc:
                        raise WorkflowError(
                            "DYNAMIC_CHECK_INCOMPLETE",
                            "动态事实映射复核未完整覆盖全部提案，未生成正式报告",
                        ) from exc
                    decisions = {
                        mapping_proposal_key(decision): decision
                        for decision in review_obj.decisions
                    }
                    accepted_reference_keys = accepted_reference_keys_by_file.get(
                        document.file_id, set()
                    )
                    for proposal in mapping_obj.mappings:
                        key = mapping_proposal_key(proposal)
                        reference_key = (
                            proposal.source_file_id,
                            proposal.reference_field_key,
                            location_key(proposal.reference_location),
                        )
                        if (
                            proposal.target_fact_id not in accepted_target_fact_ids
                            or reference_key not in accepted_reference_keys
                        ):
                            raise WorkflowError(
                                "DYNAMIC_CHECK_INCOMPLETE",
                                "映射引用了未通过事实评审的事实，未生成正式报告",
                            )
                        decision = decisions.get(key)
                        accepted = bool(
                            proposal.decision == "MATCH"
                            and decision
                            and decision.decision == "ACCEPT"
                            and proposal.confidence
                            >= self.settings.LLM_CONSENSUS_MIN_CONFIDENCE
                            and decision.confidence
                            >= self.settings.LLM_CONSENSUS_MIN_CONFIDENCE
                            and review_obj.confidence
                            >= self.settings.LLM_CONSENSUS_MIN_CONFIDENCE
                            and review_obj.evidence_complete
                        )
                        if accepted:
                            mapping_records.append(
                                {
                                    **proposal.model_dump(mode="json"),
                                    "status": "ACCEPT",
                                }
                            )
                        else:
                            uncertain_reference_file_ids.add(document.file_id)
                    requirement_decisions = {
                        decision.target_fact_id: decision
                        for decision in review_obj.missing_requirement_decisions
                    }
                    for requirement in mapping_obj.missing_requirements:
                        if requirement.target_fact_id not in accepted_target_fact_ids:
                            raise WorkflowError(
                                "DYNAMIC_CHECK_INCOMPLETE",
                                "缺失要求引用了未通过事实评审的事实，未生成正式报告",
                            )
                        decision = requirement_decisions.get(requirement.target_fact_id)
                        if (
                            decision
                            and decision.decision == "ACCEPT"
                            and requirement.confidence
                            >= self.settings.LLM_CONSENSUS_MIN_CONFIDENCE
                            and decision.confidence
                            >= self.settings.LLM_CONSENSUS_MIN_CONFIDENCE
                            and review_obj.confidence
                            >= self.settings.LLM_CONSENSUS_MIN_CONFIDENCE
                            and review_obj.evidence_complete
                        ):
                            required_missing.add(
                                (requirement.target_fact_id, document.file_id)
                            )
                        elif decision is not None:
                            uncertain_reference_file_ids.add(document.file_id)
                else:
                    uncertain_reference_file_ids.add(document.file_id)
                model_runs.append(
                    {
                        "file_id": document.file_id,
                        "purpose": "FACT_MAPPING",
                        "configured_model": mapping.get("configured_model"),
                        "actual_model": mapping.get("actual_model"),
                        "duration_ms": mapping.get("duration_ms", 0),
                        "request_attempts": mapping.get("request_attempts", 0),
                        "structure_retries": mapping.get("structure_retries", 0),
                        "status": "SUCCEEDED",
                    }
                )
                if mapping_review_value:
                    model_runs.append(
                        {
                            "file_id": document.file_id,
                            "purpose": "FACT_MAPPING_REVIEW",
                            "configured_model": mapping_review.get("configured_model"),
                            "actual_model": mapping_review.get("actual_model"),
                            "duration_ms": mapping_review.get("duration_ms", 0),
                            "request_attempts": mapping_review.get("request_attempts", 0),
                            "structure_retries": mapping_review.get("structure_retries", 0),
                            "status": "SUCCEEDED",
                        }
                    )
            if mapping_skipped_reference_file_ids and not diagnostic_mode:
                raise WorkflowError(
                    "DYNAMIC_CHECK_INCOMPLETE",
                    "动态事实检查没有通过独立评审的合格事实，未生成正式报告",
                )
        existing_warning_codes = {warning["code"] for warning in warnings}
        warnings.extend(
            warning.model_dump(mode="json")
            for warning in template_review.warnings
            if warning.code not in existing_warning_codes
        )
        if not successful_extractions:
            warnings.append(
                ProcessingWarning(
                    code="DRAFT_REVIEW_RULE_BASED_LIMITATION",
                    message="已执行模板确定性检查；尚未形成跨文件事实核对或法律判断。",
                    requires_manual_review=False,
                ).model_dump(mode="json")
            )
        failed_rules = template_review.failed_rule_checks
        target_file_id = next(
            (document.file_id for document in documents if document.role == "TARGET"), None
        )
        reference_file_ids = [
            document.file_id for document in documents if document.role == "REFERENCE"
        ]
        fact_matrix = build_fact_matrix(
            extractions_by_file,
            target_file_id=target_file_id,
            reference_file_ids=reference_file_ids,
            mapping_records=mapping_records if mapping_enforced else None,
            required_missing=required_missing,
            uncertain_reference_file_ids=uncertain_reference_file_ids,
            consensus_fields=consensus_fields if review_enforced else None,
        )
        fact_risks, fact_reviews, fact_passed = fact_matrix_result_items(
            fact_matrix,
            include_conflicts=False,
            # Accepted missing requirements are formal risks. An unaccepted
            # mapping is excluded from formal conclusions: it cannot become a
            # risk or a pass, and does not erase accepted mapping evidence.
            include_uncertain=False,
            include_required_missing=True,
        )
        if mapping_enforced and not mapping_review_enforced and not diagnostic_mode:
            for item in fact_matrix:
                target_candidate = item.get("target_candidate") or {}
                target_evidence = {
                    "file_id": target_candidate.get("source_file_id"),
                    "text": target_candidate.get("evidence_text"),
                    "location": target_candidate.get("location"),
                }
                for relation in item.get("reference_results", []):
                    if relation.get("status") != "UNCERTAIN":
                        continue
                    candidate = relation.get("candidate") or {}
                    if not candidate:
                        continue
                    source_file_id = candidate.get("source_file_id")
                    fact_risks.append(
                        {
                            "risk_id": (
                                f"risk_fact_mapping_{item['target_fact_id']}_"
                                f"{source_file_id}"
                            ),
                            "module_code": "FACT_CONSISTENCY",
                            "risk_type": "ADDITION_OR_CHANGE",
                            "change_type": "SEMANTIC_MAPPING_UNCERTAIN",
                            "title": "辅助资料对应关系不明确",
                            "description": (
                                "目标事实与辅助资料事实的业务对应关系"
                                "无法由模型可靠确认。"
                            ),
                            "source_evidence": [target_evidence, {
                                "file_id": source_file_id,
                                "text": candidate.get("evidence_text"),
                                "location": candidate.get("location"),
                            }],
                            "related_diff_ids": [],
                            "related_rule_ids": [],
                            "requires_manual_action": True,
                        }
                    )
        if review_enforced:
            reviewed_keys = consensus_fields
            mapped_target_fact_ids = {
                str(record["target_fact_id"])
                for record in mapping_records
                if record.get("target_fact_id")
            }
            mapped_reference_keys = {
                (
                    str(record["source_file_id"]),
                    str(record["reference_field_key"]),
                    location_key(record["reference_location"]),
                )
                for record in mapping_records
                if all(
                    record.get(key)
                    for key in (
                        "source_file_id",
                        "reference_field_key",
                        "reference_location",
                    )
                )
            }
            for item in fact_matrix:
                if item.get("target_fact_id") not in mapped_target_fact_ids:
                    continue
                target_candidate = item.get("target_candidate") or {}
                target_key = (
                    target_candidate.get("field_key"),
                    target_candidate.get("source_file_id"),
                    location_key(target_candidate.get("location") or {}),
                )
                if target_key not in reviewed_keys:
                    fact_reviews.append(
                        {
                            "review_id": f"review_fact_consensus_{item['target_fact_id']}",
                            "module_code": "FACT_CONSISTENCY",
                            "reason_code": "FACT_CONSENSUS_UNCERTAIN",
                            "title": f"{item['display_name']}需要双模型复核",
                            "description": "用于跨资料映射的事实未通过独立评审共识门。",
                            "source_evidence": [
                                {
                                    "file_id": target_candidate.get("source_file_id"),
                                    "text": target_candidate.get("evidence_text"),
                                    "location": target_candidate.get("location"),
                                }
                            ],
                            "related_diff_ids": [],
                            "requires_manual_action": True,
                        }
                    )
            for extraction in extractions_by_file.values():
                for fact in extraction.facts:
                    reference_key = (
                        fact.source_file_id,
                        fact.field_key,
                        location_key(fact.location),
                    )
                    fact_key = (fact.field_key, fact.source_file_id, location_key(fact.location))
                    if (
                        reference_key in mapped_reference_keys
                        and fact_key not in reviewed_keys
                    ):
                        fact_reviews.append(
                            {
                                "review_id": (
                                    "review_fact_consensus_"
                                    f"{fact.field_key}_{fact.source_file_id}"
                                ),
                                "module_code": "FACT_CONSISTENCY",
                                "reason_code": "FACT_CONSENSUS_UNCERTAIN",
                                "title": f"{fact.display_name}需要双模型复核",
                                "description": "用于跨资料映射的事实未通过独立评审共识门。",
                                "source_evidence": [
                                    {
                                        "file_id": fact.source_file_id,
                                        "text": fact.evidence_text,
                                        "location": fact.location.model_dump(
                                            mode="json", exclude_none=True
                                        ),
                                    }
                                ],
                                "related_diff_ids": [],
                                "requires_manual_action": True,
                            }
                        )
            for reference_file_id in sorted(uncertain_reference_file_ids):
                if (
                    reference_file_id in mapping_skipped_reference_file_ids
                    and not diagnostic_mode
                ):
                    continue
                fact_reviews.append(
                    {
                        "review_id": f"review_mapping_consensus_{reference_file_id}",
                        "module_code": "FACT_CONSISTENCY",
                        "reason_code": "MAPPING_CONSENSUS_UNCERTAIN",
                        "title": "跨资料映射需要独立复核",
                        "description": "映射或映射复核未形成独立模型共识。",
                        "source_evidence": [{"file_id": reference_file_id}],
                        "related_diff_ids": [],
                        "requires_manual_action": True,
                    }
                )
        if fact_reviews and diagnostic_mode:
            diagnostic_review_items.extend(fact_reviews)
            fact_reviews = []
            fact_risks = []
            fact_passed = []
        if fact_reviews:
            mapped_target_review_gaps = 0
            mapped_reference_review_gaps = 0
            for item in fact_matrix:
                if item.get("target_fact_id") not in mapped_target_fact_ids:
                    continue
                target_candidate = item.get("target_candidate") or {}
                target_key = (
                    target_candidate.get("field_key"),
                    target_candidate.get("source_file_id"),
                    location_key(target_candidate.get("location") or {}),
                )
                mapped_target_review_gaps += target_key not in reviewed_keys
            for extraction in extractions_by_file.values():
                for fact in extraction.facts:
                    reference_key = (
                        fact.source_file_id,
                        fact.field_key,
                        location_key(fact.location),
                    )
                    fact_key = (
                        fact.field_key,
                        fact.source_file_id,
                        location_key(fact.location),
                    )
                    if reference_key in mapped_reference_keys:
                        mapped_reference_review_gaps += fact_key not in reviewed_keys
            raise WorkflowError(
                "DYNAMIC_CHECK_INCOMPLETE",
                "动态事实检查存在无法可靠确认的内容，未生成正式报告",
                details={
                    "failure_code": "FACT_CONSENSUS_INCOMPLETE",
                    "review_item_count": len(fact_reviews),
                    "mapping_record_count": len(mapping_records),
                    "uncertain_reference_file_count": len(uncertain_reference_file_ids),
                    "consensus_field_count": len(consensus_fields),
                    "mapped_target_review_gaps": mapped_target_review_gaps,
                    "mapped_reference_review_gaps": mapped_reference_review_gaps,
                },
            )
        cross_document_diffs = (
            fact_conflict_diff_items(fact_matrix, target_file_id=target_file_id)
            if mapping_enforced and not diagnostic_mode
            else []
        )
        risk_items = build_risk_items(
            template_review.diff_items,
            module_code="TEMPLATE_INTEGRITY",
            failed_rules=failed_rules,
        )
        risk_items.extend(
            build_risk_items(
                cross_document_diffs,
                module_code="FACT_CONSISTENCY",
            )
        )
        risk_items.extend(fact_risks)
        comparison_documents = [
            document for document in documents if document.role in {"TEMPLATE", "TARGET"}
        ]
        passed_checks = build_comparison_passed_checks(
            comparison_documents,
            template_review.diff_items,
            template_review.diagnostics.comparison,
            check_prefix="check_template",
            module_code="TEMPLATE_INTEGRITY",
            content_title="模板固定内容未发现变化",
            numeric_sensitive=True,
        )
        if options.get("check_blank_fields", True) and not failed_rules:
            passed_checks.append(
                {
                    "check_id": "check_required_fields",
                    "module_code": "TEMPLATE_COMPLETENESS",
                    "title": "未发现明确漏填标记",
                    "description": "已执行占位符、空白线和基础表格必填检查。",
                }
            )
        passed_checks.extend(fact_passed)
        numeric_rule_checks: list[dict[str, Any]] = []
        numeric_risks: list[dict[str, Any]] = []
        numeric_reviews: list[dict[str, Any]] = []
        if options.get("check_numeric_consistency", True):
            unique_specs = {}
            spec_definitions: dict[str, dict[str, Any]] = {}
            conflicting_spec_ids: set[str] = set()
            for spec in validation_specs:
                definition = spec.model_dump(
                    mode="json", exclude={"confidence", "evidence_locations"}
                )
                existing = spec_definitions.get(spec.validation_id)
                if existing is not None and existing != definition:
                    conflicting_spec_ids.add(spec.validation_id)
                    continue
                spec_definitions[spec.validation_id] = definition
                unique_specs[spec.validation_id] = spec
            consensus_facts = [
                fact
                for extraction in extractions_by_file.values()
                for fact in extraction.facts
                if not review_enforced
                or (fact.field_key, fact.source_file_id, location_key(fact.location))
                in consensus_fields
            ]
            active_qualified_facts = (
                {
                    ref: fact
                    for ref, fact in qualified_fact_values.items()
                    if not review_enforced
                    or (
                        fact.field_key,
                        fact.source_file_id,
                        location_key(fact.location),
                    )
                    in consensus_fields
                }
                if review_enforced
                else qualified_fact_values
            )
            for spec in unique_specs.values():
                if spec.validation_id in conflicting_spec_ids:
                    numeric_reviews.append(
                        {
                            "review_id": f"review_numeric_{spec.validation_id}",
                            "module_code": "NUMERIC_CONSISTENCY",
                            "reason_code": "NUMERIC_RULE_DEFINITION_CONFLICT",
                            "title": spec.display_name,
                            "description": "不同文件提出了同 ID 但内容不一致的数值规则。",
                            "source_evidence": [],
                            "related_diff_ids": [],
                            "requires_manual_action": True,
                        }
                    )
                    continue
                if (
                    review_enforced
                    and spec.validation_id not in accepted_spec_ids
                    and spec.validation_id not in semantic_plan_spec_ids
                ):
                    numeric_reviews.append(
                        {
                            "review_id": f"review_numeric_{spec.validation_id}",
                            "module_code": "NUMERIC_CONSISTENCY",
                            "reason_code": "NUMERIC_RULE_UNCERTAIN",
                            "title": spec.display_name,
                            "description": "数值规则未通过双模型共识门，需要人工复核。",
                            "source_evidence": [],
                            "related_diff_ids": [],
                            "requires_manual_action": True,
                        }
                    )
                    continue
                try:
                    referenced_fact_refs(spec.expression)
                except Exception:
                    evaluation_facts: Any = consensus_facts
                else:
                    evaluation_facts = active_qualified_facts
                check = evaluate_validation_spec(spec, evaluation_facts)
                if diagnostic_mode:
                    continue
                numeric_rule_checks.append(
                    {
                        "rule_id": spec.validation_id,
                        "rule_name": spec.display_name,
                        "status": check["status"],
                        "location": (
                            {
                                "file_id": next(
                                    (
                                        evidence_ref.source_file_id
                                        for plan in llm_semantic_plans.values()
                                        for internal_spec in (
                                            plan.get("internal_plan").validation_specs
                                            if plan.get("internal_plan") is not None
                                            else []
                                        )
                                        if internal_spec.validation_id == spec.validation_id
                                        for evidence_ref in internal_spec.evidence_refs
                                    ),
                                    None,
                                ),
                                **spec.evidence_locations[0].model_dump(
                                    mode="json", exclude_none=True
                                ),
                            }
                            if spec.evidence_locations
                            else None
                        ),
                        "inputs": {},
                        "message": check["message"],
                    }
                )
                if check["status"] == "FAILED":
                    numeric_risks.append(
                        {
                            "risk_id": f"risk_numeric_{spec.validation_id}",
                            "module_code": "NUMERIC_CONSISTENCY",
                            "risk_type": "ADDITION_OR_CHANGE",
                            "change_type": "NUMERIC_RULE_FAILED",
                            "title": spec.display_name,
                            "description": check["message"],
                            "source_evidence": check.get("source_evidence", []),
                            "related_diff_ids": [],
                            "related_rule_ids": [spec.validation_id],
                            "requires_manual_action": True,
                        }
                    )
                elif check["status"] == "REVIEW_REQUIRED":
                    numeric_reviews.append(
                        {
                            "review_id": f"review_numeric_{spec.validation_id}",
                            "module_code": "NUMERIC_CONSISTENCY",
                            "reason_code": "NUMERIC_RULE_UNCERTAIN",
                            "title": spec.display_name,
                            "description": check["message"],
                            "source_evidence": check.get("source_evidence", []),
                            "related_diff_ids": [],
                            "requires_manual_action": True,
                        }
                    )
                else:
                    passed_checks.append(
                        {
                            "check_id": f"check_numeric_{spec.validation_id}",
                            "module_code": "NUMERIC_CONSISTENCY",
                            "title": spec.display_name,
                            "description": check["message"],
                        }
                    )
        if numeric_reviews and diagnostic_mode:
            diagnostic_review_items.extend(numeric_reviews)
            numeric_reviews = []
            numeric_risks = []
        if numeric_reviews:
            raise WorkflowError(
                "DYNAMIC_CHECK_INCOMPLETE",
                "动态数值检查存在无法可靠执行的规则，未生成正式报告",
                details={
                    "failure_code": "NUMERIC_CONSENSUS_INCOMPLETE",
                    "review_item_count": len(numeric_reviews),
                },
            )
        risk_items.extend(numeric_risks)
        if diagnostic_mode and successful_extractions:
            warnings.append(
                ProcessingWarning(
                    code="LLM_SAME_MODEL_DIAGNOSTIC",
                    message=(
                        "当前模型评审仅用于开发诊断，未形成独立模型共识，不能生成正式通过结论。"
                    ),
                    requires_manual_review=True,
                ).model_dump(mode="json")
            )
        statistics = build_statistics(risk_items, diagnostic_review_items, passed_checks)
        conclusion = (
            "RISK_FOUND"
            if risk_items
            else "REVIEW_REQUIRED"
            if diagnostic_mode and successful_extractions
            else "PASS"
        )
        public_semantic_concepts = [] if (
            diagnostic_mode or not self.settings.LLM_SEMANTIC_PLAN_ENABLED
        ) else [
            concept
            for extraction in extractions_by_file.values()
            for concept in extraction.semantic_concepts
        ]
        public_validation_specs = [] if (
            diagnostic_mode or not self.settings.LLM_SEMANTIC_PLAN_ENABLED
        ) else [
            spec.model_dump(mode="json")
            for spec in {spec.validation_id: spec for spec in validation_specs}.values()
        ]
        result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "task_id": task_id,
            "task_type": TaskType.DRAFT_REVIEW.value,
            "conclusion": conclusion,
            "summary": {
                "title": "起草合同模板确定性检查结果",
                "description": (
                    f"已完成 {len(files)} 份文件检查，确认 {len(risk_items)} 项风险，"
                    f"{len(passed_checks)} 项校验通过。"
                ),
                "statistics": statistics,
            },
            "files": files,
            "risk_items": risk_items,
            "review_items": diagnostic_review_items,
            "passed_checks": passed_checks,
            "diff_items": [
                item.model_dump(mode="json")
                for item in [*template_review.diff_items, *cross_document_diffs]
            ],
            "fact_matrix": fact_matrix,
            "rule_checks": [*template_review.rule_checks, *numeric_rule_checks],
            "warnings": warnings,
            "advice": {
                "overall_advice": "请按证据位置复核固定条款差异和未填写字段。",
                "priority_actions": ["处理固定条款、数值和必填问题"],
                "manual_review_focus": ["模板固定文字、金额期限、占位符和表格必填项"],
                "limitations": [
                    (
                        "跨资料对应由模型识别，并经过原文证据和程序规则校验，不构成法律判断"
                        if successful_extractions and not review_enforced
                        else "模型语义映射和数值规则仅在证据与双模型共识门内生效，不构成法律判断"
                        if successful_extractions
                        else "未执行辅助资料事实抽取、跨文件核对、LLM 或法律判断"
                    )
                ],
            },
            "metadata": {
                "execution_mode": (
                    "HYBRID_DIAGNOSTIC"
                    if successful_extractions and diagnostic_mode
                    else "HYBRID"
                    if successful_extractions
                    else "RULE_BASED"
                ),
                "workflow_version": DRAFT_REVIEW_WORKFLOW_VERSION,
                "rules_version": DRAFT_REVIEW_RULES_VERSION,
                "primary_model": next(
                    (run.get("actual_model") or run.get("configured_model") for run in model_runs),
                    None,
                ),
                "model_runs": model_runs,
                "independent_review": bool(
                    successful_extractions and review_enforced and not diagnostic_mode
                ),
                "review_mode": (
                    "SAME_MODEL_DIAGNOSTIC"
                    if successful_extractions and diagnostic_mode
                    else "INDEPENDENT_MODEL"
                    if successful_extractions and review_enforced
                    else "NOT_RUN"
                ),
                "reviewed_files": sorted(strict_review_files),
                "semantic_concepts": public_semantic_concepts,
                "validation_specs": public_validation_specs,
                "comparison_diagnostics": template_review.diagnostics.comparison.model_dump(
                    mode="json"
                ),
                "template_diagnostics": template_review.diagnostics.model_dump(mode="json"),
            },
            "mock": False,
        }
        ensure_fallback_risk_advices(result)
        return result

    async def run(
        self,
        *,
        task_id: str,
        task_type: TaskType,
        files: list[dict[str, Any]],
        options: dict[str, Any],
        progress_callback: ProgressCallback,
    ) -> WorkflowOutput:
        if task_type != TaskType.DRAFT_REVIEW:
            raise WorkflowError("PARSE_FAILED", "起草检查工作流仅支持 DRAFT_REVIEW")
        roles = [item["role"] for item in files]
        if roles.count("TARGET") != 1 or roles.count("TEMPLATE") != 1 or "REFERENCE" not in roles:
            raise WorkflowError("PARSE_FAILED", "起草检查文件角色不完整")
        async with TaskWorkspace(self.settings.TEMP_ROOT, task_id) as workspace:
            graph = self._build_graph(workspace, progress_callback)
            state = await graph.ainvoke(
                DraftReviewState(task_id=task_id, files=files, options=options)
            )
            documents = state["parsed_documents"]
            local_by_id = {file.file_id: file for file in state["local_files"]}
            page_counts = {
                file_id: sidecar.page_count
                for file_id, sidecar in state.get("page_location_sidecars", {}).items()
            }
            metadata = [
                {
                    "file_id": document.file_id,
                    "detected_mime_type": local_by_id[document.file_id].detected_mime_type,
                    "file_size": local_by_id[document.file_id].file_size,
                    "sha256": document.sha256,
                    "page_count": page_counts.get(document.file_id, document.page_count),
                    "parser_name": document.parser_name,
                    "parse_status": self._parse_status(document),
                    "parse_warnings": [
                        warning.model_dump(mode="json") for warning in document.warnings
                    ],
                }
                for document in documents
            ]
            return WorkflowOutput(result=state["result"], file_metadata=metadata)
