from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.documents.models import DocumentLocation


class StrictLlmSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentProfile(StrictLlmSchema):
    file_id: str = Field(min_length=1)
    document_kind: str = Field(
        min_length=1,
        max_length=100,
        description="开放式文档用途；无法可靠判断时使用 UNKNOWN",
    )
    title: str | None = Field(default=None, max_length=500)
    confidence: float = Field(ge=0, le=1)
    evidence_locations: list[DocumentLocation] = Field(min_length=1)

    @field_validator("document_kind")
    @classmethod
    def normalize_document_kind(cls, value: str) -> str:
        return value.strip() or "UNKNOWN"


class FactCandidate(StrictLlmSchema):
    field_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    display_name: str = Field(min_length=1, max_length=100)
    value_type: Literal[
        "TEXT", "MONEY", "DATE", "PERCENTAGE", "DURATION", "ENTITY", "IDENTIFIER", "UNKNOWN"
    ]
    raw_value: str = Field(min_length=1, max_length=4000)
    normalized_hint: str | None = Field(default=None, max_length=4000)
    source_file_id: str = Field(min_length=1)
    evidence_text: str = Field(min_length=1, max_length=8000)
    location: DocumentLocation
    confidence: float = Field(ge=0, le=1)


class DocumentFactExtraction(StrictLlmSchema):
    profile: DocumentProfile
    facts: list[FactCandidate] = Field(default_factory=list)
    missing_field_keys: list[str] = Field(default_factory=list)

    @field_validator("missing_field_keys")
    @classmethod
    def validate_missing_keys(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("missing_field_keys must be unique")
        return values
