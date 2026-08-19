from typing import Any

from app.core.config import Settings
from app.core.enums import TaskType
from app.workflows.final_compare import FinalCompareWorkflowExecutor
from app.workflows.mock_graphs import MockWorkflowExecutor, ProgressCallback
from app.workflows.types import WorkflowOutput


class WorkflowRouter:
    def __init__(
        self,
        settings: Settings,
        *,
        mock: MockWorkflowExecutor | None = None,
        final_compare: FinalCompareWorkflowExecutor | None = None,
    ) -> None:
        self.mock = mock or MockWorkflowExecutor(settings)
        self.final_compare = final_compare or FinalCompareWorkflowExecutor(settings)

    async def run(
        self,
        *,
        task_id: str,
        task_type: TaskType,
        files: list[dict[str, Any]],
        options: dict[str, Any],
        progress_callback: ProgressCallback,
    ) -> WorkflowOutput:
        if task_type == TaskType.FINAL_COMPARE:
            return await self.final_compare.run(
                task_id=task_id,
                task_type=task_type,
                files=files,
                options=options,
                progress_callback=progress_callback,
            )
        result = await self.mock.run(
            task_id=task_id,
            task_type=task_type,
            files=files,
            options=options,
            progress_callback=progress_callback,
        )
        return WorkflowOutput(result=result)
