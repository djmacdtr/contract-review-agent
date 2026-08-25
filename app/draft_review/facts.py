from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.adapters.llm.schemas import (
    CompactDocumentFactExtraction,
    DocumentFactExtraction,
    DocumentProfile,
    FactCandidate,
    FactReview,
    SemanticConcept,
    SemanticEvidenceRef,
    SemanticPlanResponse,
    ValidationSpec,
)
from app.comparison.models import DiffItem, DiffSegment, DiffSide
from app.documents.models import DocumentBlock, DocumentLocation, ParsedDocument
from app.documents.normalization import normalize_text


class EvidenceValidationError(ValueError):
    def __init__(
        self, message: str, *, code: str = "LLM_RESPONSE_SCHEMA_INVALID"
    ) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FactIndexEntry:
    fact_id: str
    fact: FactCandidate

    @property
    def source_file_id(self) -> str:
        return self.fact.source_file_id

    @property
    def normalized_value(self) -> str:
        return normalize_fact(self.fact)

    @property
    def evidence_ref(self) -> dict[str, Any]:
        return {
            "source_file_id": self.fact.source_file_id,
            "location": self.fact.location.model_dump(mode="json", exclude_none=True),
        }


FactIndex = dict[tuple[str, str], FactIndexEntry]


NUMERIC_VALUE_TYPES = {
    "MONEY",
    "PERCENTAGE",
    "RATE",
    "DURATION",
    "NUMBER",
    "QUANTITY",
}

_NUMBER_TOKEN = r"[-+]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?"
_NUMERIC_CANDIDATE_PATTERNS = (
    (
        "IDENTIFIER",
        re.compile(r"(?:编号|代码|证号)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{2,})"),
        0,
        100,
    ),
    (
        "DATE",
        re.compile(r"\d{4}[年./-]\d{1,2}[月./-]\d{1,2}日?"),
        0,
        90,
    ),
    (
        "PERCENTAGE",
        re.compile(rf"{_NUMBER_TOKEN}\s*(?:%|％|百分之)"),
        0,
        80,
    ),
    (
        "MONEY",
        re.compile(rf"{_NUMBER_TOKEN}\s*(?:人民币|元|万元|亿元|万|亿|CNY|RMB|￥|¥)"),
        0,
        75,
    ),
    (
        "DURATION",
        re.compile(rf"{_NUMBER_TOKEN}\s*(?:年|个月|月|周|星期|天|日)"),
        0,
        70,
    ),
    (
        "QUANTITY",
        re.compile(rf"{_NUMBER_TOKEN}\s*(?:台|件|个|套|期|BP|基点)"),
        0,
        60,
    ),
    ("NUMBER", re.compile(_NUMBER_TOKEN), 0, 10),
)
_STRUCTURAL_NUMBER_CONTEXT = re.compile(
    r"(?:第|条|款|项|章节|页码?|编号|序号|目录|no\.?|no：?)\s*$",
    flags=re.IGNORECASE,
)
MAX_COMPACT_FACTS = 64
MAX_NUMERIC_CANDIDATES_PER_CHUNK = 48


def fact_identity_key(fact: FactCandidate) -> tuple[object, ...]:
    return (
        fact.field_key,
        fact.source_file_id,
        location_key(fact.location),
        fact.value_type,
        normalize_text(fact.raw_value),
    )


def stable_fact_id(fact: FactCandidate) -> str:
    canonical = {
        "source_file_id": fact.source_file_id,
        "field_key": fact.field_key,
        "location": location_key(fact.location),
        "value_type": fact.value_type,
        "raw_value": normalize_text(fact.raw_value),
    }
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
    return f"fact_{digest}"


def build_fact_index(extractions: dict[str, DocumentFactExtraction]) -> FactIndex:
    index: FactIndex = {}
    for file_id, extraction in extractions.items():
        for fact in extraction.facts:
            if fact.source_file_id != file_id:
                raise EvidenceValidationError(
                    "fact index source_file_id does not match extraction",
                    code="FILE_ID_MISMATCH",
                )
            fact_id = stable_fact_id(fact)
            ref = (fact_id, fact.source_file_id)
            existing = index.get(ref)
            if existing is None:
                index[ref] = FactIndexEntry(fact_id=fact_id, fact=fact)
                continue
            if fact_identity_key(existing.fact) != fact_identity_key(fact):
                raise EvidenceValidationError(
                    "stable fact_id collision", code="FACT_IDENTITY_DUPLICATED"
                )
            # Chunked extraction can repeat an identical fact. Keep one stable
            # identity while retaining the strongest grounded model confidence.
            if fact.confidence > existing.fact.confidence:
                index[ref] = FactIndexEntry(fact_id=fact_id, fact=fact)
    return index


