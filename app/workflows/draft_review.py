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
    FactMappingResponse,
    FactMappingReview,
    FactReview,
)
from app.core.config import Settings
from app.core.enums import TaskStage, TaskType
from app.core.errors import WorkflowError
from app.documents.models import ParsedDocument, ProcessingWarning
from app.documents.normalization import normalize_text
from app.documents.parsers import ParserRegistry
from app.documents.router import DocumentParsingRouter
from app.draft_review.facts import (
    EvidenceValidationError,
    build_fact_matrix,
    chunk_document,
    chunk_payload,
    evidence_location_exists,
    fact_matrix_result_items,
    location_key,
    merge_chunk_extractions,
    target_fact_catalog,
)
from app.draft_review.numeric_rules import evaluate_validation_spec
from app.draft_review.template_checks import TemplateReviewResult, analyze_template
from app.results.risk_model import build_review_items, build_risk_items, build_statistics
from app.schemas.results import RESULT_SCHEMA_VERSION
from app.services.downloader import LocalFile, SafeFileDownloadService
from app.services.temp_files import TaskWorkspace
from app.workflows.mock_graphs import ProgressCallback
from app.workflows.types import WorkflowOutput

DRAFT_REVIEW_WORKFLOW_VERSION = "0.5.1"
DRAFT_REVIEW_RULES_VERSION = "0.4.1"


