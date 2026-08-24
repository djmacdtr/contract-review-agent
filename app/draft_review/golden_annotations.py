from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.comparison.engine import is_ocr_review_only_diff
from app.comparison.models import DiffItem
from app.documents.models import DocumentLocation, ParsedDocument
from app.documents.normalization import normalize_text
from app.draft_review.template_checks import TemplateReviewResult

AnnotationClassification = Literal[
    "RISK", "ALLOWED_FILL", "ALIGNMENT_FALSE_POSITIVE", "MANUAL_REVIEW"
]


class GoldenLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int | None = None
    paragraph_index: int | None = None
    table_index: int | None = None
    row: int | None = None
    column: int | None = None


class GoldenCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_type: str
    baseline_location: GoldenLocation | None = None
    target_location: GoldenLocation | None = None
    baseline_text_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    target_text_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    actual_outcome: Literal["RISK", "MANUAL_REVIEW"] | None = None
    classification: AnnotationClassification | None = None
    note: str | None = Field(default=None, max_length=1000)


class GoldenAnnotationSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format_version: Literal["1.0"] = "1.0"
    target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidates: list[GoldenCandidate]

    @model_validator(mode="after")
    def fingerprints_are_unique(self) -> GoldenAnnotationSet:
        fingerprints = [item.fingerprint for item in self.candidates]
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("golden candidate fingerprints must be unique")
        return self


class GoldenValidationSummary(BaseModel):
    candidate_count: int
    annotation_count: int
    missing_annotation_count: int
    stale_annotation_count: int
    suppressed_annotation_count: int
    classification_mismatch_count: int
    classification_counts: dict[str, int]

    @property
    def complete(self) -> bool:
        return (
            self.missing_annotation_count == 0
            and self.stale_annotation_count == 0
            and self.classification_mismatch_count == 0
        )


def _location(location: DocumentLocation | None) -> GoldenLocation | None:
    if location is None:
        return None
    return GoldenLocation.model_validate(
        location.model_dump(
            mode="json",
            include={"page", "paragraph_index", "table_index", "row", "column"},
        )
    )


def _text_hash(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()


def _fingerprint(payload: dict[str, object]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _diff_candidate(diff: DiffItem) -> GoldenCandidate:
    baseline_location = _location(diff.baseline.location if diff.baseline else None)
    target_location = _location(diff.target.location if diff.target else None)
    baseline_hash = _text_hash(diff.baseline.text if diff.baseline else None)
    target_hash = _text_hash(diff.target.text if diff.target else None)
    payload = {
        "candidate_type": diff.diff_type,
        "baseline_location": baseline_location.model_dump(mode="json")
        if baseline_location
        else None,
        "target_location": target_location.model_dump(mode="json") if target_location else None,
        "baseline_text_sha256": baseline_hash,
        "target_text_sha256": target_hash,
    }
    return GoldenCandidate(
        fingerprint=_fingerprint(payload),
        actual_outcome=("MANUAL_REVIEW" if is_ocr_review_only_diff(diff) else "RISK"),
        **payload,
    )


def _table_candidate(
    table_index: int,
    template: ParsedDocument,
    target: ParsedDocument,
) -> GoldenCandidate:
    baseline = next(
        (block for block in template.blocks if block.location.table_index == table_index), None
    )
    current = next(
        (block for block in target.blocks if block.location.table_index == table_index), None
    )
    baseline_location = GoldenLocation(table_index=table_index) if baseline else None
    target_location = GoldenLocation(table_index=table_index) if current else None
    baseline_hash = _text_hash(baseline.raw_text if baseline else None)
    target_hash = _text_hash(current.raw_text if current else None)
    payload = {
        "candidate_type": "TABLE_STRUCTURE_EXPANDED",
        "baseline_location": baseline_location.model_dump(mode="json")
        if baseline_location
        else None,
        "target_location": target_location.model_dump(mode="json") if target_location else None,
        "baseline_text_sha256": baseline_hash,
        "target_text_sha256": target_hash,
    }
    return GoldenCandidate(
        fingerprint=_fingerprint(payload), actual_outcome="MANUAL_REVIEW", **payload
    )


def build_annotation_candidates(
    template: ParsedDocument,
    target: ParsedDocument,
    review: TemplateReviewResult,
) -> GoldenAnnotationSet:
    candidates = [_diff_candidate(diff) for diff in review.diff_items]
    candidates.extend(
        _table_candidate(table_index, template, target)
        for table_index in review.diagnostics.expanded_table_indexes
    )
    return GoldenAnnotationSet(
        target_sha256=target.sha256,
        template_sha256=template.sha256,
        candidates=sorted(candidates, key=lambda item: item.fingerprint),
    )


def merge_existing_annotations(
    generated: GoldenAnnotationSet,
    existing: GoldenAnnotationSet | None,
) -> GoldenAnnotationSet:
    if existing is None:
        return generated
    existing_by_fingerprint = {item.fingerprint: item for item in existing.candidates}
    candidates = []
    for candidate in generated.candidates:
        old = existing_by_fingerprint.get(candidate.fingerprint)
        candidates.append(
            candidate.model_copy(
                update={
                    "classification": old.classification if old else None,
                    "note": old.note if old else None,
                }
            )
        )
    return generated.model_copy(update={"candidates": candidates})


def validate_annotations(
    actual: GoldenAnnotationSet,
    annotated: GoldenAnnotationSet,
) -> GoldenValidationSummary:
    if actual.target_sha256 != annotated.target_sha256:
        raise ValueError("target file SHA-256 does not match the annotation set")
    if actual.template_sha256 != annotated.template_sha256:
        raise ValueError("template file SHA-256 does not match the annotation set")
    actual_fingerprints = {item.fingerprint for item in actual.candidates}
    actual_by_fingerprint = {item.fingerprint: item for item in actual.candidates}
    annotated_by_fingerprint = {item.fingerprint: item for item in annotated.candidates}
    missing = [
        fingerprint
        for fingerprint in actual_fingerprints
        if fingerprint not in annotated_by_fingerprint
        or annotated_by_fingerprint[fingerprint].classification is None
    ]
    absent = set(annotated_by_fingerprint) - actual_fingerprints
    suppressed = {
        fingerprint
        for fingerprint in absent
        if annotated_by_fingerprint[fingerprint].classification
        in {"ALLOWED_FILL", "ALIGNMENT_FALSE_POSITIVE"}
    }
    stale = absent - suppressed
    mismatches = [
        fingerprint
        for fingerprint in actual_fingerprints & set(annotated_by_fingerprint)
        if annotated_by_fingerprint[fingerprint].classification is not None
        and annotated_by_fingerprint[fingerprint].classification
        != actual_by_fingerprint[fingerprint].actual_outcome
    ]
    counts: dict[str, int] = {}
    for item in annotated.candidates:
        if item.fingerprint not in actual_fingerprints or item.classification is None:
            continue
        counts[item.classification] = counts.get(item.classification, 0) + 1
    return GoldenValidationSummary(
        candidate_count=len(actual_fingerprints),
        annotation_count=sum(counts.values()),
        missing_annotation_count=len(missing),
        stale_annotation_count=len(stale),
        suppressed_annotation_count=len(suppressed),
        classification_mismatch_count=len(mismatches),
        classification_counts=counts,
    )


def load_annotation_set(path: Path) -> GoldenAnnotationSet:
    return GoldenAnnotationSet.model_validate_json(path.read_text(encoding="utf-8"))
