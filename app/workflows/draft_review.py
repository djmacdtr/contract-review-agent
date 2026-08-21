from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.adapters.document_parser.textin_parser import TextInDocumentParser
from app.core.config import Settings
from app.core.enums import TaskStage, TaskType
from app.core.errors import WorkflowError
from app.documents.models import ParsedDocument, ProcessingWarning
from app.documents.parsers import ParserRegistry
from app.documents.router import DocumentParsingRouter
from app.draft_review.template_checks import TemplateReviewResult, analyze_template
from app.results.risk_model import build_review_items, build_risk_items, build_statistics
from app.schemas.results import RESULT_SCHEMA_VERSION
from app.services.downloader import LocalFile, SafeFileDownloadService
from app.services.temp_files import TaskWorkspace
from app.workflows.mock_graphs import ProgressCallback
from app.workflows.types import WorkflowOutput

DRAFT_REVIEW_WORKFLOW_VERSION = "0.3.1"
DRAFT_REVIEW_RULES_VERSION = "0.3.1"


class DraftReviewState(TypedDict, total=False):
    task_id: str
    files: list[dict[str, Any]]
    options: dict[str, Any]
    local_files: list[LocalFile]
    parsed_documents: list[ParsedDocument]
    template_review: TemplateReviewResult
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

    def _build_graph(self, workspace: TaskWorkspace, callback: ProgressCallback):
        graph = StateGraph(DraftReviewState)

        async def download_files(state: DraftReviewState) -> dict[str, Any]:
            await callback(TaskStage.DOWNLOADING, 10, "正在受控下载起草检查文件")
            return {"local_files": await self.downloader.prepare(state["files"], workspace)}

        async def parse_documents(state: DraftReviewState) -> dict[str, Any]:
            await callback(TaskStage.PARSING, 35, "正在逐份解析目标、模板和辅助资料")
            return {
                "parsed_documents": await self.parsers.parse_draft_review(state["local_files"])
            }

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

        async def build_result(state: DraftReviewState) -> dict[str, Any]:
            await callback(TaskStage.RULE_CHECKING, 85, "正在汇总模板差异和必填检查")
            return {
                "result": self._build_result(
                    state["task_id"],
                    state["files"],
                    state["parsed_documents"],
                    state["template_review"],
                )
            }

        async def persist_result(state: DraftReviewState) -> dict[str, Any]:
            await callback(TaskStage.PERSISTING_RESULT, 97, "正在保存多文档解析结果")
            return {}

        graph.add_node("download_files", download_files)
        graph.add_node("parse_documents", parse_documents)
        graph.add_node("compare_template", compare_template)
        graph.add_node("build_result", build_result)
        graph.add_node("persist_result", persist_result)
        graph.add_edge(START, "download_files")
        graph.add_edge("download_files", "parse_documents")
        graph.add_edge("parse_documents", "compare_template")
        graph.add_edge("compare_template", "build_result")
        graph.add_edge("build_result", "persist_result")
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
    ) -> dict[str, Any]:
        input_by_id = {item["file_id"]: item for item in input_files}
        files: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        for document in documents:
            document_warnings = [warning.model_dump(mode="json") for warning in document.warnings]
            for warning in document_warnings:
                warning["file_id"] = warning.get("file_id") or document.file_id
                warnings.append(warning)
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
                    "document_profile": {
                        "document_kind": "UNKNOWN",
                        "title": None,
                        "confidence": 0.0,
                        "generated_by": "NOT_RUN",
                        "evidence_locations": [],
                    },
                    "content_structure": self._content_structure(document),
                }
            )
        existing_warning_codes = {warning["code"] for warning in warnings}
        warnings.extend(
            warning.model_dump(mode="json")
            for warning in template_review.warnings
            if warning.code not in existing_warning_codes
        )
        warnings.append(
            ProcessingWarning(
                code="DRAFT_REVIEW_RULE_BASED_LIMITATION",
                message="已执行模板确定性检查；尚未执行辅助资料事实抽取、跨文件核对或 LLM。",
                requires_manual_review=False,
            ).model_dump(mode="json")
        )
        failed_rules = template_review.failed_rule_checks
        risk_items = build_risk_items(
            template_review.diff_items,
            module_code="TEMPLATE_INTEGRITY",
            failed_rules=failed_rules,
        )
        review_items = build_review_items(
            template_review.diff_items,
            template_review.warnings,
            module_code="TEMPLATE_RELIABILITY",
        )
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
            "diff_items": [
                item.model_dump(mode="json") for item in template_review.diff_items
            ],
            "fact_matrix": [],
            "rule_checks": template_review.rule_checks,
            "warnings": warnings,
            "advice": {
                "overall_advice": "请按证据位置复核固定条款差异和未填写字段。",
                "priority_actions": ["处理固定条款、数值和必填问题"],
                "manual_review_focus": ["模板固定文字、金额期限、占位符和表格必填项"],
                "limitations": ["未执行辅助资料事实抽取、跨文件核对、LLM 或法律判断"],
            },
            "metadata": {
                "execution_mode": "RULE_BASED",
                "workflow_version": DRAFT_REVIEW_WORKFLOW_VERSION,
                "rules_version": DRAFT_REVIEW_RULES_VERSION,
                "primary_model": None,
                "model_runs": [],
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
