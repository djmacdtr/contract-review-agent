from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.enums import TaskType
from app.workflows.mock_graphs import ProgressCallback


@dataclass
class WorkflowOutput:
    result: dict[str, Any]
    file_metadata: list[dict[str, Any]] = field(default_factory=list)


class WorkflowExecutor(Protocol):
    async def run(
        self,
        *,
        task_id: str,
        task_type: TaskType,
        files: list[dict[str, Any]],
        options: dict[str, Any],
        progress_callback: ProgressCallback,
    ) -> WorkflowOutput: ...
