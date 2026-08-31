"""Private Advice-only regeneration for a successful FINAL_COMPARE report."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.adapters.llm.base import ContractLlmClient
from app.adapters.llm.openai_client import OpenAIContractLlmClient
from app.core.config import Settings
from app.core.enums import TaskStage, TaskStatus, TaskType
from app.core.errors import WorkflowError
from app.db.models import CheckTask, TaskResult
from app.db.session import SessionFactory
from app.results.advice import ensure_fallback_risk_advices
from app.results.advice_batches import generate_advice_in_batches
from app.schemas.results import TaskResultData
from app.workflows.types import WorkflowOutput

FINAL_COMPARE_ADVICE_REGENERATION_VERSION = "final-compare-advice-v1"
FINAL_COMPARE_ADVICE_REGENERATION_MODE = "ADVICE_ONLY"


def _map_file_id(value: str, file_id_map: dict[str, str]) -> str:
    if value in file_id_map:
        return file_id_map[value]
    for old_id, new_id in sorted(file_id_map.items(), key=lambda item: -len(item[0])):
        if value.startswith(f"{old_id}_"):
            return f"{new_id}{value[len(old_id):]}"
    return value


def _remap_file_references(
    value: Any,
    file_id_map: dict[str, str],
    *,
    task_id: str,
) -> Any:
    reference_keys = {
        "file_id",
        "source_file_id",
        "reference_file_id",
        "target_file_id",
        "baseline_file_id",
        "file_ids",
        "source_file_ids",
        "reference_file_ids",
        "target_file_ids",
    }

    def visit(item: Any, key: str | None = None, *, root: bool = False) -> Any:
        if isinstance(item, dict):
            return {
                name: (
                    task_id
                    if root and name == "task_id"
                    else visit(child, name)
                )
                for name, child in item.items()
            }
        if isinstance(item, list):
            return [visit(child, key) for child in item]
        if isinstance(item, tuple):
            return tuple(visit(child, key) for child in item)
        if isinstance(item, str) and (
            key in reference_keys or (key is not None and key.endswith("_file_id"))
        ):
            return _map_file_id(item, file_id_map)
        return item

    remapped = visit(value, root=True)
    if isinstance(remapped, dict):
        remapped["task_id"] = task_id
    return remapped


def _file_references(value: Any) -> list[str]:
    keys = {
        "file_id",
        "source_file_id",
        "reference_file_id",
        "target_file_id",
        "baseline_file_id",
        "file_ids",
        "source_file_ids",
        "reference_file_ids",
        "target_file_ids",
    }
    found: list[str] = []

    def visit(item: Any, key: str | None = None) -> None:
        if isinstance(item, dict):
            for name, child in item.items():
                visit(child, name)
        elif isinstance(item, list | tuple):
            for child in item:
                visit(child, key)
        elif isinstance(item, str) and (
            key in keys or (key is not None and key.endswith("_file_id"))
        ):
            found.append(item)

    visit(value)
    return found


class FinalCompareAdviceRegenerationWorkflowExecutor:
    """Run only Advice against a read-only successful source result."""

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory=SessionFactory,
        llm: ContractLlmClient | None = None,
        prepared_results: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.prepared_results = prepared_results or {}
        self.llm = llm or (
            OpenAIContractLlmClient(
                settings,
                advice_response_format_override="json_object",
            )
            if settings.llm_configured
            else None
        )

    async def _load_source(self, source_task_id: str) -> tuple[CheckTask, dict[str, Any]]:
        async with self.session_factory() as session:
            source = (
                await session.execute(
                    select(CheckTask)
                    .where(CheckTask.id == source_task_id)
                    .options(selectinload(CheckTask.files))
                )
            ).scalar_one_or_none()
            row = await session.get(TaskResult, source_task_id)
        if source is None or row is None:
            raise WorkflowError(
                "ADVICE_REGENERATION_SOURCE_INVALID",
                "Advice 再生成来源报告不存在",
                details={
                    "failure_stage": "ADVICE_REGENERATION_SOURCE",
                    "failure_code": "SOURCE_NOT_FOUND",
                },
            )
        if source.status != TaskStatus.SUCCEEDED or source.task_type != TaskType.FINAL_COMPARE:
            raise WorkflowError(
                "ADVICE_REGENERATION_SOURCE_INVALID",
                "Advice 再生成仅接受成功的 FINAL_COMPARE 报告",
                details={
                    "failure_stage": "ADVICE_REGENERATION_SOURCE",
                    "failure_code": "SOURCE_STATUS_INVALID",
                },
            )
        try:
            result = TaskResultData.model_validate(row.result).model_dump(mode="json")
        except (TypeError, ValueError) as exc:
            raise WorkflowError(
                "ADVICE_REGENERATION_SOURCE_INVALID",
                "来源报告不符合正式结果结构",
                details={
                    "failure_stage": "ADVICE_REGENERATION_SOURCE",
                    "failure_code": "SOURCE_RESULT_INVALID",
                },
            ) from exc
        return source, result

    def _remap_source_result(
        self,
        source: CheckTask,
        source_result: dict[str, Any],
        current_files: list[dict[str, Any]],
        task_id: str,
    ) -> dict[str, Any]:
        source_files_by_role = {item.role.value: item for item in source.files}
        current_by_role = {str(item.get("role")): item for item in current_files}
        result_files_by_role = {
            str(item.get("role")): item for item in source_result.get("files", [])
        }
        expected_roles = {"BASELINE", "TARGET"}
        if (
            set(source_files_by_role) != expected_roles
            or set(current_by_role) != expected_roles
            or set(result_files_by_role) != expected_roles
        ):
            raise WorkflowError(
                "ADVICE_REGENERATION_SOURCE_INVALID",
                "来源报告文件结构不完整",
                details={
                    "failure_stage": "FILE_ID_REMAP",
                    "failure_code": "FILE_ROLE_SET_INVALID",
                },
            )
        file_id_map: dict[str, str] = {}
        for role in sorted(expected_roles):
            source_file = source_files_by_role[role]
            current_file = current_by_role[role]
            result_file = result_files_by_role[role]
            if (
                source_file.id != result_file.get("file_id")
                or source_file.file_name != current_file.get("file_name")
                or result_file.get("file_name") != current_file.get("file_name")
            ):
                raise WorkflowError(
                    "ADVICE_REGENERATION_SOURCE_INVALID",
                    "来源报告文件身份与再生成文件不一致",
                    details={
                        "failure_stage": "FILE_ID_REMAP",
                        "failure_code": "FILE_ID_SOURCE_MISMATCH",
                    },
                )
            file_id_map[source_file.id] = str(current_file["file_id"])

        remapped = _remap_file_references(source_result, file_id_map, task_id=task_id)
        old_ids = set(file_id_map)
        new_ids = set(file_id_map.values())
        for reference in _file_references(remapped):
            if reference in old_ids or reference.startswith(tuple(f"{item}_" for item in old_ids)):
                raise WorkflowError(
                    "ADVICE_REGENERATION_FILE_REMAP_INCOMPLETE",
                    "再生成报告仍引用来源文件身份",
                    details={
                        "failure_stage": "FILE_ID_REMAP",
                        "failure_code": "OLD_FILE_ID_REMAINED",
                    },
                )
            if reference not in new_ids and not reference.startswith(
                tuple(f"{item}_" for item in new_ids)
            ):
                raise WorkflowError(
                    "ADVICE_REGENERATION_FILE_REMAP_INCOMPLETE",
                    "再生成报告包含未知文件身份",
                    details={
                        "failure_stage": "FILE_ID_REMAP",
                        "failure_code": "UNKNOWN_FILE_ID",
                    },
                )
        return remapped

    async def run(
        self,
        *,
        task_id: str,
        task_type: TaskType,
        files: list[dict[str, Any]],
        options: dict[str, Any],
        progress_callback,
    ) -> WorkflowOutput:
        if task_type != TaskType.FINAL_COMPARE:
            raise WorkflowError(
                "ADVICE_REGENERATION_TASK_INVALID",
                "Advice 再生成仅支持 FINAL_COMPARE",
            )
        source_task_id = options.get("_final_compare_advice_source_task_id")
        if not isinstance(source_task_id, str) or not source_task_id:
            raise WorkflowError(
                "ADVICE_REGENERATION_TASK_INVALID",
                "Advice 再生成缺少来源任务",
                details={
                    "failure_stage": "ADVICE_REGENERATION_SETUP",
                    "failure_code": "SOURCE_TASK_ID_MISSING",
                },
            )
        if options.get("_final_compare_advice_regeneration_version") != (
            FINAL_COMPARE_ADVICE_REGENERATION_VERSION
        ):
            raise WorkflowError(
                "ADVICE_REGENERATION_TASK_INVALID",
                "Advice 再生成版本标记无效",
                details={
                    "failure_stage": "ADVICE_REGENERATION_SETUP",
                    "failure_code": "REGENERATION_VERSION_INVALID",
                },
            )
        source, source_result = await self._load_source(source_task_id)
        prepared = self.prepared_results.get(source_task_id)
        result = self._remap_source_result(
            source,
            prepared if prepared is not None else source_result,
            files,
            task_id,
        )
        if prepared is None:
            for risk in result.get("risk_items", []):
                risk["analysis_advice"] = None
            result["advice"] = {
                "overall_advice": "请按来源位置处理确认风险，并单独复核不确定事项。",
                "priority_actions": [],
                "manual_review_focus": [],
                "limitations": [],
                "evidence_refs": [],
                "risk_advices": [],
            }
            result.setdefault("metadata", {})["model_runs"] = [
                item
                for item in result["metadata"].get("model_runs", [])
                if not (isinstance(item, dict) and item.get("purpose") == "RISK_ADVICE")
            ]
            if self.llm is None or not hasattr(self.llm, "generate_advice"):
                raise WorkflowError(
                    "ADVICE_REGENERATION_NOT_CONFIGURED",
                    "Advice 模型未配置，无法执行再生成",
                    details={
                        "failure_stage": "GENERATING_ADVICE",
                        "failure_code": "LLM_NOT_CONFIGURED",
                    },
                )
        await progress_callback(TaskStage.GENERATING_ADVICE, 92, "正在分批重新生成建议")
        if prepared is None:
            stats = await generate_advice_in_batches(
                result,
                self.llm,
                require_dynamic_anchor=True,
            )
        else:
            coverage = result.get("metadata", {}).get("advice_coverage", {})
            model_rate = coverage.get("model_rate")
            if not isinstance(model_rate, (int, float)) or model_rate < 0.95:
                raise WorkflowError(
                    "ADVICE_REGENERATION_QUALITY_GATE",
                    "Advice 模型覆盖率未达到发布门禁",
                    details={
                        "failure_stage": "ADVICE_QUALITY_GATE",
                        "failure_code": "MODEL_COVERAGE_BELOW_THRESHOLD",
                    },
                )
            stats = None
        coverage = result.get("metadata", {}).get("advice_coverage", {})
        model_rate = coverage.get("model_rate")
        if not isinstance(model_rate, (int, float)) or model_rate < 0.95:
            raise WorkflowError(
                "ADVICE_REGENERATION_QUALITY_GATE",
                "Advice 模型覆盖率未达到发布门禁",
                details={
                    "failure_stage": "ADVICE_QUALITY_GATE",
                    "failure_code": "MODEL_COVERAGE_BELOW_THRESHOLD",
                },
            )
        stats_data = stats.as_dict() if stats is not None else coverage
        result["metadata"]["advice_regeneration"] = {
            "source_task_id": source_task_id,
            "version": FINAL_COMPARE_ADVICE_REGENERATION_VERSION,
            "mode": FINAL_COMPARE_ADVICE_REGENERATION_MODE,
            "fact_extraction_calls": 0,
            "comparison_calls": 0,
            "page_location_calls": 0,
            **stats_data,
        }
        ensure_fallback_risk_advices(result)
        TaskResultData.model_validate(result)
        await progress_callback(TaskStage.PERSISTING_RESULT, 97, "正在保存 Advice 再生成报告")
        return WorkflowOutput(result=result, file_metadata=[])