def _refresh_result_status(result: dict[str, Any]) -> None:
    """Keep late-stage review additions reflected in conclusion and statistics."""
    risk_items = result.get("risk_items", [])
    review_items = result.get("review_items", [])
    passed_checks = result.get("passed_checks", [])
    result["summary"]["statistics"] = build_statistics(
        risk_items, review_items, passed_checks
    )
    result["summary"]["description"] = (
        f"已解析 {len(result.get('files', []))} 份文件，确认 {len(risk_items)} 项风险，"
        f"另有 {len(review_items)} 项需要人工复核。"
    )
    if risk_items:
        result["conclusion"] = "RISK_FOUND"
    elif review_items:
        result["conclusion"] = "REVIEW_REQUIRED"
    else:
        result["conclusion"] = "PASS"


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
            return {
                "template_review": analyze_template(
                    by_role["TEMPLATE"],
                    by_role["TARGET"],
                    ignore_formatting=options.get("ignore_formatting", True),
                    ignore_headers_footers=options.get("ignore_headers_footers", True),
                    check_blank_fields=options.get("check_blank_fields", True),
                    ocr_low_confidence_threshold=self.settings.OCR_LOW_CONFIDENCE_THRESHOLD,
                )
            }

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
                    for blocks in chunk_document(document, self.settings.LLM_CHUNK_MAX_CHARS):
                        extraction = await self.llm.extract_facts(chunk_payload(document, blocks))
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
                        review_payload = {
                            "file_id": document.file_id,
                            "role": document.role,
                            "facts": merged.model_dump(mode="json")["facts"],
                            "semantic_concepts": merged.model_dump(mode="json")[
                                "semantic_concepts"
                            ],
                            "validation_specs": merged.model_dump(mode="json")["validation_specs"],
                        }
                        review_result = await review_method(review_payload)
                        review = FactReview.model_validate(review_result.value)
                        if review.file_id != document.file_id:
                            raise EvidenceValidationError(
                                "review file_id does not match parsed document"
                            )
                        fact_index = {
                            (fact.field_key, fact.source_file_id, location_key(fact.location)): fact
                            for fact in merged.facts
                        }
                        for decision in review.decisions:
                            key = (
                                decision.field_key,
                                decision.source_file_id,
                                location_key(decision.location),
                            )
                            fact = fact_index.get(key)
                            if fact is None:
                                raise EvidenceValidationError(
                                    "review decision does not match a candidate fact"
                                )
                            if decision.evidence_text and normalize_text(
                                decision.evidence_text
                            ) not in normalize_text(fact.evidence_text):
                                raise EvidenceValidationError(
                                    "review evidence does not match candidate evidence"
                                )
                        for concept in review.semantic_concepts:
                            for location in concept.evidence_locations:
                                if not evidence_location_exists(document, location):
                                    raise EvidenceValidationError(
                                        "review concept location does not exist"
                                    )
                        for spec in review.validation_specs:
                            for location in spec.evidence_locations:
                                if not evidence_location_exists(document, location):
                                    raise EvidenceValidationError(
                                        "review validation location does not exist"
                                    )
                        reviews[document.file_id] = {
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
                    if document.file_id not in extractions:
                        extractions[document.file_id] = {
                            "error": getattr(exc, "code", "LLM_EVIDENCE_INVALID"),
                            "message": "辅助资料事实抽取未完成，需要人工复核。",
                        }
                    reviews[document.file_id] = {
                        "error": getattr(exc, "code", "LLM_REVIEW_INVALID"),
                        "message": "独立事实评审未完成，需要人工复核。",
                    }
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
                    if document.file_id not in mappings:
                        mappings[document.file_id] = {
                            "error": getattr(exc, "code", "LLM_MAPPING_INVALID"),
                            "message": "跨资料事实映射未完成，需要人工复核。",
                        }
                    mapping_reviews[document.file_id] = {
                        "error": getattr(exc, "code", "LLM_MAPPING_REVIEW_INVALID"),
                        "message": "独立映射评审未完成，需要人工复核。",
                    }
            return {"llm_mappings": mappings, "llm_mapping_reviews": mapping_reviews}

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
                state.get("options", {}),
            )
            return {"result": result}

        async def generate_advice(state: DraftReviewState) -> dict[str, Any]:
            result = state["result"]
            if self.llm is None or not hasattr(self.llm, "generate_advice"):
                return {}
            await callback(TaskStage.GENERATING_ADVICE, 92, "正在根据已有证据生成建议")
            evidence_refs = []
            for item in result.get("risk_items", []) + result.get("review_items", []):
                evidence_refs.extend(
                    {"file_id": evidence.get("file_id"), "location": evidence.get("location")}
                    for evidence in item.get("source_evidence", [])
                    if evidence.get("file_id") and evidence.get("location")
                )
            payload = {
                "risk_items": result.get("risk_items", []),
                "review_items": result.get("review_items", []),
                "passed_checks": result.get("passed_checks", []),
                "fact_matrix": result.get("fact_matrix", []),
                "rule_checks": result.get("rule_checks", []),
                "evidence_refs": evidence_refs,
            }
            try:
                advice = AdviceResponse.model_validate(
                    (await self.llm.generate_advice(payload)).value
                ).model_dump(mode="json")
                valid = {(ref["file_id"], location_key(ref["location"])) for ref in evidence_refs}
                raw_refs = advice.pop("evidence_refs", [])
                filtered = []
                for ref in raw_refs:
                    if (ref["file_id"], location_key(ref["location"])) in valid:
                        filtered.append(ref)
                advice["evidence_refs"] = filtered
                if len(filtered) != len(raw_refs):
                    advice.setdefault("limitations", []).append(
                        "部分建议证据引用未能回查，已移除。"
                    )
                    result.setdefault("warnings", []).append(
                        {
                            "code": "LLM_ADVICE_EVIDENCE_REVIEW_REQUIRED",
                            "message": "部分模型建议证据引用无法回查，需要人工复核。",
                            "requires_manual_review": True,
                        }
                    )
                    result.setdefault("review_items", []).append(
                        {
                            "review_id": "review_llm_advice_evidence",
                            "module_code": "LLM_ADVICE",
                            "reason_code": "LLM_ADVICE_EVIDENCE_UNVERIFIED",
                            "title": "模型建议证据需要人工复核",
                            "description": "部分建议引用不属于已生成的风险或复核证据，已过滤。",
                            "source_evidence": [],
                            "related_diff_ids": [],
                            "requires_manual_action": True,
                        }
                    )
                result["advice"] = advice
                _refresh_result_status(result)
            except (LlmClientError, ValidationError, ValueError, AssertionError):
                result.setdefault("warnings", []).append(
                    {
                        "code": "LLM_ADVICE_REVIEW_REQUIRED",
                        "message": "模型建议未完成，需要人工复核已有证据。",
                        "requires_manual_review": True,
                    }
                )
                result.setdefault("review_items", []).append(
                    {
                        "review_id": "review_llm_advice",
                        "module_code": "LLM_ADVICE",
                        "reason_code": "LLM_ADVICE_UNAVAILABLE",
                        "title": "模型建议不可用",
                        "description": "建议生成失败不影响确定性检查结果。",
                        "source_evidence": [],
                        "related_diff_ids": [],
                        "requires_manual_action": True,
                    }
                )
                result["summary"]["statistics"] = build_statistics(
                    result["risk_items"], result["review_items"], result["passed_checks"]
                )
                _refresh_result_status(result)
            return {"result": result}

        async def persist_result(state: DraftReviewState) -> dict[str, Any]:
            await callback(TaskStage.PERSISTING_RESULT, 97, "正在保存多文档解析结果")
            return {}

        graph.add_node("download_files", download_files)
        graph.add_node("parse_documents", parse_documents)
        graph.add_node("compare_template", compare_template)
        graph.add_node("extract_facts", extract_facts)
        graph.add_node("map_cross_document_facts", map_cross_document_facts)
        graph.add_node("build_result", build_result)
        graph.add_node("generate_advice", generate_advice)
        graph.add_node("persist_result", persist_result)
        graph.add_edge(START, "download_files")
        graph.add_edge("download_files", "parse_documents")
        graph.add_edge("parse_documents", "compare_template")
        graph.add_edge("compare_template", "extract_facts")
        graph.add_edge("extract_facts", "map_cross_document_facts")
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
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        input_by_id = {item["file_id"]: item for item in input_files}
        files: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        review_warnings: list[ProcessingWarning] = []
        llm_reviews = llm_reviews or {}
        llm_mappings = llm_mappings or {}
        llm_mapping_reviews = llm_mapping_reviews or {}
        options = options or {}
        review_enforced = self.llm is not None and hasattr(self.llm, "review_facts")
        extractions_by_file: dict[str, DocumentFactExtraction] = {}
        consensus_fields: set[tuple[str, str, tuple[object, ...]]] = set()
        strict_review_files: set[str] = set()
        validation_specs = []
        accepted_spec_ids: set[str] = set()
        mapping_records: list[dict[str, Any]] = []
        required_missing: set[tuple[str, str]] = set()
        uncertain_reference_file_ids: set[str] = set()
        model_runs: list[dict[str, Any]] = []
        successful_extractions = 0
        for document in documents:
            document_warnings = [warning.model_dump(mode="json") for warning in document.warnings]
            for warning in document_warnings:
                warning["file_id"] = warning.get("file_id") or document.file_id
                warnings.append(warning)
            review_warnings.extend(document.warnings)
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
                strict_review = bool(review_value) and (
                    not self.settings.LLM_REQUIRE_INDEPENDENT_MODEL
                    or (extraction_model and review_model and extraction_model != review_model)
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
                elif review.get("error"):
                    review_warning = ProcessingWarning(
                        code=str(review["error"]),
                        message="独立事实评审未完成，需要人工复核。",
                        requires_manual_review=True,
                        file_id=document.file_id,
                    )
                    review_warnings.append(review_warning)
                    warnings.append(review_warning.model_dump(mode="json"))
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
                            "status": "SUCCEEDED",
                        }
                    )
            elif extraction.get("error"):
                llm_warning = ProcessingWarning(
                    code=str(extraction["error"]),
                    message="辅助资料事实抽取未完成，需要人工复核。",
                    requires_manual_review=True,
                    file_id=document.file_id,
                )
                review_warnings.append(llm_warning)
                warnings.append(llm_warning.model_dump(mode="json"))
                profile = {
                    "document_kind": "UNKNOWN",
                    "title": None,
                    "confidence": 0.0,
                    "generated_by": "FAILED",
                    "evidence_locations": [],
                }
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
        mapping_enforced = self.llm is not None and hasattr(self.llm, "map_facts")
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
                strict_mapping_review = bool(mapping_review_value) and (
                    not self.settings.LLM_REQUIRE_INDEPENDENT_MODEL
                    or (
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
        review_warnings.extend(template_review.warnings)
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
        fact_risks, fact_reviews, fact_passed = fact_matrix_result_items(fact_matrix)
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
        risk_items = build_risk_items(
            template_review.diff_items,
            module_code="TEMPLATE_INTEGRITY",
            failed_rules=failed_rules,
        )
        review_items = build_review_items(
            template_review.diff_items,
            review_warnings,
            module_code="TEMPLATE_RELIABILITY",
        )
        risk_items.extend(fact_risks)
        review_items.extend(fact_reviews)
        passed_checks = []
        if template_review.diagnostics.comparison.reliable:
            passed_checks.append(
                {
                    "check_id": "check_template_alignment",
                    "module_code": "TEMPLATE_INTEGRITY",
                    "title": "模板正文对齐可靠",
                    "description": "目标合同和模板正文达到确定性对齐阈值。",
                }
            )
        if not failed_rules:
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
                if review_enforced and spec.validation_id not in accepted_spec_ids:
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
                check = evaluate_validation_spec(spec, consensus_facts)
                numeric_rule_checks.append(
                    {
                        "rule_id": spec.validation_id,
                        "rule_name": spec.display_name,
                        "status": check["status"],
                        "location": spec.evidence_locations[0].model_dump(
                            mode="json", exclude_none=True
                        )
                        if spec.evidence_locations
                        else None,
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
        numeric_risks and risk_items.extend(numeric_risks)
        numeric_reviews and review_items.extend(numeric_reviews)
        statistics = build_statistics(risk_items, review_items, passed_checks)
        if risk_items:
            conclusion = "RISK_FOUND"
        elif review_items:
            conclusion = "REVIEW_REQUIRED"
        else:
            conclusion = "PASS"
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "task_id": task_id,
            "task_type": TaskType.DRAFT_REVIEW.value,
            "conclusion": conclusion,
            "summary": {
                "title": "起草合同模板确定性检查结果",
                "description": (
                    f"已解析 {len(files)} 份文件，确认 {len(risk_items)} 项风险，"
                    f"另有 {len(review_items)} 项需要人工复核。"
                ),
                "statistics": statistics,
            },
            "files": files,
            "risk_items": risk_items,
            "review_items": review_items,
            "passed_checks": passed_checks,
            "diff_items": [item.model_dump(mode="json") for item in template_review.diff_items],
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
                "execution_mode": "HYBRID" if successful_extractions else "RULE_BASED",
                "workflow_version": DRAFT_REVIEW_WORKFLOW_VERSION,
                "rules_version": DRAFT_REVIEW_RULES_VERSION,
                "primary_model": next(
                    (run.get("actual_model") or run.get("configured_model") for run in model_runs),
                    None,
                ),
                "model_runs": model_runs,
                "reviewed_files": sorted(strict_review_files),
                "semantic_concepts": [
                    concept
                    for extraction in extractions_by_file.values()
                    for concept in extraction.semantic_concepts
                ],
                "validation_specs": [
                    spec.model_dump(mode="json")
                    for spec in {spec.validation_id: spec for spec in validation_specs}.values()
                ],
                "comparison_diagnostics": template_review.diagnostics.comparison.model_dump(
                    mode="json"
                ),
                "template_diagnostics": template_review.diagnostics.model_dump(mode="json"),
            },
            "mock": False,
        }

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