def fact_index_payload(
    index: FactIndex,
    accepted_refs: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    selected = accepted_refs if accepted_refs is not None else set(index)
    payload: list[dict[str, Any]] = []
    for ref in sorted(selected):
        entry = index.get(ref)
        if entry is None:
            continue
        payload.append(
            {
                "fact_id": entry.fact_id,
                **entry.fact.model_dump(
                    mode="json", exclude={"evidence_text", "normalized_hint"}
                ),
            }
        )
    return payload


def accepted_fact_refs(
    extraction: DocumentFactExtraction,
    review: FactReview | None,
    min_confidence: float,
) -> set[tuple[str, str]]:
    if (
        review is None
        or review.file_id != extraction.profile.file_id
        or not review.evidence_complete
        or review.confidence < min_confidence
    ):
        return set()
    expected_keys = {
        (fact.field_key, fact.source_file_id, location_key(fact.location))
        for fact in extraction.facts
    }
    decision_keys = [
        (decision.field_key, decision.source_file_id, location_key(decision.location))
        for decision in review.decisions
    ]
    if len(decision_keys) != len(set(decision_keys)) or set(decision_keys) != expected_keys:
        return set()
    decisions = {
        (
            decision.field_key,
            decision.source_file_id,
            location_key(decision.location),
        ): decision
        for decision in review.decisions
    }
    accepted: set[tuple[str, str]] = set()
    for fact in extraction.facts:
        key = (fact.field_key, fact.source_file_id, location_key(fact.location))
        decision = decisions.get(key)
        if (
            decision is not None
            and decision.decision == "ACCEPT"
            and decision.confidence >= min_confidence
            and fact.confidence >= min_confidence
        ):
            accepted.add((stable_fact_id(fact), fact.source_file_id))
    return accepted


def verified_fact_index(
    extractions: dict[str, DocumentFactExtraction],
    reviews: dict[str, FactReview | None],
    min_confidence: float,
) -> tuple[FactIndex, set[tuple[str, str]]]:
    index = build_fact_index(extractions)
    accepted: set[tuple[str, str]] = set()
    for file_id, extraction in extractions.items():
        accepted.update(
            accepted_fact_refs(extraction, reviews.get(file_id), min_confidence)
        )
    return index, accepted


def compact_location(location: DocumentLocation) -> dict[str, Any]:
    return {
        key: value
        for key, value in location.model_dump(mode="json", exclude_none=True).items()
        if key in {"page", "paragraph_index", "table_index", "row", "column"}
    }


def target_fact_catalog(extraction: DocumentFactExtraction) -> list[dict[str, Any]]:
    return [
        {
            "target_fact_id": f"target_fact_{index:06d}",
            **fact.model_dump(mode="json"),
        }
        for index, fact in enumerate(extraction.facts, start=1)
    ]


def numeric_candidates(
    blocks: list[DocumentBlock],
) -> list[dict[str, Any]]:
    selected, _metrics = _scan_numeric_candidates(blocks)
    return selected


def _scan_numeric_candidates(
    blocks: list[DocumentBlock],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw_candidates: list[dict[str, Any]] = []
    structural_suppressed = 0
    for block in blocks:
        text = block.raw_text
        for kind, pattern, group_index, priority in _NUMERIC_CANDIDATE_PATTERNS:
            for match in pattern.finditer(text):
                start, end = match.span(group_index)
                raw_value = match.group(group_index)
                prefix = text[max(0, start - 8) : start]
                structural = bool(_STRUCTURAL_NUMBER_CONTEXT.search(prefix))
                if structural and kind != "IDENTIFIER":
                    structural_suppressed += 1
                    continue
                raw_candidates.append(
                {
                    "raw_value": raw_value,
                    "candidate_kind": kind,
                    "location": compact_location(block.location),
                    "span": {"start": start, "end": end},
                    "_priority": priority,
                    "_block_id": block.block_id,
                }
            )
    selected: list[dict[str, Any]] = []
    duplicate_suppressed = 0
    for candidate in sorted(
        raw_candidates,
        key=lambda item: (
            item["_block_id"],
            item["span"]["start"],
            -item["_priority"],
            item["span"]["end"],
        ),
    ):
        duplicate = False
        for existing in selected:
            if existing["_block_id"] != candidate["_block_id"]:
                continue
            existing_span = existing["span"]
            candidate_span = candidate["span"]
            overlaps = not (
                candidate_span["end"] <= existing_span["start"]
                or candidate_span["start"] >= existing_span["end"]
            )
            if overlaps and existing["_priority"] >= candidate["_priority"]:
                duplicate = True
                break
        if duplicate:
            duplicate_suppressed += 1
            continue
        canonical = {
            "block_id": candidate["_block_id"],
            "location": candidate["location"],
            "span": candidate["span"],
            "kind": candidate["candidate_kind"],
        }
        candidate_id = hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        selected.append(
            {
                "candidate_id": f"numeric_{candidate_id}",
                "raw_value": candidate["raw_value"],
                "candidate_kind": candidate["candidate_kind"],
                "location": candidate["location"],
                "span": candidate["span"],
                "_priority": candidate["_priority"],
                "_block_id": candidate["_block_id"],
            }
        )
    type_counts: dict[str, int] = {}
    for candidate in selected:
        kind = candidate["candidate_kind"]
        type_counts[kind] = type_counts.get(kind, 0) + 1
    public_selected = [
        {
            key: candidate[key]
            for key in ("candidate_id", "raw_value", "candidate_kind", "location", "span")
        }
        for candidate in selected
    ]
    return public_selected, {
        "candidate_total": len(raw_candidates) + structural_suppressed,
        "candidate_unique": len(public_selected),
        "suppressed_count": structural_suppressed + duplicate_suppressed,
        "structural_suppressed_count": structural_suppressed,
        "duplicate_suppressed_count": duplicate_suppressed,
        "type_counts": type_counts,
    }


def numeric_candidate_metrics(blocks: list[DocumentBlock]) -> dict[str, Any]:
    """Return aggregate candidate metrics without exposing candidate text."""

    _selected, metrics = _scan_numeric_candidates(blocks)
    metrics["batch_count"] = 1 if blocks else 0
    return metrics


def compact_extraction_payload(
    document: ParsedDocument,
    blocks: list[DocumentBlock],
) -> dict[str, Any]:
    payload = chunk_payload(document, blocks)
    payload["blocks"] = [
        {**item, "location": compact_location(block.location)}
        for block, item in zip(blocks, payload["blocks"], strict=True)
    ]
    evidence_blocks = list(payload["blocks"])
    for block in blocks:
        if block.table is None:
            continue
        for row in block.table.rows:
            for column, cell in enumerate(row.cells):
                location = cell.location.model_copy(
                    update={
                        "table_index": block.table.table_index,
                        "row": row.row,
                        "column": (
                            cell.location.column
                            if cell.location.column is not None
                            else column
                        ),
                    }
                )
                evidence_blocks.append(
                    {
                        "block_id": (
                            f"{block.block_id}_r{row.row:04d}_c{location.column or column:04d}"
                        ),
                        "type": "TABLE_CELL",
                        "text": cell.raw_text,
                        "location": compact_location(location),
                    }
                )
    payload["evidence_blocks"] = evidence_blocks
    payload["numeric_candidates"] = numeric_candidates(blocks)
    payload["numeric_candidate_metrics"] = numeric_candidate_metrics(blocks)
    payload["extraction_requirements"] = {
        "open_ended_field_keys": True,
        "classify_numeric_candidates": True,
        "facts_max_items": MAX_COMPACT_FACTS,
        "return_only_profile_and_facts": True,
    }
    return payload


def chunk_document(
    document: ParsedDocument,
    max_chars: int,
    *,
    max_numeric_candidates: int | None = None,
) -> list[list[DocumentBlock]]:
    chunks: list[list[DocumentBlock]] = []
    current: list[DocumentBlock] = []
    current_chars = 0
    current_numeric_candidates = 0
    for block in document.blocks:
        block_chars = len(block.raw_text)
        block_numeric_candidates = len(numeric_candidates([block]))
        over_numeric_limit = (
            max_numeric_candidates is not None
            and current_numeric_candidates + block_numeric_candidates > max_numeric_candidates
        )
        if current and (current_chars + block_chars > max_chars or over_numeric_limit):
            chunks.append(current)
            current = []
            current_chars = 0
            current_numeric_candidates = 0
        current.append(block)
        current_chars += block_chars
        current_numeric_candidates += block_numeric_candidates
    if current:
        chunks.append(current)
    return chunks or [[]]


def chunk_payload(document: ParsedDocument, blocks: list[DocumentBlock]) -> dict[str, Any]:
    return {
        "file_id": document.file_id,
        "role": document.role,
        "blocks": [
            {
                "block_id": block.block_id,
                "type": block.type,
                "text": block.raw_text,
                "location": block.location.model_dump(mode="json", exclude_none=True),
            }
            for block in blocks
        ],
    }


def expand_compact_extraction(
    payload: dict[str, Any],
    compact: CompactDocumentFactExtraction,
) -> DocumentFactExtraction:
    """Recover full fact evidence from the input payload without model duplication."""

    file_id = payload.get("file_id")
    if compact.profile.file_id != file_id:
        raise EvidenceValidationError(
            "compact profile file_id does not match payload", code="FILE_ID_MISMATCH"
        )
    evidence_by_location: dict[tuple[object, ...], list[str]] = defaultdict(list)
    for item in payload.get("evidence_blocks", payload.get("blocks", [])):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        try:
            key = location_key(item["location"])
        except (TypeError, ValueError):
            continue
        evidence_by_location[key].append(item["text"])

    profile_locations = compact.profile.evidence_locations
    if any(location_key(location) not in evidence_by_location for location in profile_locations):
        raise EvidenceValidationError(
            "compact profile evidence location does not exist",
            code="PROFILE_LOCATION_NOT_FOUND",
        )

    facts: list[FactCandidate] = []
    seen_facts: set[tuple[object, ...]] = set()
    for compact_fact in compact.facts:
        fact_location = location_key(compact_fact.location)
        candidates = evidence_by_location.get(fact_location, [])
        if not candidates:
            raise EvidenceValidationError(
                "compact fact location does not exist", code="FACT_LOCATION_NOT_FOUND"
            )
        raw_value = normalize_text(compact_fact.raw_value)
        evidence_text = next(
            (
                candidate
                for candidate in candidates
                if raw_value and raw_value in normalize_text(candidate)
            ),
            None,
        )
        if evidence_text is None:
            raise EvidenceValidationError(
                "compact fact raw_value is not grounded at its location",
                code="FACT_VALUE_NOT_GROUNDED",
            )
        identity = (
            compact_fact.field_key,
            location_key(compact_fact.location),
            raw_value,
        )
        if identity in seen_facts:
            raise EvidenceValidationError(
                "compact extraction contains duplicate fact identity",
                code="FACT_IDENTITY_DUPLICATED",
            )
        seen_facts.add(identity)
        facts.append(
            FactCandidate(
                field_key=compact_fact.field_key,
                concept_id=compact_fact.concept_id,
                display_name=compact_fact.display_name,
                value_type=compact_fact.value_type,
                raw_value=compact_fact.raw_value,
                normalized_hint=normalize_text(compact_fact.raw_value),
                source_file_id=str(file_id),
                evidence_text=evidence_text,
                location=compact_fact.location,
                confidence=compact_fact.confidence,
            )
        )
    return DocumentFactExtraction(
        profile=DocumentProfile(
            file_id=str(file_id),
            document_kind=compact.profile.document_kind,
            title=compact.profile.title,
            confidence=compact.profile.confidence,
            evidence_locations=profile_locations,
        ),
        facts=facts,
        missing_field_keys=[],
        semantic_concepts=[],
        validation_specs=[],
    )


def _parent_block_index(
    document: ParsedDocument,
    location: DocumentLocation,
) -> int:
    wanted = location_key(location)
    for index, block in enumerate(document.blocks):
        if location_key(block.location) == wanted:
            return index
    if location.table_index is not None:
        for index, block in enumerate(document.blocks):
            if block.location.table_index == location.table_index:
                return index
    raise EvidenceValidationError("review evidence location has no parent document block")


def _review_unit_blocks(
    document: ParsedDocument,
    locations: list[DocumentLocation],
    context_blocks: int,
) -> set[int]:
    selected: set[int] = set()
    for location in locations:
        anchor = _parent_block_index(document, location)
        start = max(0, anchor - context_blocks)
        end = min(len(document.blocks), anchor + context_blocks + 1)
        selected.update(range(start, end))
    return selected


def _review_payload(
    document: ParsedDocument,
    block_indexes: set[int],
    facts: list[FactCandidate],
    concepts: list[SemanticConcept],
    specs: list[ValidationSpec],
) -> dict[str, Any]:
    blocks = [document.blocks[index] for index in sorted(block_indexes)]
    return {
        **chunk_payload(document, blocks),
        "facts": [fact.model_dump(mode="json") for fact in facts],
        "semantic_concepts": [concept.model_dump(mode="json") for concept in concepts],
        "validation_specs": [spec.model_dump(mode="json") for spec in specs],
        "review_requirements": {
            "required_decision_count": len(facts),
            "one_decision_per_fact": True,
            "evaluate_each_fact_independently": True,
        },
    }


def _payload_chars(payload: dict[str, Any]) -> int:
    import json

    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def build_fact_review_batches(
    document: ParsedDocument,
    extraction: DocumentFactExtraction,
    *,
    max_chars: int,
    context_blocks: int,
) -> list[dict[str, Any]]:
    """Build evidence-local review payloads without truncating source blocks."""

    units: list[
        tuple[
            set[int],
            list[FactCandidate],
            list[SemanticConcept],
            list[ValidationSpec],
        ]
    ] = []
    for fact in extraction.facts:
        units.append(
            (
                _review_unit_blocks(document, [fact.location], context_blocks),
                [fact],
                [],
                [],
            )
        )
    for concept in extraction.semantic_concepts:
        units.append(
            (
                _review_unit_blocks(document, concept.evidence_locations, context_blocks),
                [],
                [concept],
                [],
            )
        )
    for spec in extraction.validation_specs:
        units.append(
            (
                _review_unit_blocks(document, spec.evidence_locations, context_blocks),
                [],
                [],
                [spec],
            )
        )
    if not units:
        return [_review_payload(document, set(), [], [], [])]

    batches: list[dict[str, Any]] = []
    current_blocks: set[int] = set()
    current_facts: list[FactCandidate] = []
    current_concepts: list[SemanticConcept] = []
    current_specs: list[ValidationSpec] = []
    for block_indexes, facts, concepts, specs in units:
        proposed = _review_payload(
            document,
            current_blocks | block_indexes,
            [*current_facts, *facts],
            [*current_concepts, *concepts],
            [*current_specs, *specs],
        )
        if _payload_chars(proposed) <= max_chars:
            current_blocks |= block_indexes
            current_facts.extend(facts)
            current_concepts.extend(concepts)
            current_specs.extend(specs)
            continue
        if current_facts or current_concepts or current_specs:
            batches.append(
                _review_payload(
                    document,
                    current_blocks,
                    current_facts,
                    current_concepts,
                    current_specs,
                )
            )
        single = _review_payload(document, block_indexes, facts, concepts, specs)
        if _payload_chars(single) > max_chars:
            raise EvidenceValidationError("single review unit exceeds review batch limit")
        current_blocks = set(block_indexes)
        current_facts = list(facts)
        current_concepts = list(concepts)
        current_specs = list(specs)
    if current_facts or current_concepts or current_specs:
        batches.append(
            _review_payload(
                document,
                current_blocks,
                current_facts,
                current_concepts,
                current_specs,
            )
        )
    return batches


def merge_fact_review_batches(
    document: ParsedDocument,
    extraction: DocumentFactExtraction,
    reviewed_batches: list[tuple[dict[str, Any], FactReview]],
) -> FactReview:
    """Validate exact batch identities and merge accepted review artifacts."""

    expected_facts = {
        (fact.field_key, fact.source_file_id, location_key(fact.location)): fact
        for fact in extraction.facts
    }
    all_decisions: dict[tuple[object, ...], Any] = {}
    accepted_concepts: dict[str, SemanticConcept] = {}
    accepted_specs: dict[str, ValidationSpec] = {}
    confidences: list[float] = []
    evidence_complete = True
    for payload, review in reviewed_batches:
        if review.file_id != document.file_id:
            raise EvidenceValidationError("review file_id does not match parsed document")
        batch_facts = {
            (
                fact["field_key"],
                fact["source_file_id"],
                location_key(fact["location"]),
            ): fact
            for fact in payload["facts"]
        }
        batch_decisions: dict[tuple[object, ...], Any] = {}
        for decision in review.decisions:
            key = (
                decision.field_key,
                decision.source_file_id,
                location_key(decision.location),
            )
            candidate = batch_facts.get(key)
            if candidate is None or key in batch_decisions or key in all_decisions:
                raise EvidenceValidationError("review decision is duplicated or outside its batch")
            if decision.evidence_text and normalize_text(
                decision.evidence_text
            ) not in normalize_text(candidate["evidence_text"]):
                raise EvidenceValidationError("review evidence does not match candidate evidence")
            batch_decisions[key] = decision
            all_decisions[key] = decision
        if set(batch_decisions) != set(batch_facts):
            raise EvidenceValidationError("review batch did not decide every candidate fact")

        batch_concepts = {
            item["concept_id"]: SemanticConcept.model_validate(item)
            for item in payload["semantic_concepts"]
        }
        for concept in review.semantic_concepts:
            candidate = batch_concepts.get(concept.concept_id)
            if candidate is None or concept.concept_id in accepted_concepts:
                raise EvidenceValidationError("review concept is duplicated or outside its batch")
            if concept.model_dump(mode="json", exclude={"confidence"}) != candidate.model_dump(
                mode="json", exclude={"confidence"}
            ):
                raise EvidenceValidationError("review concept does not match candidate concept")
            accepted_concepts[concept.concept_id] = concept

        batch_specs = {
            item["validation_id"]: ValidationSpec.model_validate(item)
            for item in payload["validation_specs"]
        }
        for spec in review.validation_specs:
            candidate = batch_specs.get(spec.validation_id)
            if candidate is None or spec.validation_id in accepted_specs:
                raise EvidenceValidationError("review spec is duplicated or outside its batch")
            if {
                location_key(location) for location in spec.evidence_locations
            } != {
                location_key(location) for location in candidate.evidence_locations
            }:
                raise EvidenceValidationError("review spec evidence changed outside its batch")
            accepted_specs[spec.validation_id] = spec
        confidences.append(review.confidence)
        evidence_complete = evidence_complete and review.evidence_complete

    if set(all_decisions) != set(expected_facts):
        raise EvidenceValidationError("review batches did not cover every candidate fact")
    return FactReview(
        file_id=document.file_id,
        decisions=list(all_decisions.values()),
        semantic_concepts=list(accepted_concepts.values()),
        validation_specs=list(accepted_specs.values()),
        confidence=min(confidences, default=0.0),
        evidence_complete=bool(reviewed_batches) and evidence_complete,
    )


def location_key(location: DocumentLocation | dict[str, Any]) -> tuple[object, ...]:
    if isinstance(location, dict):
        location = DocumentLocation.model_validate(location)
    return (
        location.page,
        location.paragraph_index,
        location.table_index,
        location.row,
        location.column,
    )


def _evidence_at(document: ParsedDocument) -> dict[tuple[object, ...], list[str]]:
    evidence: dict[tuple[object, ...], list[str]] = defaultdict(list)
    for block in document.blocks:
        evidence[location_key(block.location)].append(block.raw_text)
        if block.table:
            for row in block.table.rows:
                for cell in row.cells:
                    evidence[location_key(cell.location)].append(cell.raw_text)
    return evidence


def evidence_location_exists(document: ParsedDocument, location: DocumentLocation) -> bool:
    return location_key(location) in _evidence_at(document)


def validate_extraction_evidence(
    document: ParsedDocument,
    extraction: DocumentFactExtraction,
) -> None:
    if extraction.profile.file_id != document.file_id:
        raise EvidenceValidationError(
            "profile file_id does not match parsed document", code="FILE_ID_MISMATCH"
        )
    evidence = _evidence_at(document)
    for location in extraction.profile.evidence_locations:
        if location_key(location) not in evidence:
            raise EvidenceValidationError(
                "profile evidence location does not exist",
                code="PROFILE_LOCATION_NOT_FOUND",
            )
    for fact in extraction.facts:
        if fact.source_file_id != document.file_id:
            raise EvidenceValidationError(
                "fact source_file_id does not match parsed document",
                code="FILE_ID_MISMATCH",
            )
        fact_location = location_key(fact.location)
        candidates = evidence.get(fact_location, [])
        if not candidates:
            raise EvidenceValidationError(
                "fact evidence location does not exist", code="FACT_LOCATION_NOT_FOUND"
            )
        normalized_fact_evidence = normalize_text(fact.evidence_text)
        if not any(
            normalized_fact_evidence in normalize_text(candidate) for candidate in candidates
        ):
            raise EvidenceValidationError(
                "fact evidence is not present at the declared location",
                code="FACT_VALUE_NOT_GROUNDED",
            )
    for concept in extraction.semantic_concepts:
        for location in concept.evidence_locations:
            if location_key(location) not in evidence:
                raise EvidenceValidationError(
                    "semantic concept evidence location does not exist",
                    code="FACT_LOCATION_NOT_FOUND",
                )
    for spec in extraction.validation_specs:
        for location in spec.evidence_locations:
            if location_key(location) not in evidence:
                raise EvidenceValidationError(
                    "validation evidence location does not exist",
                    code="FACT_LOCATION_NOT_FOUND",
                )


def _semantic_evidence_exists(
    documents_by_file: dict[str, ParsedDocument],
    evidence_ref: SemanticEvidenceRef,
) -> bool:
    document = documents_by_file.get(evidence_ref.source_file_id)
    return bool(document and evidence_location_exists(document, evidence_ref.location))


def _semantic_ast_references(node: Any) -> set[tuple[str, str]]:
    if not isinstance(node, dict):
        raise EvidenceValidationError("semantic AST node must be an object")
    if node.get("op") == "fact":
        if set(node) != {"op", "fact_id", "source_file_id"}:
            raise EvidenceValidationError("semantic AST fact nodes require qualified references")
        return {(str(node["fact_id"]), str(node["source_file_id"]))}
    references: set[tuple[str, str]] = set()
    for key in ("args", "left", "right"):
        child = node.get(key)
        if isinstance(child, list):
            for item in child:
                references.update(_semantic_ast_references(item))
        elif isinstance(child, dict):
            references.update(_semantic_ast_references(child))
    return references


def validate_semantic_plan(
    *,
    primary_file_id: str,
    documents_by_file: dict[str, ParsedDocument],
    plan: SemanticPlanResponse,
    fact_index: FactIndex,
    accepted_refs: set[tuple[str, str]],
) -> None:
    """Validate file-qualified semantic evidence and references."""

    if plan.file_id != primary_file_id:
        raise EvidenceValidationError("semantic plan file_id does not match primary document")
    for concept in plan.semantic_concepts:
        for fact_ref in concept.fact_refs:
            ref = (fact_ref.fact_id, fact_ref.source_file_id)
            if ref not in fact_index or ref not in accepted_refs:
                raise EvidenceValidationError("semantic concept references an unverified fact")
        if any(
            not _semantic_evidence_exists(documents_by_file, evidence_ref)
            for evidence_ref in concept.evidence_refs
        ):
            raise EvidenceValidationError("semantic concept evidence location does not exist")
    for spec in plan.validation_specs:
        if any(
            not _semantic_evidence_exists(documents_by_file, evidence_ref)
            for evidence_ref in spec.evidence_refs
        ):
            raise EvidenceValidationError("validation evidence location does not exist")
        references = _semantic_ast_references(spec.expression)
        if not references <= fact_index.keys() or not references <= accepted_refs:
            raise EvidenceValidationError("validation rule references an unverified fact")


def project_semantic_plan(
    plan: SemanticPlanResponse,
) -> tuple[list[SemanticConcept], list[ValidationSpec]]:
    """Project internal plans back to the existing public extraction models."""

    concepts = [
        SemanticConcept(
            concept_id=concept.concept_id,
            display_name=concept.display_name,
            value_type=concept.value_type,
            aliases=concept.aliases,
            evidence_locations=[ref.location for ref in concept.evidence_refs],
            confidence=concept.confidence,
        )
        for concept in plan.semantic_concepts
    ]
    specs = [
        ValidationSpec(
            validation_id=spec.validation_id,
            display_name=spec.display_name,
            expression=spec.expression,
            evidence_locations=[ref.location for ref in spec.evidence_refs],
            confidence=spec.confidence,
        )
        for spec in plan.validation_specs
    ]
    return concepts, specs


def _merge_named_models(
    extractions: list[DocumentFactExtraction],
    *,
    attribute: str,
    identity: str,
) -> list[Any]:
    merged: dict[str, Any] = {}
    for extraction in extractions:
        for item in getattr(extraction, attribute):
            item_id = getattr(item, identity)
            value = item.model_dump(mode="json")
            if isinstance(item, SemanticConcept):
                comparable = {
                    key: content
                    for key, content in value.items()
                    if key not in {"confidence", "aliases", "evidence_locations"}
                }
            else:
                comparable = {
                    key: content
                    for key, content in value.items()
                    if key not in {"confidence", "evidence_locations"}
                }
            if item_id not in merged:
                merged[item_id] = item
                continue
            existing = merged[item_id]
            existing_value = existing.model_dump(mode="json")
            if isinstance(existing, SemanticConcept):
                existing_comparable = {
                    key: content
                    for key, content in existing_value.items()
                    if key not in {"confidence", "aliases", "evidence_locations"}
                }
            else:
                existing_comparable = {
                    key: content
                    for key, content in existing_value.items()
                    if key not in {"confidence", "evidence_locations"}
                }
            if existing_comparable != comparable:
                raise EvidenceValidationError(f"conflicting duplicate {identity}: {item_id}")
            locations = list(existing.evidence_locations)
            seen_locations = {location_key(location) for location in locations}
            for location in item.evidence_locations:
                if location_key(location) not in seen_locations:
                    seen_locations.add(location_key(location))
                    locations.append(location)
            if isinstance(existing, SemanticConcept) and isinstance(item, SemanticConcept):
                aliases = list(dict.fromkeys([*existing.aliases, *item.aliases]))
                merged[item_id] = existing.model_copy(
                    update={
                        "aliases": aliases,
                        "evidence_locations": locations,
                        "confidence": max(existing.confidence, item.confidence),
                    }
                )
            elif isinstance(existing, ValidationSpec) and isinstance(item, ValidationSpec):
                merged[item_id] = existing.model_copy(
                    update={
                        "evidence_locations": locations,
                        "confidence": max(existing.confidence, item.confidence),
                    }
                )
    return list(merged.values())


def merge_chunk_extractions(
    document: ParsedDocument,
    extractions: list[DocumentFactExtraction],
) -> DocumentFactExtraction:
    if not extractions:
        raise EvidenceValidationError("document extraction returned no chunks")
    for extraction in extractions:
        validate_extraction_evidence(document, extraction)
    profile_source = max(extractions, key=lambda item: item.profile.confidence).profile
    profile_locations: list[DocumentLocation] = []
    seen_locations: set[tuple[object, ...]] = set()
    for extraction in extractions:
        for location in extraction.profile.evidence_locations:
            key = location_key(location)
            if key not in seen_locations:
                seen_locations.add(key)
                profile_locations.append(location)
    facts: list[FactCandidate] = []
    seen_facts: set[tuple[object, ...]] = set()
    for extraction in extractions:
        for fact in extraction.facts:
            key = (
                fact.field_key,
                normalize_text(fact.raw_value),
                location_key(fact.location),
            )
            if key not in seen_facts:
                seen_facts.add(key)
                facts.append(fact)
    missing = set(extractions[0].missing_field_keys)
    for extraction in extractions[1:]:
        missing &= set(extraction.missing_field_keys)
    return DocumentFactExtraction(
        profile=DocumentProfile(
            **profile_source.model_dump(exclude={"evidence_locations"}),
            evidence_locations=profile_locations,
        ),
        facts=facts,
        missing_field_keys=sorted(missing),
        semantic_concepts=_merge_named_models(
            extractions, attribute="semantic_concepts", identity="concept_id"
        ),
        validation_specs=_merge_named_models(
            extractions, attribute="validation_specs", identity="validation_id"
        ),
    )


def _currency(raw: str) -> str | None:
    currency_markers = {
        "CNY": ("人民币", "CNY", "RMB", "￥", "¥"),
        "USD": ("美元", "USD", "US$"),
        "EUR": ("欧元", "EUR", "€"),
        "HKD": ("港币", "港元", "HKD", "HK$"),
    }
    upper = raw.upper()
    for code, markers in currency_markers.items():
        if any(marker.upper() in upper for marker in markers):
            return code
    return None


def _number(raw: str) -> Decimal | None:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", raw.replace(",", "").replace("，", ""))
    if not match:
        return None
    try:
        return Decimal(match.group())
    except InvalidOperation:
        return None


def normalized_fact_components(fact: FactCandidate) -> dict[str, Any] | None:
    raw = normalize_text(fact.raw_value)
    if fact.value_type == "MONEY":
        value = raw.replace(",", "").replace("，", "")
        number = _number(value)
        if number is not None:
            if "亿" in value:
                number *= Decimal("100000000")
            elif "万" in value:
                number *= Decimal("10000")
            return {"kind": "MONEY", "value": number, "currency": _currency(raw)}
    if fact.value_type in {"PERCENTAGE", "RATE"}:
        if not re.search(r"(?:%|％|百分之|BP|基点)", raw, flags=re.I):
            return None
        number = _number(raw)
        if number is not None:
            if re.search(r"(?:BP|基点)", raw, flags=re.I):
                number /= Decimal("100")
            return {"kind": "PERCENTAGE", "value": number, "unit": "PERCENT_POINT"}
    if fact.value_type == "DURATION":
        match = re.search(r"(\d+(?:\.\d+)?)\s*(年|个月|月|周|天|日)", raw)
        if match:
            number = Decimal(match.group(1))
            unit = match.group(2)
            if unit == "年":
                return {"kind": "DURATION", "value": number * 12, "unit": "MONTH"}
            if unit in {"个月", "月"}:
                return {"kind": "DURATION", "value": number, "unit": "MONTH"}
            return {
                "kind": "DURATION",
                "value": number * (7 if unit == "周" else 1),
                "unit": "DAY",
            }
    if fact.value_type == "DATE":
        match = re.search(r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?", raw)
        if match:
            year, month, day = (int(match.group(index)) for index in range(1, 4))
            try:
                return {"kind": "DATE", "value": date(year, month, day).isoformat()}
            except ValueError:
                return None
    if fact.value_type in {"NUMBER", "QUANTITY"}:
        number = _number(raw)
        if number is not None:
            unit_match = re.search(
                r"[-+]?\d+(?:\.\d+)?\s*([\u4e00-\u9fffA-Za-z]+)", raw.replace(",", "")
            )
            unit = unit_match.group(1).casefold() if unit_match else None
            return {"kind": fact.value_type, "value": number, "unit": unit}
    return None


def normalize_fact(fact: FactCandidate) -> str:
    components = normalized_fact_components(fact)
    if components:
        kind = components["kind"]
        value = components["value"]
        rendered = value.normalize() if isinstance(value, Decimal) else value
        if kind == "MONEY":
            currency = components.get("currency")
            return f"MONEY:{rendered}" + (f":{currency}" if currency else "")
        if kind == "PERCENTAGE":
            return f"PERCENTAGE:{rendered}%"
        if kind == "DURATION":
            suffix = "M" if components["unit"] == "MONTH" else "D"
            return f"DURATION:{rendered}{suffix}"
        if kind == "DATE":
            return f"DATE:{rendered}"
        unit = components.get("unit")
        return f"{kind}:{rendered}" + (f":{unit}" if unit else "")
    raw = normalize_text(fact.raw_value)
    hint = normalize_text(fact.normalized_hint or "")
    normalized = hint or raw
    normalized = re.sub(r"[\s·•,，。.;；:：()（）\[\]【】]", "", normalized).casefold()
    return f"{fact.value_type}:{normalized}"


def compare_facts(target: FactCandidate, reference: FactCandidate) -> bool | None:
    target_value = normalized_fact_components(target)
    reference_value = normalized_fact_components(reference)
    if target_value is None or reference_value is None:
        if target.value_type in NUMERIC_VALUE_TYPES | {"DATE"} or reference.value_type in (
            NUMERIC_VALUE_TYPES | {"DATE"}
        ):
            return None
        return normalize_fact(target) == normalize_fact(reference)
    if target_value["kind"] != reference_value["kind"]:
        return None
    if target_value["kind"] == "MONEY":
        target_currency = target_value.get("currency")
        reference_currency = reference_value.get("currency")
        if target_currency != reference_currency and (target_currency or reference_currency):
            return None
    if target_value.get("unit") != reference_value.get("unit"):
        return None
    return bool(target_value["value"] == reference_value["value"])


def fact_conflict_diff_items(
    matrix: list[dict[str, Any]],
    *,
    target_file_id: str | None = None,
) -> list[DiffItem]:
    """Convert confirmed cross-document fact conflicts into two-sided diffs."""

    differences: list[DiffItem] = []
    seen: set[str] = set()
    for item in matrix:
        if item.get("status") != "CONFLICT":
            continue
        target_candidate = item.get("target_candidate") or {}
        if target_file_id and target_candidate.get("source_file_id") != target_file_id:
            continue
        for relation in item.get("reference_results", []):
            if relation.get("status") != "CONFLICT" or not relation.get("candidate"):
                continue
            reference_candidate = relation["candidate"]
            target_fact = FactCandidate.model_validate(
                {key: value for key, value in target_candidate.items() if key != "normalized_value"}
            )
            reference_fact = FactCandidate.model_validate(
                {
                    key: value
                    for key, value in reference_candidate.items()
                    if key != "normalized_value"
                }
            )
            target_id = stable_fact_id(target_fact)
            reference_id = stable_fact_id(reference_fact)
            seed = f"{target_id}:{reference_id}".encode()
            diff_id = f"fact_diff_{hashlib.sha256(seed).hexdigest()[:20]}"
            if diff_id in seen:
                continue
            seen.add(diff_id)
            numeric = (
                target_fact.value_type in NUMERIC_VALUE_TYPES | {"DATE"}
                or reference_fact.value_type in NUMERIC_VALUE_TYPES | {"DATE"}
            )
            differences.append(
                DiffItem(
                    diff_id=diff_id,
                    diff_type="NUMERIC_CHANGED" if numeric else "MODIFIED",
                    title=f"{item['display_name']}来源值不一致",
                    baseline=DiffSide(
                        file_id=reference_fact.source_file_id,
                        location=reference_fact.location,
                        locations=[reference_fact.location],
                        text=reference_fact.evidence_text,
                    ),
                    target=DiffSide(
                        file_id=target_fact.source_file_id,
                        location=target_fact.location,
                        locations=[target_fact.location],
                        text=target_fact.evidence_text,
                    ),
                    segments=[
                        DiffSegment(operation="DELETE", text=reference_fact.evidence_text),
                        DiffSegment(operation="INSERT", text=target_fact.evidence_text),
                    ],
                    confidence=min(target_fact.confidence, reference_fact.confidence),
                    requires_manual_review=False,
                    certainty="CONFIRMED",
                )
            )
    return differences


def build_fact_matrix(
    extractions: dict[str, DocumentFactExtraction],
    *,
    target_file_id: str | None = None,
    reference_file_ids: list[str] | None = None,
    mapping_records: list[dict[str, Any]] | None = None,
    required_missing: set[tuple[str, str]] | None = None,
    uncertain_reference_file_ids: set[str] | None = None,
    consensus_fields: set[tuple[str, str, tuple[object, ...]]] | None = None,
) -> list[dict[str, Any]]:
    if not extractions:
        return []
    target_file_id = target_file_id or next(iter(extractions))
    target_extraction = extractions.get(target_file_id)
    if target_extraction is None:
        return []
    reference_file_ids = reference_file_ids or [
        file_id for file_id in extractions if file_id != target_file_id
    ]
    required_missing = required_missing or set()
    uncertain_reference_file_ids = uncertain_reference_file_ids or set()
    catalogs = target_fact_catalog(target_extraction)
    records_by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if mapping_records is None:
        # Backward-compatible helper behavior for direct callers. Production passes
        # explicit mapping records produced by the cross-document consensus stage.
        for target in catalogs:
            target_fact = FactCandidate.model_validate(
                {key: value for key, value in target.items() if key != "target_fact_id"}
            )
            for file_id in reference_file_ids:
                extraction = extractions.get(file_id)
                if extraction is None:
                    continue
                for fact in extraction.facts:
                    if fact.field_key == target_fact.field_key:
                        records_by_target[target["target_fact_id"]].append(
                            {
                                "target_fact_id": target["target_fact_id"],
                                "source_file_id": file_id,
                                "reference_field_key": fact.field_key,
                                "reference_location": fact.location.model_dump(
                                    mode="json", exclude_none=True
                                ),
                                "status": "ACCEPT",
                            }
                        )
    else:
        for record in mapping_records:
            records_by_target[str(record["target_fact_id"])].append(record)
    matrix: list[dict[str, Any]] = []
    for target in catalogs:
        target_fact_id = target["target_fact_id"]
        target_fact = FactCandidate.model_validate(
            {key: value for key, value in target.items() if key != "target_fact_id"}
        )
        target_key = (
            target_fact.field_key,
            target_fact.source_file_id,
            location_key(target_fact.location),
        )
        target_consensus = consensus_fields is None or target_key in consensus_fields
        candidates = [target_fact]
        reference_results: list[dict[str, Any]] = []
        for reference_file_id in reference_file_ids:
            extraction = extractions.get(reference_file_id)
            records = [
                record
                for record in records_by_target.get(target_fact_id, [])
                if record["source_file_id"] == reference_file_id
            ]
            matched: list[tuple[dict[str, Any], FactCandidate]] = []
            if extraction is not None:
                facts_by_key = {
                    (fact.field_key, location_key(fact.location)): fact for fact in extraction.facts
                }
                for record in records:
                    fact = facts_by_key.get(
                        (
                            record["reference_field_key"],
                            location_key(record["reference_location"]),
                        )
                    )
                    if fact is not None:
                        matched.append((record, fact))
            if not records:
                if reference_file_id in uncertain_reference_file_ids:
                    reference_results.append(
                        {
                            "source_file_id": reference_file_id,
                            "status": "UNCERTAIN",
                            "candidate": None,
                            "reason_code": "MAPPING_UNAVAILABLE",
                            "requires_manual_review": True,
                        }
                    )
                    continue
                reference_results.append(
                    {
                        "source_file_id": reference_file_id,
                        "status": "MISSING",
                        "candidate": None,
                        "reason_code": "NOT_MENTIONED",
                        "requires_manual_review": (
                            target_fact_id,
                            reference_file_id,
                        )
                        in required_missing,
                    }
                )
                continue
            if not target_consensus or not matched or any(
                record.get("status") != "ACCEPT" for record, _fact in matched
            ):
                candidate = matched[0][1] if len(matched) == 1 else None
                if candidate is not None:
                    candidates.append(candidate)
                reference_results.append(
                    {
                        "source_file_id": reference_file_id,
                        "status": "UNCERTAIN",
                        "candidate": (
                            {
                                **candidate.model_dump(mode="json"),
                                "normalized_value": normalize_fact(candidate),
                            }
                            if candidate
                            else None
                        ),
                        "reason_code": "SEMANTIC_MAPPING_UNCERTAIN",
                        "requires_manual_review": True,
                    }
                )
                continue
            comparisons: list[bool | None] = []
            for _record, fact in matched:
                reference_key = (fact.field_key, fact.source_file_id, location_key(fact.location))
                if consensus_fields is not None and reference_key not in consensus_fields:
                    comparisons.append(None)
                else:
                    comparisons.append(compare_facts(target_fact, fact))
                candidates.append(fact)
            if None in comparisons or len(set(comparisons)) > 1:
                relation_status = "UNCERTAIN"
                reason_code = "VALUE_OR_CONTEXT_INCOMPARABLE"
            elif any(result is False for result in comparisons):
                relation_status = "CONFLICT"
                reason_code = "VALUE_CONFLICT"
            else:
                relation_status = "CONSISTENT"
                reason_code = "VALUE_CONSISTENT"
            candidate = matched[0][1] if len(matched) == 1 else None
            reference_results.append(
                {
                    "source_file_id": reference_file_id,
                    "status": relation_status,
                    "candidate": (
                        {
                            **candidate.model_dump(mode="json"),
                            "normalized_value": normalize_fact(candidate),
                        }
                        if candidate
                        else None
                    ),
                    "reason_code": reason_code,
                    "requires_manual_review": relation_status == "UNCERTAIN",
                }
            )
        relation_statuses = {item["status"] for item in reference_results}
        if "CONFLICT" in relation_statuses:
            status = "CONFLICT"
        elif "CONSISTENT" in relation_statuses:
            status = "CONSISTENT"
        elif "UNCERTAIN" in relation_statuses or not target_consensus:
            status = "UNCERTAIN"
        else:
            status = "MISSING"
        matrix.append(
            {
                "target_fact_id": target_fact_id,
                "field_key": target_fact.field_key,
                "display_name": target_fact.display_name,
                "status": status,
                "target_candidate": {
                    **target_fact.model_dump(mode="json"),
                    "normalized_value": normalize_fact(target_fact),
                },
                "candidates": [
                    {
                        **fact.model_dump(mode="json"),
                        "normalized_value": normalize_fact(fact),
                    }
                    for fact in candidates
                ],
                "reference_results": reference_results,
                "missing_source_file_ids": sorted(
                    item["source_file_id"]
                    for item in reference_results
                    if item["status"] == "MISSING"
                ),
            }
        )
    return matrix


def fact_matrix_result_items(
    matrix: list[dict[str, Any]],
    *,
    include_conflicts: bool = True,
    include_uncertain: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    risks: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    passed: list[dict[str, Any]] = []
    for item in matrix:
        target_evidence = {
            "file_id": item["target_candidate"]["source_file_id"],
            "text": item["target_candidate"]["evidence_text"],
            "location": item["target_candidate"]["location"],
        }
        conflict_evidence = [target_evidence]
        for relation in item.get("reference_results", []):
            candidate = relation.get("candidate")
            if relation["status"] == "CONFLICT" and candidate:
                conflict_evidence.append(
                    {
                        "file_id": candidate["source_file_id"],
                        "text": candidate["evidence_text"],
                        "location": candidate["location"],
                    }
                )
        safe_key = re.sub(r"[^a-z0-9_]+", "_", item["field_key"].casefold())
        item_suffix = f"{safe_key}_{item.get('target_fact_id', 'target')}"
        if item["status"] == "CONFLICT" and include_conflicts:
            risks.append(
                {
                    "risk_id": f"risk_fact_{item_suffix}",
                    "module_code": "FACT_CONSISTENCY",
                    "risk_type": "ADDITION_OR_CHANGE",
                    "change_type": "SOURCE_CONFLICT",
                    "title": f"{item['display_name']}存在来源冲突",
                    "description": "不同来源给出了不一致的已抽取事实，系统不自动选择正确值。",
                    "source_evidence": conflict_evidence,
                    "related_diff_ids": [],
                    "related_rule_ids": [],
                    "requires_manual_action": True,
                }
            )
        uncertain_relations = [
            relation
            for relation in item.get("reference_results", [])
            if relation["status"] == "UNCERTAIN"
        ]
        required_missing_relations = [
            relation
            for relation in item.get("reference_results", [])
            if relation["status"] == "MISSING" and relation.get("requires_manual_review")
        ]
        if include_uncertain and (item["status"] == "UNCERTAIN" or uncertain_relations):
            reviews.append(
                {
                    "review_id": f"review_fact_uncertain_{item_suffix}",
                    "module_code": "FACT_CONSISTENCY",
                    "reason_code": "FACT_UNCERTAIN",
                    "title": f"{item['display_name']}需要人工复核",
                    "description": "字段语义、单位、币种、时间范围或证据共识不足。",
                    "source_evidence": [target_evidence],
                    "related_diff_ids": [],
                    "requires_manual_action": True,
                }
            )
        if include_uncertain and required_missing_relations:
            risks.append(
                {
                    "risk_id": f"risk_fact_missing_{item_suffix}",
                    "module_code": "FACT_CONSISTENCY",
                    "risk_type": "DELETION_OR_MISSING",
                    "change_type": "REQUIRED_SOURCE_MISSING",
                    "title": f"{item['display_name']}在要求的资料中未提及",
                    "description": "经可靠校验计划确认，该来源应包含此事实，但未找到对应内容。",
                    "source_evidence": [target_evidence],
                    "related_diff_ids": [],
                    "related_rule_ids": [],
                    "requires_manual_action": True,
                }
            )
        if item["status"] == "CONSISTENT" and not required_missing_relations:
            passed.append(
                {
                    "check_id": f"check_fact_{item_suffix}",
                    "module_code": "FACT_CONSISTENCY",
                    "title": f"{item['display_name']}来源一致",
                    "description": "至少两个来源的规范化事实值一致。",
                }
            )
    return risks, reviews, passed
