from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.adapters.document_parser.cached_parser import (
    CachedExternalDocumentParser,
    SqlAlchemyDocumentParseCache,
)
from app.adapters.document_parser.textin_parser import TextInDocumentParser
from app.adapters.llm.base import ContractLlmClient
from app.adapters.llm.openai_client import OpenAIContractLlmClient
from app.comparison.duplicate_clusters import (
    apply_deterministic_final_compare_filters,
    validate_final_compare_duplicate_clusters,
)
from app.comparison.engine import (
    CompareOptions,
    compare_documents,
)
from app.comparison.models import ComparisonResult
from app.core.config import Settings
from app.core.enums import TaskStage, TaskType
from app.core.errors import WorkflowError
from app.db.session import SessionFactory
from app.documents.models import ParsedDocument, ProcessingWarning
from app.documents.page_locations import (
    apply_docx_page_location_sidecars,
    validate_public_page_coverage,
)
from app.documents.parsers import ParserRegistry
from app.documents.router import DocumentParsingRouter
from app.results.advice import (
    ensure_fallback_risk_advices,
)
from app.results.advice_batches import generate_advice_in_batches
from app.results.passed_checks import build_comparison_passed_checks
from app.results.risk_model import (
    build_comparison_review_items,
    build_risk_items,
    build_statistics,
)
from app.schemas.results import RESULT_SCHEMA_VERSION
from app.services.downloader import DOCX_MIME, LocalFile, SafeFileDownloadService
from app.services.temp_files import TaskWorkspace
from app.workflows.mock_graphs import ProgressCallback
from app.workflows.types import WorkflowOutput

FINAL_COMPARE_WORKFLOW_VERSION = "0.7.0"
FINAL_COMPARE_RULES_VERSION = "0.7.0"
FINAL_COMPARE_LEGACY_WORKFLOW_VERSION = "0.6.0"
FINAL_COMPARE_LEGACY_RULES_VERSION = "0.6.0"


class FinalCompareState(TypedDict, total=False):
    task_id: str
    files: list[dict[str, Any]]
    options: dict[str, Any]
    local_files: list[LocalFile]
    parsed_documents: list[ParsedDocument]
    comparison: ComparisonResult
    page_location_sidecars: dict[str, Any]
    result: dict[str, Any]


