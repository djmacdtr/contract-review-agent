from typing import Any


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class TaskNotFoundError(AppError):
    def __init__(self, task_id: str) -> None:
        super().__init__("TASK_NOT_FOUND", "任务不存在", status_code=404, details={"task_id": task_id})


class WorkflowError(Exception):
    """A safe, stable task failure that may be persisted and returned to clients."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable
