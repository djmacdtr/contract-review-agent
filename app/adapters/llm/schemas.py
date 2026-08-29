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
    display_name: str = Field(min_length=1, max_length=80)
    value_type: ValueType
    aliases: list[str] = Field(default_factory=list, max_length=8)
    evidence_locations: list[DocumentLocation] = Field(default_factory=list, max_length=8)
    confidence: float = Field(ge=0, le=1)


class ValidationSpec(StrictLlmSchema):
    """A dynamic validation plan; ``expression`` is checked by the numeric AST validator."""

    validation_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    display_name: str = Field(min_length=1, max_length=80)
    expression: dict[str, Any]
    evidence_locations: list[DocumentLocation] = Field(default_factory=list, max_length=8)
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


class SemanticFactRef(StrictLlmSchema):
    """Internal, unambiguous reference to one verified fact."""

    fact_id: str = Field(pattern=r"^fact_[0-9a-f]{24}$")
    source_file_id: str = Field(min_length=1, max_length=160)


class SemanticEvidenceRef(StrictLlmSchema):
    """Internal evidence reference that keeps the owning file explicit."""

    source_file_id: str = Field(min_length=1, max_length=160)
    location: DocumentLocation


class SemanticConceptPlan(StrictLlmSchema):
    """Internal semantic concept plan; never exposed as a public result field."""

    concept_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    display_name: str = Field(min_length=1, max_length=80)
    value_type: ValueType
    aliases: list[str] = Field(default_factory=list, max_length=8)
    fact_refs: list[SemanticFactRef] = Field(min_length=1, max_length=512)
    evidence_refs: list[SemanticEvidenceRef] = Field(min_length=1, max_length=512)
    confidence: float = Field(ge=0, le=1)


class SemanticValidationSpec(StrictLlmSchema):
    """Internal numeric rule plan with file-qualified fact references."""

    validation_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    display_name: str = Field(min_length=1, max_length=80)
    expression: dict[str, Any]
    evidence_refs: list[SemanticEvidenceRef] = Field(min_length=1, max_length=512)
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


class CompactDocumentProfile(StrictLlmSchema):
    """Small wire representation used by the first extraction phase."""

    file_id: str = Field(min_length=1, max_length=160)
    document_kind: str = Field(min_length=1, max_length=80)
    title: str | None = Field(default=None, max_length=160)
    confidence: float = Field(ge=0, le=1)
    evidence_locations: list[DocumentLocation] = Field(min_length=1, max_length=4)

    @field_validator("document_kind")
    @classmethod
    def normalize_document_kind(cls, value: str) -> str:
        return value.strip() or "UNKNOWN"


class CompactDocumentOverview(StrictLlmSchema):
    """One-time document overview response without program-owned identity."""

    document_kind: str = Field(min_length=1, max_length=80)
    title: str | None = Field(default=None, max_length=160)
    confidence: float = Field(ge=0, le=1)
    evidence_locations: list[DocumentLocation] = Field(min_length=1, max_length=8)

    @field_validator("document_kind")
    @classmethod
    def normalize_document_kind(cls, value: str) -> str:
        return value.strip() or "UNKNOWN"


class NumericCandidateDecision(StrictLlmSchema):
    """Explicit disposition for one numeric candidate in a fact batch."""

    candidate_index: int = Field(ge=1, le=128)
    decision: Literal["FACT", "IGNORE"]
    reason_code: str = Field(min_length=1, max_length=40)


class NumericCandidateItem(StrictLlmSchema):
    """The model-owned part of one numeric candidate decision.

    Candidate text, evidence and location deliberately do not cross the model
    boundary.  The extractor only returns the index and its interpretation;
    the program rehydrates the candidate from the input payload.
    """

    candidate_id: str | None = Field(default=None, pattern=r"^numeric_[0-9a-f]{16}$")
    candidate_index: int | None = Field(default=None, ge=1, le=24)
    semantic_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    display_name: str = Field(min_length=1, max_length=80)
    value_type: ValueType
    normalized_meaning: str | None = Field(default=None, max_length=256)
    requires_cross_document_validation: bool = True
    decision: Literal["FACT", "IGNORE"]
    reason_code: str = Field(min_length=1, max_length=40)
    confidence: float = Field(ge=0, le=1)


class NumericCandidateExtraction(StrictLlmSchema):
    """Strict numeric-candidate chain response."""

    items: list[NumericCandidateItem] = Field(..., max_length=24)


class TextFactItem(StrictLlmSchema):
    """A compact non-numeric fact grounded to one input structure unit."""

    unit_id: str = Field(pattern=r"^unit_[0-9a-f]{8,64}$")
    semantic_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    display_name: str = Field(min_length=1, max_length=80)
    value_type: ValueType
    quote: str | None = Field(default=None, min_length=1, max_length=4000)
    confidence: float = Field(ge=0, le=1)


class TextFactExtraction(StrictLlmSchema):
    """Strict non-numeric fact chain response."""

    items: list[TextFactItem] = Field(..., max_length=12)
    has_more: bool


# Descriptive aliases used by the extraction controller and tests.  Keeping
# one canonical model avoids divergent public/internal schemas.
NumericCandidateDecisionResponse = NumericCandidateExtraction
NonNumericFactExtraction = TextFactExtraction


