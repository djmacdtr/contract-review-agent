from datetime import datetime

from pydantic import BaseModel, Field

from app.core.enums import Conclusion, TaskStage, TaskStatus, TaskType


class TaskAccepted(BaseModel):
    task_id: str = Field(examples=["tsk_01K2DRAFT"])
    task_type: TaskType
    status: TaskStatus
    progress: int = Field(ge=0, le=100)
    created_at: datetime
    status_url: str
    result_url: str
    source_task_id: str | None = None


class TaskErrorView(BaseModel):
    code: str
    message: str
    details: dict | None = None


class TaskDetail(BaseModel):
    task_id: str
    task_type: TaskType
    client_reference_id: str | None
    status: TaskStatus
    stage: TaskStage
    stage_message: str | None
    progress: int = Field(ge=0, le=100)
    attempt_count: int
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime
    finished_at: datetime | None
    error: TaskErrorView | None


class TaskSummary(BaseModel):
    task_id: str
    task_type: TaskType
    client_reference_id: str | None
    status: TaskStatus
    progress: int
    conclusion: Conclusion | None
    high_risk_count: int
    medium_risk_count: int
    low_risk_count: int
    info_count: int
    created_at: datetime
    finished_at: datetime | None


class TaskListData(BaseModel):
    items: list[TaskSummary]
    page: int
    page_size: int
    total: int

