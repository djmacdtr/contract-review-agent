import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.core.config import Settings
from app.core.enums import TaskStage, TaskType
from app.schemas.results import RESULT_SCHEMA_VERSION

ProgressCallback = Callable[[TaskStage, int, str], Awaitable[None]]


class WorkflowState(TypedDict, total=False):
    task_id: str
    task_type: str
    files: list[dict[str, Any]]
    result: dict[str, Any]
    completed_nodes: list[str]


class MockWorkflowExecutor:
    def __init__(self, settings: Settings, *, fail_stage: TaskStage | None = None) -> None:
        self.settings = settings
        self.fail_stage = fail_stage

    def _node(
        self,
        name: str,
        stage: TaskStage,
        progress: int,
        message: str,
        callback: ProgressCallback,
    ):
        async def run(state: WorkflowState) -> dict[str, Any]:
            if self.fail_stage == stage:
                raise RuntimeError(f"injected mock failure at {stage.value}")
            await callback(stage, progress, message)
            if self.settings.MOCK_STAGE_DELAY_SECONDS:
                await asyncio.sleep(self.settings.MOCK_STAGE_DELAY_SECONDS)
            return {"completed_nodes": [*state.get("completed_nodes", []), name]}

        return run

    def _build_graph(self, task_type: TaskType, callback: ProgressCallback):
        graph = StateGraph(WorkflowState)
        if task_type == TaskType.DRAFT_REVIEW:
            stages = [
                ("prepare_files", TaskStage.DOWNLOADING, 8, "模拟文件准备"),
                ("parse_documents", TaskStage.PARSING, 22, "模拟文档解析"),
                ("compare_template", TaskStage.TEMPLATE_COMPARE, 38, "模拟模板比对"),
                ("extract_facts", TaskStage.FACT_EXTRACTION, 54, "模拟事实抽取"),
                ("cross_validate", TaskStage.CROSS_VALIDATE, 66, "模拟跨资料核对"),
                ("rule_check", TaskStage.RULE_CHECKING, 78, "模拟规则检查"),
                ("generate_advice", TaskStage.GENERATING_ADVICE, 90, "模拟建议生成"),
                ("persist_result", TaskStage.PERSISTING_RESULT, 97, "保存模拟结果"),
            ]
        else:
            stages = [
                ("prepare_files", TaskStage.DOWNLOADING, 10, "模拟文件准备"),
                ("parse_documents", TaskStage.PARSING, 28, "模拟文档解析"),
                ("compare_versions", TaskStage.VERSION_COMPARE, 55, "模拟版本比对"),
                ("rule_check", TaskStage.RULE_CHECKING, 75, "模拟差异风险分类"),
                ("generate_advice", TaskStage.GENERATING_ADVICE, 90, "模拟建议生成"),
                ("persist_result", TaskStage.PERSISTING_RESULT, 97, "保存模拟结果"),
            ]
        previous = START
        for name, stage, progress, message in stages:
            graph.add_node(name, self._node(name, stage, progress, message, callback))
            graph.add_edge(previous, name)
            previous = name
        graph.add_edge(previous, END)
        return graph.compile()

    async def run(
        self,
        *,
        task_id: str,
        task_type: TaskType,
        files: list[dict[str, Any]],
        options: dict[str, Any] | None = None,
        progress_callback: ProgressCallback,
    ) -> dict[str, Any]:
        graph = self._build_graph(task_type, progress_callback)
        await graph.ainvoke(
            WorkflowState(
                task_id=task_id,
                task_type=task_type.value,
                files=files,
                completed_nodes=[],
            )
        )
        return self.build_result(task_id, task_type, files)

    def build_result(
        self, task_id: str, task_type: TaskType, files: list[dict[str, Any]]
    ) -> dict[str, Any]:
        file_views = [
            {
                "file_id": item["file_id"],
                "role": item["role"],
                "file_name": item["file_name"],
                "safe_url": item["safe_url"],
                "parser_name": "mock-parser",
                "parse_status": "WARNING",
            }
            for item in files
        ]
        shared_warning = {
            "code": "MOCK_RESULT",
            "message": "这是工程闭环模拟结果，未下载、解析或审查任何真实合同。",
            "requires_manual_review": False,
        }
        if task_type == TaskType.DRAFT_REVIEW:
            risk_items = [
                {
                    "risk_id": f"risk_{task_id[-8:]}",
                    "module_code": "FACT_CONSISTENCY",
                    "risk_type": "ADDITION_OR_CHANGE",
                    "change_type": "SOURCE_CONFLICT",
                    "title": "模拟：融资金额需要人工核对",
                    "description": "此项仅用于验证前端渲染，不代表真实合同存在风险。",
                    "source_evidence": [],
                    "related_diff_ids": [],
                    "related_rule_ids": [],
                    "requires_manual_action": True,
                    "analysis_advice": "请核对模拟融资金额风险对应的目标合同与辅助资料来源。",
                }
            ]
            diff_items: list[dict[str, Any]] = []
            fact_matrix = [
                {
                    "field_key": "financing_amount",
                    "display_name": "融资金额（模拟）",
                    "status": "CONFLICT",
                    "candidates": [
                        {
                            "field_key": "financing_amount",
                            "display_name": "融资金额（模拟）",
                            "value_type": "MONEY",
                            "raw_value": "5,000.00万元（模拟）",
                            "normalized_hint": "50000000.00",
                            "normalized_value": "50000000.00",
                            "source_file_id": files[0]["file_id"],
                            "evidence_text": "模拟事实，不代表合同正文",
                            "location": {"paragraph_index": 1},
                            "confidence": 0.0,
                        }
                    ],
                    "missing_source_file_ids": [],
                }
            ]
            rule_checks = [
                {
                    "rule_id": "mock.rent_schedule.row_equation",
                    "rule_name": "每期本金加利息等于当期租金（模拟）",
                    "status": "FAILED",
                    "location": {"file_id": files[0]["file_id"], "table_index": 1, "row": 1},
                    "inputs": {
                        "principal": "6003808.01",
                        "interest": "572500.99",
                        "rent": "6576308.00",
                    },
                    "expected": "6576309.00",
                    "actual": "6576308.00",
                    "tolerance": "0.01",
                    "message": "模拟计算错误，仅用于界面测试",
                }
            ]
            title = "模拟起草检查结果"
        else:
            risk_items = []
            diff_items = [
                {
                    "diff_id": f"diff_{task_id[-8:]}",
                    "diff_type": "NUMERIC_CHANGED",
                    "title": "模拟：租赁期限发生变化",
                    "baseline": {
                        "file_id": files[0]["file_id"],
                        "location": {"section": "租赁附表"},
                        "text": "租赁期限为24个月（模拟）",
                    },
                    "target": {
                        "file_id": files[1]["file_id"],
                        "location": {"section": "租赁附表"},
                        "text": "租赁期限为36个月（模拟）",
                    },
                    "segments": [
                        {"operation": "EQUAL", "text": "租赁期限为"},
                        {"operation": "DELETE", "text": "24"},
                        {"operation": "INSERT", "text": "36"},
                        {"operation": "EQUAL", "text": "个月"},
                    ],
                    "confidence": 0.0,
                    "requires_manual_review": True,
                }
            ]
            fact_matrix = []
            rule_checks = []
            risk_items = [
                {
                    "risk_id": f"risk_{task_id[-8:]}",
                    "module_code": "VERSION_CHANGE",
                    "risk_type": "ADDITION_OR_CHANGE",
                    "change_type": "NUMERIC_CHANGED",
                    "title": "模拟：租赁期限发生变化",
                    "description": "此项仅用于验证前端渲染，不代表真实合同存在风险。",
                    "source_evidence": [],
                    "related_diff_ids": [f"diff_{task_id[-8:]}"],
                    "related_rule_ids": [],
                    "requires_manual_action": True,
                    "analysis_advice": "请核对模拟期限由 24 个月变为 36 个月的业务依据。",
                }
            ]
            title = "模拟放款比对结果"

        review_items: list[dict[str, Any]] = []
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "task_id": task_id,
            "task_type": task_type.value,
            "conclusion": "RISK_FOUND" if risk_items else "PASS",
            "summary": {
                "title": title,
                "description": "模拟结果仅用于验证任务、API 和控制台闭环，不构成合同审查意见。",
                "statistics": {
                    "risk_count": len(risk_items),
                    "deletion_or_missing_count": 0,
                    "addition_or_change_count": len(risk_items),
                    "review_count": 0,
                    "passed_check_count": 0,
                    "legacy_statistics": False,
                },
            },
            "files": file_views,
            "risk_items": risk_items,
            "review_items": review_items,
            "passed_checks": [],
            "diff_items": diff_items,
            "fact_matrix": fact_matrix,
            "rule_checks": rule_checks,
            "warnings": [shared_warning],
            "advice": {
                "overall_advice": "模拟建议：请结合原始文件人工复核。",
                "priority_actions": ["不要将本结果用于实际审批或放款决策"],
                "manual_review_focus": ["金额、期限、主体及证据位置"],
                "limitations": ["未执行真实下载、解析、OCR、差异计算或 LLM 调用"],
            },
            "metadata": {
                "execution_mode": "MOCK",
                "workflow_version": self.settings.WORKFLOW_VERSION,
                "rules_version": self.settings.RULES_VERSION,
                "primary_model": self.settings.LLM_EXTRACTION_MODEL,
                "model_runs": [
                    {
                        "node": (
                            "extract_facts"
                            if task_type == TaskType.DRAFT_REVIEW
                            else "generate_advice"
                        ),
                        "configured_model": self.settings.LLM_EXTRACTION_MODEL,
                        "actual_model": None,
                        "prompt_version": "mock-v1",
                        "schema_valid": True,
                        "mock": True,
                    }
                ],
            },
            "mock": True,
        }
