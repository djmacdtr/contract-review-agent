from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.adapters.document_parser.textin_parser import TextInDocumentParser
from app.comparison.engine import (
    CompareOptions,
    compare_documents,
    is_low_confidence_ocr_text_diff,
)
from app.comparison.models import ComparisonResult
from app.core.config import Settings
from app.core.enums import TaskStage, TaskType
from app.core.errors import WorkflowError
from app.documents.models import ParsedDocument, ProcessingWarning
from app.documents.parsers import ParserRegistry
from app.documents.router import DocumentParsingRouter
from app.services.downloader import LocalFile, SafeFileDownloadService
from app.services.temp_files import TaskWorkspace
from app.workflows.mock_graphs import ProgressCallback
from app.workflows.types import WorkflowOutput

FINAL_COMPARE_WORKFLOW_VERSION = "0.3.0"
FINAL_COMPARE_RULES_VERSION = "0.3.0"


class FinalCompareState(TypedDict, total=False):
    task_id: str
    files: list[dict[str, Any]]
    options: dict[str, Any]
    local_files: list[LocalFile]
    parsed_documents: list[ParsedDocument]
    comparison: ComparisonResult
    result: dict[str, Any]


class FinalCompareWorkflowExecutor:
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
        graph = StateGraph(FinalCompareState)

        async def download_files(state: FinalCompareState) -> dict[str, Any]:
            await callback(TaskStage.DOWNLOADING, 10, "正在受控下载两个文件")
            return {"local_files": await self.downloader.prepare(state["files"], workspace)}

        async def parse_documents(state: FinalCompareState) -> dict[str, Any]:
            await callback(TaskStage.PARSING, 35, "正在解析 DOCX、文本型 PDF 或扫描 PDF")
            parsed = []
            for file in state["local_files"]:
                parsed.append(await self.parsers.parse(file))
            return {"parsed_documents": parsed}

        async def compare_versions(state: FinalCompareState) -> dict[str, Any]:
            await callback(TaskStage.VERSION_COMPARE, 68, "正在对齐条款、文字和基础表格")
            by_role = {document.role: document for document in state["parsed_documents"]}
            if "BASELINE" not in by_role or "TARGET" not in by_role:
                raise WorkflowError("COMPARISON_FAILED", "比对任务缺少基准文件或目标文件")
            raw_options = state.get("options", {})
            compared = compare_documents(
                by_role["BASELINE"],
                by_role["TARGET"],
                CompareOptions(
                    ignore_formatting=raw_options.get("ignore_formatting", True),
                    ignore_headers_footers=raw_options.get("ignore_headers_footers", True),
                    numeric_sensitive=raw_options.get("numeric_sensitive", True),
                    ocr_low_confidence_threshold=self.settings.OCR_LOW_CONFIDENCE_THRESHOLD,
                ),
            )
            return {"comparison": compared}

        async def build_result(state: FinalCompareState) -> dict[str, Any]:
            await callback(TaskStage.RULE_CHECKING, 86, "正在分类差异并生成固定规则摘要")
            result = self._build_result(
                state["task_id"], state["files"], state["parsed_documents"], state["comparison"]
            )
            return {"result": result}

        async def persist_result(state: FinalCompareState) -> dict[str, Any]:
            await callback(TaskStage.PERSISTING_RESULT, 97, "正在保存确定性比对结果")
            return {}

        graph.add_node("download_files", download_files)
        graph.add_node("parse_documents", parse_documents)
        graph.add_node("compare_versions", compare_versions)
        graph.add_node("build_result", build_result)
        graph.add_node("persist_result", persist_result)
        graph.add_edge(START, "download_files")
        graph.add_edge("download_files", "parse_documents")
        graph.add_edge("parse_documents", "compare_versions")
        graph.add_edge("compare_versions", "build_result")
        graph.add_edge("build_result", "persist_result")
        graph.add_edge("persist_result", END)
        return graph.compile()

    def _build_result(
        self,
        task_id: str,
        input_files: list[dict[str, Any]],
        documents: list[ParsedDocument],
        comparison: ComparisonResult,
    ) -> dict[str, Any]:
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
        diffs = [item.model_dump(mode="json") for item in comparison.diff_items]
        statistics = {"total": len(diffs), "high": 0, "medium": 0, "low": 0, "info": 0}
        for item in comparison.diff_items:
            statistics[item.severity.lower()] += 1
        if diffs and all(
            is_low_confidence_ocr_text_diff(item, self.settings.OCR_LOW_CONFIDENCE_THRESHOLD)
            for item in comparison.diff_items
        ):
            conclusion = "REVIEW_REQUIRED"
        elif diffs:
            conclusion = "RISK_FOUND"
        elif any(warning.requires_manual_review for warning in comparison.warnings):
            conclusion = "REVIEW_REQUIRED"
        else:
            conclusion = "PASS"
        warnings = [warning.model_dump(mode="json") for warning in comparison.warnings]
        warnings.append(
            ProcessingWarning(
                code="RULE_BASED_LIMITATION",
                message="本结果来自确定性文字、数值和基础表格比对，可能包含 OCR 解析，不包含 LLM 或法律判断",
                requires_manual_review=False,
            ).model_dump(mode="json")
        )
        return {
            "schema_version": self.settings.RESULT_SCHEMA_VERSION,
            "task_id": task_id,
            "task_type": TaskType.FINAL_COMPARE.value,
            "conclusion": conclusion,
            "summary": {
                "title": "确定性合同版本比对结果",
                "description": f"共发现 {len(diffs)} 项可追溯差异；结果仅用于辅助人工复核。",
                "statistics": statistics,
            },
            "files": files,
            "risk_items": [],
            "diff_items": diffs,
            "fact_matrix": [],
            "rule_checks": [],
            "warnings": warnings,
            "advice": {
                "overall_advice": f"请按来源位置人工复核 {len(diffs)} 项确定性差异。",
                "priority_actions": ["优先复核 HIGH 级金额、期限、主体及关键条款变化"],
                "manual_review_focus": ["差异前后文本及段落、页码或表格位置"],
                "limitations": ["可能使用 OCR；未执行 LLM、复杂合同规则或法律判断"],
            },
            "metadata": {
                "execution_mode": "RULE_BASED",
                "workflow_version": FINAL_COMPARE_WORKFLOW_VERSION,
                "rules_version": FINAL_COMPARE_RULES_VERSION,
                "primary_model": None,
                "model_runs": [],
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
        if task_type != TaskType.FINAL_COMPARE:
            raise WorkflowError("COMPARISON_FAILED", "真实比对工作流仅支持 FINAL_COMPARE")
        async with TaskWorkspace(self.settings.TEMP_ROOT, task_id) as workspace:
            graph = self._build_graph(workspace, progress_callback)
            state = await graph.ainvoke(
                FinalCompareState(task_id=task_id, files=files, options=options)
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
