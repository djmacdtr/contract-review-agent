from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.comparison.models import DiffItem
from app.core.enums import Conclusion, TaskType

RESULT_SCHEMA_VERSION = "2.1"


class ResultStatistics(BaseModel):
    risk_count: int = Field(ge=0)
    deletion_or_missing_count: int = Field(ge=0)
    addition_or_change_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    passed_check_count: int = Field(ge=0)
    legacy_statistics: bool = False

    @model_validator(mode="before")
    @classmethod
    def convert_legacy_statistics(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "risk_count" in value:
            return value
        risk_count = int(value.get("high", 0)) + int(value.get("medium", 0))
        review_count = int(value.get("low", 0)) + int(value.get("info", 0))
        return {
            "risk_count": risk_count,
            "deletion_or_missing_count": 0,
            "addition_or_change_count": risk_count,
            "review_count": review_count,
            "passed_check_count": 0,
            "legacy_statistics": True,
        }


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
    parser_metadata: dict[str, Any] = Field(default_factory=dict)


class ResultMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    execution_mode: str
    workflow_version: str
    rules_version: str
    primary_model: str | None
    model_runs: list[dict[str, Any]]


class RiskItem(BaseModel):
    risk_id: str
    module_code: str
    risk_type: Literal["DELETION_OR_MISSING", "ADDITION_OR_CHANGE"]
    change_type: str
    title: str
    description: str
    source_evidence: list[dict[str, Any]] = Field(default_factory=list)
    related_diff_ids: list[str] = Field(default_factory=list)
    related_rule_ids: list[str] = Field(default_factory=list)
    requires_manual_action: bool = True
    analysis_advice: str | None = None


class ReviewItem(BaseModel):
    review_id: str
    module_code: str
    reason_code: str
    title: str
    description: str
    source_evidence: list[dict[str, Any]] = Field(default_factory=list)
    related_diff_ids: list[str] = Field(default_factory=list)
    requires_manual_action: bool = True


class PassedCheck(BaseModel):
    check_id: str
    module_code: str
    title: str
    description: str


class FactMatrixCandidate(BaseModel):
    field_key: str
    display_name: str
    value_type: str
    raw_value: str
    normalized_hint: str | None = None
    normalized_value: str
    source_file_id: str
    evidence_text: str
    location: dict[str, Any]
    confidence: float = Field(ge=0, le=1)


class FactReferenceResult(BaseModel):
    source_file_id: str
    status: Literal["CONSISTENT", "CONFLICT", "MISSING", "UNCERTAIN"]
    candidate: FactMatrixCandidate | None = None
    reason_code: str
    requires_manual_review: bool = False


class FactMatrixItem(BaseModel):
    target_fact_id: str | None = None
    field_key: str
    display_name: str
    status: Literal["CONSISTENT", "CONFLICT", "MISSING", "UNCERTAIN"] = Field(
        description="MISSING 表示辅助资料未提及目标事实（NOT_MENTIONED），不是冲突"
    )
    target_candidate: FactMatrixCandidate | None = None
    candidates: list[FactMatrixCandidate] = Field(default_factory=list)
    reference_results: list[FactReferenceResult] = Field(default_factory=list)
    missing_source_file_ids: list[str] = Field(default_factory=list)


class RuleCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rule_id: str
    rule_name: str
    status: Literal["PASSED", "FAILED", "REVIEW_REQUIRED"]
    location: dict[str, Any] | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    expected: str | None = None
    actual: str | None = None
    message: str


class TaskResultData(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str = Field(examples=["2.1"])
    task_id: str
    task_type: TaskType
    conclusion: Conclusion
    summary: ResultSummary
    files: list[ResultFile]
    risk_items: list[RiskItem]
    review_items: list[ReviewItem] = Field(default_factory=list)
    passed_checks: list[PassedCheck] = Field(default_factory=list)
    diff_items: list[DiffItem]
    fact_matrix: list[FactMatrixItem]
    rule_checks: list[RuleCheck]
    warnings: list[dict[str, Any]]
    advice: dict[str, Any]
    metadata: ResultMetadata
    mock: bool

    @model_validator(mode="before")
    @classmethod
    def convert_legacy_risk_items(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("schema_version") in {"2.0", "2.1"}:
            return value
        converted = dict(value)
        legacy_items = []
        for index, item in enumerate(value.get("risk_items") or [], start=1):
            if "module_code" in item and "risk_type" in item:
                legacy_items.append(item)
                continue
            change_type = str(item.get("category") or "LEGACY_RISK")
            legacy_items.append(
                {
                    "risk_id": item.get("risk_id") or f"risk_legacy_{index:06d}",
                    "module_code": "LEGACY_RESULT",
                    "risk_type": (
                        "DELETION_OR_MISSING"
                        if change_type in {"DELETED", "MISSING", "BLANK_OR_UNFILLED"}
                        else "ADDITION_OR_CHANGE"
                    ),
                    "change_type": change_type,
                    "title": item.get("title") or "历史风险项",
                    "description": item.get("description") or "该事项来自旧版结果。",
                    "source_evidence": item.get("sources") or [],
                    "related_diff_ids": item.get("related_diff_ids") or [],
                    "related_rule_ids": item.get("related_rule_ids") or [],
                    "requires_manual_action": True,
                }
            )
        converted["risk_items"] = legacy_items
        return converted