class FinalCompareWorkflowExecutor:
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
            external=CachedExternalDocumentParser(
                TextInDocumentParser(settings),
                SqlAlchemyDocumentParseCache(SessionFactory),
                settings,
            )
            if settings.OCR_ENABLED or settings.DOCX_PAGE_LOCATION_ENABLED
            else None,
            docx_page_location_enabled=settings.DOCX_PAGE_LOCATION_ENABLED,
        )
        if llm is not None:
            self.llm = llm
        elif settings.llm_configured:
            self.llm = OpenAIContractLlmClient(
                settings,
                advice_response_format_override="json_object",
            )
        else:
            self.llm = None

    def _build_graph(self, workspace: TaskWorkspace, callback: ProgressCallback):
        graph = StateGraph(FinalCompareState)

        async def download_files(state: FinalCompareState) -> dict[str, Any]:
            await callback(TaskStage.DOWNLOADING, 10, "正在受控下载两个文件")
            return {"local_files": await self.downloader.prepare(state["files"], workspace)}

        async def parse_documents(state: FinalCompareState) -> dict[str, Any]:
            await callback(TaskStage.PARSING, 35, "正在解析 DOCX、文本型 PDF 或扫描 PDF")
            parsed = await self.parsers.parse_final_compare(state["local_files"])
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

        async def compare_versions(state: FinalCompareState) -> dict[str, Any]:
            await callback(TaskStage.VERSION_COMPARE, 68, "正在对齐条款、文字和基础表格")
            by_role = {document.role: document for document in state["parsed_documents"]}
            if "BASELINE" not in by_role or "TARGET" not in by_role:
                raise WorkflowError("COMPARISON_FAILED", "比对任务缺少基准文件或目标文件")
            raw_options = state.get("options", {})
            comparison_mode = (
                "FINAL_LOGICAL_V2"
                if self.settings.FINAL_COMPARE_LOGICAL_V2_ENABLED
                else "LEGACY"
            )
            compared = compare_documents(
                by_role["BASELINE"],
                by_role["TARGET"],
                CompareOptions(
                    ignore_formatting=raw_options.get("ignore_formatting", True),
                    ignore_headers_footers=raw_options.get("ignore_headers_footers", True),
                    numeric_sensitive=raw_options.get("numeric_sensitive", True),
                    ocr_low_confidence_threshold=self.settings.OCR_LOW_CONFIDENCE_THRESHOLD,
                    page_missing_min_equivalent=(self.settings.PAGE_MISSING_MIN_EQUIVALENT),
                    page_missing_min_anchor_similarity=(
                        self.settings.PAGE_MISSING_MIN_ANCHOR_SIMILARITY
                    ),
                    page_missing_min_structure_units=(
                        self.settings.PAGE_MISSING_MIN_STRUCTURE_UNITS
                    ),
                    comparison_mode=comparison_mode,
                ),
            )
            if not compared.diagnostics.reliable:
                raise WorkflowError(
                    "COMPARISON_UNRELIABLE",
                    "两份合同的内容对齐覆盖率不足，未生成正式报告",
                )
            if (
                comparison_mode == "FINAL_LOGICAL_V2"
                and self.settings.FINAL_COMPARE_EQUIVALENT_FILTER_ENABLED
            ):
                await callback(TaskStage.VERSION_COMPARE, 72, "正在应用确定性安全降重")
                compared = apply_deterministic_final_compare_filters(
                    compared,
                    baseline=by_role["BASELINE"],
                    target=by_role["TARGET"],
                )
            if (
                comparison_mode == "FINAL_LOGICAL_V2"
                and self.settings.FINAL_COMPARE_LLM_ADJUDICATION_ENABLED
            ):
                await callback(TaskStage.VERSION_COMPARE, 72, "正在收敛歧义差异候选")
                compared = await validate_final_compare_duplicate_clusters(
                    compared,
                    self.llm,
                    baseline=by_role["BASELINE"],
                    target=by_role["TARGET"],
                )
            return {"comparison": compared}

        async def build_result(state: FinalCompareState) -> dict[str, Any]:
            await callback(TaskStage.RULE_CHECKING, 86, "正在分类差异并生成固定规则摘要")
            result = self._build_result(
                state["task_id"],
                state["files"],
                state["parsed_documents"],
                state["comparison"],
                state.get("options", {}),
            )
            return {"result": result}

        async def generate_advice(state: FinalCompareState) -> dict[str, Any]:
            result = state["result"]
            if self.llm is None or not hasattr(self.llm, "generate_advice"):
                return {}
            await callback(TaskStage.GENERATING_ADVICE, 92, "正在根据已有证据生成建议")
            try:
                await generate_advice_in_batches(
                    result,
                    self.llm,
                    require_dynamic_anchor=True,
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

        async def persist_result(state: FinalCompareState) -> dict[str, Any]:
            await callback(TaskStage.PERSISTING_RESULT, 97, "正在保存确定性比对结果")
            result = state["result"]
            apply_docx_page_location_sidecars(result, state.get("page_location_sidecars", {}))
            if (
                self.settings.DOCX_PAGE_LOCATION_ENABLED
                and result.get("metadata", {}).get("comparison_mode")
                == "FINAL_LOGICAL_V2"
            ):
                page_coverage = validate_public_page_coverage(
                    result, state.get("page_location_sidecars", {})
                )
                result.setdefault("metadata", {})[
                    "page_location_coverage"
                ] = page_coverage
            return {"result": result}

        graph.add_node("download_files", download_files)
        graph.add_node("parse_documents", parse_documents)
        graph.add_node("compare_versions", compare_versions)
        graph.add_node("build_result", build_result)
        graph.add_node("generate_advice", generate_advice)
        graph.add_node("persist_result", persist_result)
        graph.add_edge(START, "download_files")
        graph.add_edge("download_files", "parse_documents")
        graph.add_edge("parse_documents", "compare_versions")
        graph.add_edge("compare_versions", "build_result")
        graph.add_edge("build_result", "generate_advice")
        graph.add_edge("generate_advice", "persist_result")
        graph.add_edge("persist_result", END)
        return graph.compile()

    def _build_result(
        self,
        task_id: str,
        input_files: list[dict[str, Any]],
        documents: list[ParsedDocument],
        comparison: ComparisonResult,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not comparison.diagnostics.reliable:
            raise WorkflowError(
                "COMPARISON_UNRELIABLE",
                "合同版本对齐覆盖率不足，未生成正式报告",
            )
        input_by_id = {item["file_id"]: item for item in input_files}
        files = [
            {
                "file_id": document.file_id,
                "role": document.role,
                "file_name": document.file_name,
                "safe_url": input_by_id[document.file_id]["safe_url"],
                "sha256": document.sha256,
                "page_count": document.page_count,
                "parser_name": document.parser_name,
                "parse_status": (
                    "WARNING"
                    if any(warning.requires_manual_review for warning in document.warnings)
                    else "SUCCEEDED"
                ),
                "parse_warnings": [
                    warning.model_dump(mode="json") for warning in document.warnings
                ],
                "parser_metadata": document.parser_metadata,
            }
            for document in documents
        ]
        is_logical_v2 = comparison.diagnostics.fallback_mode == "FINAL_LOGICAL_V2"
        public_comparison_diffs = (
            [
                item
                for item in comparison.diff_items
                if item.validation_status == "CONFIRMED"
            ]
            if is_logical_v2
            else list(comparison.diff_items)
        )
        review_items = (
            build_comparison_review_items(
                comparison.diff_items, module_code="VERSION_CHANGE"
            )
            if is_logical_v2
            else []
        )
        diffs = [item.model_dump(mode="json") for item in public_comparison_diffs]
        stamp_images = [
            {
                "file_name": document.file_name,
                "page": stamp.page,
                "data_uri": stamp.data_uri,
            }
            for document in documents
            for stamp in document.stamp_images
        ]
        risk_items = build_risk_items(
            public_comparison_diffs, module_code="VERSION_CHANGE"
        )
        passed_checks = build_comparison_passed_checks(
            documents,
            public_comparison_diffs,
            comparison.diagnostics,
            check_prefix="check_final",
            module_code="VERSION_CHANGE",
            content_title="合同内容未发生变化",
            numeric_sensitive=(options or {}).get("numeric_sensitive", True),
            pending_differences=comparison.diff_items if is_logical_v2 else None,
        )
        statistics = build_statistics(risk_items, review_items, passed_checks)
        conclusion = "RISK_FOUND" if risk_items else "PASS"
        warnings = [warning.model_dump(mode="json") for warning in comparison.warnings]
        warnings.append(
            ProcessingWarning(
                code="RULE_BASED_LIMITATION",
                message=(
                    "本结果来自确定性文字、数值和基础表格比对，可能包含 OCR 解析，"
                    "不包含 LLM 或法律判断"
                ),
                requires_manual_review=False,
            ).model_dump(mode="json")
        )
        result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "task_id": task_id,
            "task_type": TaskType.FINAL_COMPARE.value,
            "conclusion": conclusion,
            "summary": {
                "title": "确定性合同版本比对结果",
                "description": (
                    f"确认 {len(risk_items)} 项风险，{len(passed_checks)} 项校验通过。"
                ),
                "statistics": statistics,
            },
            "files": files,
            "stamp_images": stamp_images,
            "risk_items": risk_items,
            "review_items": review_items,
            "passed_checks": passed_checks,
            "diff_items": diffs,
            "fact_matrix": [],
            "rule_checks": [],
            "warnings": warnings,
            "advice": {
                "overall_advice": "请按来源位置处理确认风险，并单独复核不确定事项。",
                "priority_actions": ["处理金额、期限、主体及关键条款的确认变化"],
                "manual_review_focus": ["差异前后文本及段落、页码或表格位置"],
                "limitations": ["可能使用 OCR；未执行 LLM、复杂合同规则或法律判断"],
            },
            "metadata": {
                "execution_mode": "RULE_BASED",
                "workflow_version": (
                    FINAL_COMPARE_WORKFLOW_VERSION
                    if is_logical_v2
                    else FINAL_COMPARE_LEGACY_WORKFLOW_VERSION
                ),
                "rules_version": (
                    FINAL_COMPARE_RULES_VERSION
                    if is_logical_v2
                    else FINAL_COMPARE_LEGACY_RULES_VERSION
                ),
                "primary_model": None,
                "model_runs": (
                    [comparison.validation_metadata]
                    if comparison.validation_metadata.get("purpose")
                    else []
                ),
                "comparison_diagnostics": comparison.diagnostics.model_dump(mode="json"),
                "comparison_mode": comparison.diagnostics.fallback_mode,
                "candidate_validation": comparison.validation_stats,
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
        if task_type != TaskType.FINAL_COMPARE:
            raise WorkflowError("COMPARISON_FAILED", "真实比对工作流仅支持 FINAL_COMPARE")
        async with TaskWorkspace(self.settings.TEMP_ROOT, task_id) as workspace:
            graph = self._build_graph(workspace, progress_callback)
            state = await graph.ainvoke(
                FinalCompareState(task_id=task_id, files=files, options=options)
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
                    "parse_status": (
                        "WARNING"
                        if any(warning.requires_manual_review for warning in document.warnings)
                        else "SUCCEEDED"
                    ),
                    "parse_warnings": [
                        warning.model_dump(mode="json") for warning in document.warnings
                    ],
                }
                for document in documents
            ]
            return WorkflowOutput(result=state["result"], file_metadata=metadata)
