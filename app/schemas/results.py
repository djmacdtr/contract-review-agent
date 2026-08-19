from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.comparison.models import DiffItem
from app.core.enums import Conclusion, TaskType


class ResultStatistics(BaseModel):
    total: int
    high: int
    medium: int
    low: int
    info: int


class ResultSummary(BaseModel):
    title: str
    description: str
    statistics: ResultStatistics


class ResultFile(BaseModel):
    model_config = ConfigDict(extra="allow")

    file_id: str
    role: str
    file_name: str
    safe_url: str
    sha256: str | None = None
    page_count: int | None = None
    parser_name: str
    parse_status: str
    parse_warnings: list[dict[str, Any]] = Field(default_factory=list)


class ResultMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    execution_mode: str
    workflow_version: str
    rules_version: str
    primary_model: str | None
    model_runs: list[dict[str, Any]]


class TaskResultData(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str = Field(examples=["1.0"])
    task_id: str
    task_type: TaskType
    conclusion: Conclusion
    summary: ResultSummary
    files: list[ResultFile]
    risk_items: list[dict[str, Any]]
    diff_items: list[DiffItem]
    fact_matrix: list[dict[str, Any]]
    rule_checks: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    advice: dict[str, Any]
    metadata: ResultMetadata
    mock: bool
