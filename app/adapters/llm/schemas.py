from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.documents.models import DocumentLocation


class StrictLlmSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


ValueType = Literal[
    "TEXT",
    "MONEY",
    "DATE",
    "PERCENTAGE",
    "RATE",
    "DURATION",
    "NUMBER",
    "QUANTITY",
    "ENTITY",
    "IDENTIFIER",
    "UNKNOWN",
]


class SemanticConcept(StrictLlmSchema):
    """Model-discovered meaning shared by one or more document facts."""

    concept_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    display_name: str = Field(min_length=1, max_length=100)
    value_type: ValueType
    aliases: list[str] = Field(default_factory=list, max_length=20)
    evidence_locations: list[DocumentLocation] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1)


class ValidationSpec(StrictLlmSchema):
    """A dynamic validation plan; ``expression`` is checked by the numeric AST validator."""

    validation_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    display_name: str = Field(min_length=1, max_length=160)
    expression: dict[str, Any]
    evidence_locations: list[DocumentLocation] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1)

    @field_validator("expression")
    @classmethod
    def validate_expression(cls, value: dict[str, Any]) -> dict[str, Any]:
        from app.draft_review.numeric_rules import NumericAstError, validate_ast

        try:
            validate_ast(value)
        except NumericAstError as exc:
            raise ValueError(str(exc)) from exc
        return value


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
    concept_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")
    display_name: str = Field(min_length=1, max_length=100)
    value_type: ValueType
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
    semantic_concepts: list[SemanticConcept] = Field(default_factory=list)
    validation_specs: list[ValidationSpec] = Field(default_factory=list)

    @field_validator("missing_field_keys")
    @classmethod
    def validate_missing_keys(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("missing_field_keys must be unique")
        return values


class FactReviewDecision(StrictLlmSchema):
    field_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    source_file_id: str = Field(min_length=1)
    location: DocumentLocation
    decision: Literal["ACCEPT", "REJECT", "UNCERTAIN"]
    evidence_text: str = Field(default="", max_length=8000)
    confidence: float = Field(ge=0, le=1)
    reason_code: str = Field(min_length=1, max_length=80)


class FactReview(StrictLlmSchema):
    file_id: str = Field(min_length=1)
    decisions: list[FactReviewDecision] = Field(default_factory=list)
    semantic_concepts: list[SemanticConcept] = Field(default_factory=list)
    validation_specs: list[ValidationSpec] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    evidence_complete: bool


class FactMapping(StrictLlmSchema):
    """A proposed semantic match from one reference fact to one target fact."""

    target_fact_id: str = Field(pattern=r"^target_fact_\d{6}$")
    reference_field_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    source_file_id: str = Field(min_length=1)
    reference_location: DocumentLocation
    decision: Literal["MATCH", "UNCERTAIN"]
    confidence: float = Field(ge=0, le=1)
    reason_code: str = Field(min_length=1, max_length=80)


class MissingRequirement(StrictLlmSchema):
    """A dynamic plan saying absence of a target fact in this source needs review."""

    target_fact_id: str = Field(pattern=r"^target_fact_\d{6}$")
    confidence: float = Field(ge=0, le=1)
    reason_code: str = Field(min_length=1, max_length=80)


class FactMappingResponse(StrictLlmSchema):
    reference_file_id: str = Field(min_length=1)
    mappings: list[FactMapping] = Field(default_factory=list)
    missing_requirements: list[MissingRequirement] = Field(default_factory=list)


class FactMappingReviewDecision(StrictLlmSchema):
    target_fact_id: str = Field(pattern=r"^target_fact_\d{6}$")
    reference_field_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    source_file_id: str = Field(min_length=1)
    reference_location: DocumentLocation
    decision: Literal["ACCEPT", "REJECT", "UNCERTAIN"]
    confidence: float = Field(ge=0, le=1)
    reason_code: str = Field(min_length=1, max_length=80)


class MissingRequirementReviewDecision(StrictLlmSchema):
    target_fact_id: str = Field(pattern=r"^target_fact_\d{6}$")
    decision: Literal["ACCEPT", "REJECT", "UNCERTAIN"]
    confidence: float = Field(ge=0, le=1)
    reason_code: str = Field(min_length=1, max_length=80)


class FactMappingReview(StrictLlmSchema):
    reference_file_id: str = Field(min_length=1)
    decisions: list[FactMappingReviewDecision] = Field(default_factory=list)
    missing_requirement_decisions: list[MissingRequirementReviewDecision] = Field(
        default_factory=list
    )
    confidence: float = Field(ge=0, le=1)
    evidence_complete: bool


class AdviceEvidence(StrictLlmSchema):
    file_id: str = Field(min_length=1)
    location: DocumentLocation


class RiskAdvice(StrictLlmSchema):
    risk_id: str = Field(min_length=1, max_length=160)
    analysis_advice: str = Field(min_length=1, max_length=2000)


class AdviceResponse(StrictLlmSchema):
    overall_advice: str = Field(min_length=1, max_length=4000)
    priority_actions: list[str] = Field(default_factory=list, max_length=20)
    manual_review_focus: list[str] = Field(default_factory=list, max_length=20)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    evidence_refs: list[AdviceEvidence] = Field(default_factory=list, max_length=50)
    risk_advices: list[RiskAdvice] = Field(max_length=500)
