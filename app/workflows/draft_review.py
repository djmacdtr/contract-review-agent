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
from app.services.downloader import LocalFile, SafeFileDownloadService
from app.services.temp_files import TaskWorkspace
from app.workflows.mock_graphs import ProgressCallback
from app.workflows.types import WorkflowOutput

DRAFT_REVIEW_WORKFLOW_VERSION = "0.2.0"
DRAFT_REVIEW_RULES_VERSION = "0.2.0"


class DraftReviewState(TypedDict, total=False):
    task_id: str
    files: list[dict[str, Any]]
    options: dict[str, Any]
    local_files: list[LocalFile]
    parsed_documents: list[ParsedDocument]
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
            await callback(TaskStage.PARSING, 45, "正在逐份解析目标、模板和辅助资料")
            return {
                "parsed_documents": await self.parsers.parse_draft_review(state["local_files"])
            }

        async def build_result(state: DraftReviewState) -> dict[str, Any]:
            await callback(TaskStage.PARSING, 85, "全部文件解析完成，正在汇总解析状态")
            return {
                "result": self._build_result(
                    state["task_id"], state["files"], state["parsed_documents"]
                )
            }

        async def persist_result(state: DraftReviewState) -> dict[str, Any]:
            await callback(TaskStage.PERSISTING_RESULT, 97, "正在保存多文档解析结果")
            return {}

        graph.add_node("download_files", download_files)
        graph.add_node("parse_documents", parse_documents)
        graph.add_node("build_result", build_result)
        graph.add_node("persist_result", persist_result)
        graph.add_edge(START, "download_files")
        graph.add_edge("download_files", "parse_documents")
        graph.add_edge("parse_documents", "build_result")
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
        warnings.append(
            ProcessingWarning(
                code="DRAFT_REVIEW_PARSE_ONLY",
                message="本阶段仅完成真实下载和解析；尚未执行模板比对、事实抽取、规则或 LLM。",
                requires_manual_review=True,
            ).model_dump(mode="json")
        )
        return {
            "schema_version": self.settings.RESULT_SCHEMA_VERSION,
            "task_id": task_id,
            "task_type": TaskType.DRAFT_REVIEW.value,
            "conclusion": "REVIEW_REQUIRED",
            "summary": {
                "title": "起草检查多文档解析结果",
                "description": f"已真实解析 {len(files)} 份文件，后续检查阶段尚未执行。",
                "statistics": {"total": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            },
            "files": files,
            "risk_items": [],
            "diff_items": [],
            "fact_matrix": [],
            "rule_checks": [],
            "warnings": warnings,
            "advice": {
                "overall_advice": "文件解析已完成，请等待后续模板和跨资料检查能力。",
                "priority_actions": [],
                "manual_review_focus": ["逐份确认解析状态和警告"],
                "limitations": ["未执行模板比对、事实抽取、LLM、合同规则或法律判断"],
            },
            "metadata": {
                "execution_mode": "PARSER_ONLY",
                "workflow_version": DRAFT_REVIEW_WORKFLOW_VERSION,
                "rules_version": DRAFT_REVIEW_RULES_VERSION,
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
