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


class MissingDetail(BaseModel):
    boundary: Literal["START", "MIDDLE", "END"]
    estimated_page_equivalent: float | None = Field(default=None, ge=0)
    baseline_page_start: int | None = Field(default=None, ge=1)
    baseline_page_end: int | None = Field(default=None, ge=1)
    target_anchor_before_page: int | None = Field(default=None, ge=1)
    target_anchor_after_page: int | None = Field(default=None, ge=1)
    structure_unit_count: int = Field(ge=1)
    aggregated_diff_count: int = Field(ge=1)
    content_summary: str


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
        "TABLE_STRUCTURE_EXPANDED",
        "PAGE_MISSING",
        "CONTENT_BLOCK_MISSING",
    ]
    title: str
    baseline: DiffSide | None
    target: DiffSide | None
    segments: list[DiffSegment] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    requires_manual_review: bool = True
    review_reason: Literal[
        "OCR_SINGLE_CHAR_VARIANCE",
        "OCR_PLACEHOLDER_VARIANCE",
        "OCR_READING_ORDER_VARIANCE",
        "OCR_LOW_CONFIDENCE_VARIANCE",
    ] | None = None
    certainty: Literal["CONFIRMED", "INFERRED"] | None = None
    missing_detail: MissingDetail | None = None


class ComparisonDiagnostics(BaseModel):
    reliable: bool
    baseline_unit_count: int
    target_unit_count: int
    aligned_unit_count: int
    unmatched_baseline_count: int
    unmatched_target_count: int
    alignment_coverage_baseline: float = Field(ge=0, le=1)
    alignment_coverage_target: float = Field(ge=0, le=1)
    effective_alignment_coverage_baseline: float = Field(default=0, ge=0, le=1)
    explained_missing_baseline_count: int = Field(default=0, ge=0)
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
