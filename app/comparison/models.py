from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.documents.models import DocumentLocation, ProcessingWarning


class DiffSide(BaseModel):
    file_id: str
    location: DocumentLocation
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


class ComparisonResult(BaseModel):
    diff_items: list[DiffItem]
    warnings: list[ProcessingWarning] = Field(default_factory=list)
