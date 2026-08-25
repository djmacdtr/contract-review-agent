from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.adapters.document_parser.textin_parser import TextInDocumentParser
from app.adapters.llm.base import ContractLlmClient
from app.adapters.llm.openai_client import LlmClientError, OpenAIContractLlmClient
from app.adapters.llm.schemas import (
    AdviceResponse,
    DocumentFactExtraction,
    FactCandidate,
    FactMappingResponse,
    FactMappingReview,
    FactReview,
    SemanticPlanResponse,
)
from app.core.config import Settings
from app.core.enums import TaskStage, TaskType
from app.core.errors import WorkflowError
from app.documents.models import ParsedDocument, ProcessingWarning
from app.documents.parsers import ParserRegistry
from app.documents.router import DocumentParsingRouter
from app.draft_review.facts import (
    MAX_NUMERIC_CANDIDATES_PER_CHUNK,
    EvidenceValidationError,
    accepted_fact_refs,
    build_fact_index,
    build_fact_matrix,
    build_fact_review_batches,
    chunk_document,
    compact_extraction_payload,
    fact_conflict_diff_items,
    fact_index_payload,
    fact_matrix_result_items,
    location_key,
    merge_chunk_extractions,
    merge_fact_review_batches,
    project_semantic_plan,
    stable_fact_id,
    target_fact_catalog,
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
from app.services.downloader import LocalFile, SafeFileDownloadService
from app.services.temp_files import TaskWorkspace
from app.workflows.mock_graphs import ProgressCallback
from app.workflows.types import WorkflowOutput

DRAFT_REVIEW_WORKFLOW_VERSION = "0.7.0"
DRAFT_REVIEW_RULES_VERSION = "0.6.0"


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
    ) -> None:
        self.settings = settings
        self.downloader = downloader or SafeFileDownloadService(settings)
        local_parsers = parsers or ParserRegistry(
            pdf_min_text_chars_per_page=settings.PDF_MIN_TEXT_CHARS_PER_PAGE
        )
        self.parsers = document_router or DocumentParsingRouter(
            local=local_parsers,
            external=TextInDocumentParser(settings) if settings.OCR_ENABLED else None,
        )
        if llm is not None:
            self.llm = llm
        elif settings.llm_configured:
            self.llm = OpenAIContractLlmClient(settings)
        else:
            self.llm = None

    def _build_graph(self, workspace: TaskWorkspace, callback: ProgressCallback):
        graph = StateGraph(DraftReviewState)

        async def download_files(state: DraftReviewState) -> dict[str, Any]:
            await callback(TaskStage.DOWNLOADING, 10, "正在受控下载起草检查文件")
            return {"local_files": await self.downloader.prepare(state["files"], workspace)}

        async def parse_documents(state: DraftReviewState) -> dict[str, Any]:
            await callback(TaskStage.PARSING, 35, "正在逐份解析目标、模板和辅助资料")
            return {"parsed_documents": await self.parsers.parse_draft_review(state["local_files"])}

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
            if review.diagnostics.expanded_table_count:
                raise WorkflowError(
                    "COMPARISON_INCOMPLETE",
                    "目标合同与模板存在无法可靠完成逐项检查的表格结构变化",
                )
            return {"template_review": review}

        async def extract_facts(state: DraftReviewState) -> dict[str, Any]:
            if self.llm is None:
                return {"llm_extractions": {}}
            await callback(TaskStage.FACT_EXTRACTION, 75, "正在逐份抽取合同事实并保留证据")
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
                    for blocks in chunk_document(
                        document,
                        self.settings.LLM_CHUNK_MAX_CHARS,
                        max_numeric_candidates=MAX_NUMERIC_CANDIDATES_PER_CHUNK,
                    ):
                        extraction = await self.llm.extract_facts(
                            compact_extraction_payload(document, blocks)
                        )
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
                    }
                    review_method = getattr(self.llm, "review_facts", None)
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
            catalog = target_fact_catalog(target_extraction)
            target_ids = {item["target_fact_id"] for item in catalog}
            await callback(TaskStage.FACT_EXTRACTION, 80, "正在逐份映射目标合同与辅助资料事实")
            for document in state["parsed_documents"]:
                if document.role != "REFERENCE":
                    continue
                reference_value = state.get("llm_extractions", {}).get(document.file_id, {}).get(
                    "value"
                )
                if not reference_value:
                    continue
                reference_extraction = DocumentFactExtraction.model_validate(reference_value)
                reference_index = {
                    (fact.field_key, location_key(fact.location)): fact
                    for fact in reference_extraction.facts
                }
                payload = {
                    "reference_file_id": document.file_id,
                    "reference_profile": reference_extraction.profile.model_dump(mode="json"),
                    "target_facts": catalog,
                    "reference_facts": [
                        fact.model_dump(mode="json") for fact in reference_extraction.facts
                    ],
                }
                try:
                    mapping_result = await self.llm.map_facts(payload)
                    mapping = FactMappingResponse.model_validate(mapping_result.value)
                    if mapping.reference_file_id != document.file_id:
                        raise EvidenceValidationError("mapping reference file does not match")
                    proposed_keys = set()
                    for proposal in mapping.mappings:
                        key = (
                            proposal.target_fact_id,
                            proposal.reference_field_key,
                            proposal.source_file_id,
                            location_key(proposal.reference_location),
                        )
                        if proposal.target_fact_id not in target_ids:
                            raise EvidenceValidationError("mapping target fact does not exist")
                        if proposal.source_file_id != document.file_id:
                            raise EvidenceValidationError("mapping source file does not match")
                        if (
                            proposal.reference_field_key,
                            location_key(proposal.reference_location),
                        ) not in reference_index:
                            raise EvidenceValidationError("mapping reference fact does not exist")
                        if key in proposed_keys:
                            raise EvidenceValidationError("mapping contains duplicate proposal")
                        proposed_keys.add(key)
                    requirement_ids = {
                        requirement.target_fact_id for requirement in mapping.missing_requirements
                    }
                    if not requirement_ids <= target_ids:
                        raise EvidenceValidationError("missing requirement target does not exist")
                    mappings[document.file_id] = {
                        "value": mapping.model_dump(mode="json"),
                        "configured_model": mapping_result.configured_model,
                        "actual_model": mapping_result.actual_model,
                        "duration_ms": mapping_result.duration_ms,
                        "request_attempts": mapping_result.request_attempts,
                        "structure_retries": mapping_result.structure_retries,
                    }
                    if not hasattr(self.llm, "review_mappings"):
                        continue
                    review_result = await self.llm.review_mappings(
                        {**payload, "proposed_mapping": mapping.model_dump(mode="json")}
                    )
                    review = FactMappingReview.model_validate(review_result.value)
                    if review.reference_file_id != document.file_id:
                        raise EvidenceValidationError(
                            "mapping review reference file does not match"
                        )
                    for decision in review.decisions:
                        key = (
                            decision.target_fact_id,
                            decision.reference_field_key,
                            decision.source_file_id,
                            location_key(decision.reference_location),
                        )
                        if key not in proposed_keys:
                            raise EvidenceValidationError(
                                "mapping review decision does not match proposal"
                            )
                    for decision in review.missing_requirement_decisions:
                        if decision.target_fact_id not in requirement_ids:
                            raise EvidenceValidationError(
                                "missing requirement review does not match proposal"
                            )
                    mapping_reviews[document.file_id] = {
                        "value": review.model_dump(mode="json"),
                        "configured_model": review_result.configured_model,
                        "actual_model": review_result.actual_model,
                        "duration_ms": review_result.duration_ms,
                        "request_attempts": review_result.request_attempts,
                        "structure_retries": review_result.structure_retries,
                    }
                except (
                    LlmClientError,
                    EvidenceValidationError,
                    ValidationError,
                    TimeoutError,
                ) as exc:
                    raise WorkflowError(
                        "DYNAMIC_CHECK_INCOMPLETE",
                        f"文件 {document.file_name} 的跨资料事实映射未能可靠完成",
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
                    accepted_fact_refs(
                        extraction,
                        reviews_by_file.get(file_id),
                        self.settings.LLM_CONSENSUS_MIN_CONFIDENCE,
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
                result = await self.llm.plan_semantics(payload)
                plan = SemanticPlanResponse.model_validate(result.value)
                validate_semantic_plan(
                    primary_file_id=primary.file_id,
                    documents_by_file=documents_by_file,
                    plan=plan,
                    fact_index=fact_index,
                    accepted_refs=accepted_refs,
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
                        "configured_model": result.configured_model,
                        "actual_model": result.actual_model,
                        "duration_ms": result.duration_ms,
                        "request_attempts": result.request_attempts,
                        "structure_retries": result.structure_retries,
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
                merge_model_advice(result, AdviceResponse.model_validate(generated.value))
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
            return {}

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
        graph.add_edge("map_cross_document_facts", "plan_semantics")
        graph.add_edge("plan_semantics", "build_result")
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
        if template_review.diagnostics.expanded_table_count:
            raise WorkflowError(
                "UNSUPPORTED_TABLE_EXPANSION",
                "目标合同包含无法可靠检查的扩展表格，未生成正式报告",
            )
        input_by_id = {item["file_id"]: item for item in input_files}
        files: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        llm_reviews = llm_reviews or {}
        llm_mappings = llm_mappings or {}
        llm_mapping_reviews = llm_mapping_reviews or {}
        llm_semantic_plans = llm_semantic_plans or {}
        options = options or {}
        review_enforced = self.llm is not None and hasattr(self.llm, "review_facts")
        mapping_enforced = self.llm is not None and hasattr(self.llm, "map_facts")
        mapping_review_enforced = self.llm is not None and hasattr(
            self.llm, "review_mappings"
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
                validation_specs.extend(extractions_by_file[document.file_id].validation_specs)
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
                accepted_fact_refs(
                    extraction,
                    review_obj,
                    self.settings.LLM_CONSENSUS_MIN_CONFIDENCE,
                )
            )
        qualified_fact_values = {
            ref: entry.fact for ref, entry in fact_index.items() if ref in accepted_refs
        }
        if mapping_enforced:
            for document in documents:
                if document.role != "REFERENCE":
                    continue
                mapping = llm_mappings.get(document.file_id, {})
                mapping_value = mapping.get("value") or {}
                mapping_review = llm_mapping_reviews.get(document.file_id, {})
                mapping_review_value = mapping_review.get("value") or {}
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
                if strict_mapping_review:
                    review_obj = FactMappingReview.model_validate(mapping_review_value)
                    decisions = {
                        (
                            decision.target_fact_id,
                            decision.reference_field_key,
                            decision.source_file_id,
                            location_key(decision.reference_location),
                        ): decision
                        for decision in review_obj.decisions
                    }
                    for proposal in mapping_obj.mappings:
                        key = (
                            proposal.target_fact_id,
                            proposal.reference_field_key,
                            proposal.source_file_id,
                            location_key(proposal.reference_location),
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
                        mapping_records.append(
                            {
                                **proposal.model_dump(mode="json"),
                                "status": "ACCEPT" if accepted else "UNCERTAIN",
                            }
                        )
                    requirement_decisions = {
                        decision.target_fact_id: decision
                        for decision in review_obj.missing_requirement_decisions
                    }
                    for requirement in mapping_obj.missing_requirements:
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
            include_uncertain=False,
        )
        if review_enforced:
            reviewed_keys = consensus_fields
            target_ids = {
                document.file_id for document in documents if document.role == "TARGET"
            }
            for file_id, extraction in extractions_by_file.items():
                if file_id not in target_ids:
                    continue
                for fact in extraction.facts:
                    key = (fact.field_key, fact.source_file_id, location_key(fact.location))
                    if key not in reviewed_keys:
                        fact_reviews.append(
                            {
                                "review_id": (
                                    f"review_fact_consensus_{fact.field_key}_{fact.source_file_id}"
                                ),
                                "module_code": "FACT_CONSISTENCY",
                                "reason_code": "FACT_CONSENSUS_UNCERTAIN",
                                "title": f"{fact.display_name}需要双模型复核",
                                "description": (
                                    "主模型事实未通过独立评审共识门，不影响自动风险或通过。"
                                ),
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
        if fact_reviews and diagnostic_mode:
            diagnostic_review_items.extend(fact_reviews)
            fact_reviews = []
            fact_risks = []
            fact_passed = []
        if fact_reviews:
            raise WorkflowError(
                "DYNAMIC_CHECK_INCOMPLETE",
                "动态事实检查存在无法可靠确认的内容，未生成正式报告",
            )
        cross_document_diffs = (
            fact_conflict_diff_items(fact_matrix, target_file_id=target_file_id)
            if review_enforced and mapping_enforced and not diagnostic_mode
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
        public_semantic_concepts = [] if diagnostic_mode else [
            concept
            for extraction in extractions_by_file.values()
            for concept in extraction.semantic_concepts
        ]
        public_validation_specs = [] if diagnostic_mode else [
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
                    "模型语义映射和数值规则仅在证据与双模型共识门内生效，不构成法律判断"
                    if successful_extractions
                    else "未执行辅助资料事实抽取、跨文件核对、LLM 或法律判断"
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
            metadata = [
                {
                    "file_id": document.file_id,
                    "detected_mime_type": local_by_id[document.file_id].detected_mime_type,
                    "file_size": local_by_id[document.file_id].file_size,
                    "sha256": document.sha256,
                    "page_count": document.page_count,
                    "parser_name": document.parser_name,
                    "parse_status": self._parse_status(document),
                    "parse_warnings": [
                        warning.model_dump(mode="json") for warning in document.warnings
                    ],
                }
                for document in documents
            ]
            return WorkflowOutput(result=state["result"], file_metadata=metadata)
