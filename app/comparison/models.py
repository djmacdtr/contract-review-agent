from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.documents.models import DocumentLocation, ProcessingWarning


class DiffSide(BaseModel):
    file_id: str
    location: DocumentLocation
    locations: list[DocumentLocation] = Field(default_factory=list)
    text: str


class DiffSegment(BaseModel):
    operation: Literal["EQUAL", "DELETE", "INSERT"]
    text: str


class DiffItem(BaseModel):
    diff_id: str
    diff_type: Literal[
        "ADDED",
        "DELETED",
        "MODIFIED",
        "NUMERIC_CHANGED",
        "TABLE_ROW_ADDED",
        "TABLE_ROW_DELETED",
        "TABLE_CELL_CHANGED",
    ]
    severity: Literal["HIGH", "MEDIUM", "LOW", "INFO"]
    title: str
    baseline: DiffSide | None
    target: DiffSide | None
    segments: list[DiffSegment] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    requires_manual_review: bool = True


class ComparisonDiagnostics(BaseModel):
    reliable: bool
    baseline_unit_count: int
    target_unit_count: int
    aligned_unit_count: int
    unmatched_baseline_count: int
    unmatched_target_count: int
    alignment_coverage_baseline: float = Field(ge=0, le=1)
    alignment_coverage_target: float = Field(ge=0, le=1)
    unmatched_ratio_baseline: float = Field(ge=0, le=1)
    unmatched_ratio_target: float = Field(ge=0, le=1)
    global_text_similarity: float = Field(ge=0, le=1)
    candidate_diff_count: int
    emitted_diff_count: int
    compatible_table_count: int
    fallback_mode: str
    reasons: list[str] = Field(default_factory=list)


class ComparisonResult(BaseModel):
    diff_items: list[DiffItem]
    warnings: list[ProcessingWarning] = Field(default_factory=list)
    diagnostics: ComparisonDiagnostics