class CompactFactCandidate(StrictLlmSchema):
    """Fact wire shape; evidence is recovered deterministically from ``location``."""

    field_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    concept_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")
    display_name: str = Field(min_length=1, max_length=80)
    value_type: ValueType
    raw_value: str = Field(min_length=1, max_length=256)
    location: DocumentLocation
    confidence: float = Field(ge=0, le=1)
    candidate_indices: list[int] = Field(default_factory=list, max_length=8)


class CompactFactBatchExtraction(StrictLlmSchema):
    """Fact-only wire response; evidence and identity are restored by the program."""

    facts: list[CompactFactCandidate] = Field(default_factory=list, max_length=24)
    numeric_candidate_decisions: list[NumericCandidateDecision] = Field(
        default_factory=list, max_length=48
    )


class CompactDocumentFactExtraction(StrictLlmSchema):
    """First-phase response without repeated evidence or semantic planning fields."""

    profile: CompactDocumentProfile
    facts: list[CompactFactCandidate] = Field(default_factory=list, max_length=64)


class SemanticPlanResponse(StrictLlmSchema):
    """Internal second-phase response for concepts and declarative numeric rules."""

    file_id: str = Field(min_length=1, max_length=160)
    semantic_concepts: list[SemanticConceptPlan] = Field(default_factory=list, max_length=512)
    validation_specs: list[SemanticValidationSpec] = Field(default_factory=list, max_length=512)


class CompactSemanticConceptPlan(StrictLlmSchema):
    """Wire-only semantic concept; evidence is rehydrated from verified facts."""

    concept_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    display_name: str = Field(min_length=1, max_length=80)
    value_type: ValueType
    aliases: list[str] = Field(default_factory=list, max_length=8)
    fact_ids: list[str] = Field(min_length=1, max_length=512)
    confidence: float = Field(ge=0, le=1)


class CompactSemanticValidationSpec(StrictLlmSchema):
    """Wire-only numeric rule; evidence is derived from qualified AST facts."""

    validation_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    display_name: str = Field(min_length=1, max_length=80)
    expression: dict[str, Any]
    confidence: float = Field(ge=0, le=1)


class CompactSemanticPlanResponse(StrictLlmSchema):
    """Wire response that keeps model output small and program-owned."""

    file_id: str = Field(min_length=1, max_length=160)
    semantic_concepts: list[CompactSemanticConceptPlan] = Field(
        default_factory=list, max_length=512
    )
    validation_specs: list[CompactSemanticValidationSpec] = Field(
        default_factory=list, max_length=512
    )


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
    requires_cross_document_validation: bool = True


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
    decisions: list[FactReviewDecision] = Field(min_length=1)
    semantic_concepts: list[SemanticConcept] = Field(default_factory=list)
    validation_specs: list[ValidationSpec] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    evidence_complete: bool


class CompactFactReviewDecision(StrictLlmSchema):
    """Wire-only review decision addressed by the program-owned fact index."""

    fact_index: int = Field(ge=1, le=512)
    decision: Literal["ACCEPT", "REJECT", "UNCERTAIN"]
    confidence: float = Field(ge=0, le=1)
    reason_code: str = Field(min_length=1, max_length=80)


class CompactFactReview(StrictLlmSchema):
    """Small review response; identity and evidence are rehydrated in code."""

    decisions: list[CompactFactReviewDecision] = Field(min_length=1, max_length=512)
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


class FactRelationDecision(StrictLlmSchema):
    """ID-only relation over program-selected facts."""

    target_fact_id: str = Field(pattern=r"^target_fact_\d{6}$")
    reference_fact_id: str = Field(pattern=r"^fact_[0-9a-f]{24}$")
    decision: Literal["MATCH", "CONFLICT", "UNRELATED", "UNCERTAIN"]
    confidence: float = Field(ge=0, le=1)
    reason_code: str = Field(min_length=1, max_length=80)


class FactRelationBatchResponse(StrictLlmSchema):
    items: list[FactRelationDecision] = Field(default_factory=list, max_length=64)


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


class CrossValidationDecision(StrictLlmSchema):
    """One decision for one program-generated cross-document candidate group."""

    candidate_id: str = Field(pattern=r"^cross_[0-9a-f]{20}$")
    decision: Literal["MATCH", "CONFLICT", "UNRELATED", "UNCERTAIN"]
    reason: str = Field(min_length=1, max_length=160)


class CrossValidationResponse(StrictLlmSchema):
    """Bounded best-effort response for the KISS delivery path."""

    items: list[CrossValidationDecision] = Field(default_factory=list, max_length=20)


class AdviceEvidence(StrictLlmSchema):
    file_id: str = Field(min_length=1)
    location: DocumentLocation


class RiskAdvice(StrictLlmSchema):
    risk_id: str = Field(min_length=1, max_length=160)
    analysis_advice: str = Field(min_length=1, max_length=240)


class AdviceResponse(StrictLlmSchema):
    overall_advice: str = Field(min_length=1, max_length=4000)
    priority_actions: list[str] = Field(default_factory=list, max_length=20)
    manual_review_focus: list[str] = Field(default_factory=list, max_length=20)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    evidence_refs: list[AdviceEvidence] = Field(default_factory=list, max_length=50)
    risk_advices: list[RiskAdvice] = Field(max_length=500)
