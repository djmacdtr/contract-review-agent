from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from math import ceil
from typing import Any

from app.adapters.llm.schemas import (
    CompactDocumentFactExtraction,
    CompactDocumentOverview,
    CompactFactBatchExtraction,
    DocumentFactExtraction,
    DocumentProfile,
    FactCandidate,
    FactReview,
    NumericCandidateExtraction,
    SemanticConcept,
    SemanticEvidenceRef,
    SemanticPlanResponse,
    TextFactExtraction,
    ValidationSpec,
)
from app.comparison.models import DiffItem, DiffSegment, DiffSide
from app.documents.models import (
    DocumentBlock,
    DocumentLocation,
    ParsedDocument,
    ParsedTable,
    TableRow,
)
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


@dataclass(frozen=True)
class TextExtractionCandidate:
    """A target-side template delta eligible for non-numeric extraction."""

    block: DocumentBlock
    diff_ids: tuple[str, ...] = ()
    context_units: tuple[dict[str, Any], ...] = ()


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
_BARE_NUMBER_CONTEXT = re.compile(
    r"(?:金额|数额|数量|期限|利率|费率|租金|租赁|融资|价款|比例|份数|期数|"
    r"余额|本金|利息)",
    flags=re.IGNORECASE,
)
MAX_COMPACT_FACTS = 24
MAX_NUMERIC_CANDIDATES_PER_CHUNK = 48
DEFAULT_ESTIMATED_OUTPUT_TOKEN_LIMIT = 4800


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


def qualified_fact_refs(
    extraction: DocumentFactExtraction,
    review: FactReview | None,
    min_confidence: float,
    *,
    extraction_model: str | None = None,
    review_model: str | None = None,
    document: ParsedDocument | None = None,
    require_independent_model: bool = True,
    require_review: bool = True,
) -> set[tuple[str, str]]:
    """Return the single fact set that downstream dynamic consumers may use.

    ``accepted_fact_refs`` is retained as a compatibility helper for callers
    that only need the historical review decision check.  Production mapping,
    semantic planning, and result construction use this gate.  When
    ``require_review`` is false, the same evidence/identity/confidence checks
    apply without a review decision; when it is true, model identity and
    review completeness are also required.
    """

    fact_keys = [
        (fact.field_key, fact.source_file_id, location_key(fact.location))
        for fact in extraction.facts
    ]
    decision_keys = [
        (decision.field_key, decision.source_file_id, location_key(decision.location))
        for decision in review.decisions
    ] if review is not None else []
    if len(fact_keys) != len(set(fact_keys)):
        return set()
    if not require_review:
        if document is not None:
            try:
                validate_extraction_evidence(document, extraction)
            except EvidenceValidationError:
                return set()
        return {
            (stable_fact_id(fact), fact.source_file_id)
            for fact in extraction.facts
            if fact.source_file_id == extraction.profile.file_id
            and fact.confidence >= min_confidence
        }
    if (
        review is None
        or review.file_id != extraction.profile.file_id
        or not review.evidence_complete
        or review.confidence < min_confidence
    ):
        return set()
    if require_independent_model and (
        not extraction_model
        or not review_model
        or extraction_model == review_model
    ):
        return set()
    if len(decision_keys) != len(set(decision_keys)) or set(decision_keys) != set(fact_keys):
        return set()
    decisions = {
        (decision.field_key, decision.source_file_id, location_key(decision.location)): decision
        for decision in review.decisions
    }
    evidence = _evidence_at(document) if document is not None else None
    qualified: set[tuple[str, str]] = set()
    for fact in extraction.facts:
        key = (fact.field_key, fact.source_file_id, location_key(fact.location))
        decision = decisions[key]
        if (
            fact.source_file_id != extraction.profile.file_id
            or fact.confidence < min_confidence
            or decision.decision != "ACCEPT"
            or decision.confidence < min_confidence
        ):
            continue
        if evidence is not None:
            candidates = evidence.get(location_key(fact.location), [])
            if not candidates or not any(
                normalize_text(fact.evidence_text) in normalize_text(candidate)
                for candidate in candidates
            ):
                continue
        qualified.add((stable_fact_id(fact), fact.source_file_id))
    return qualified


def mapping_proposal_key(item: Any) -> tuple[str, str, str, tuple[object, ...]]:
    """Build the stable identity used by mapping and mapping-review gates."""

    def get(name: str) -> Any:
        return getattr(item, name) if hasattr(item, name) else item[name]

    return (
        str(get("target_fact_id")),
        str(get("reference_field_key")),
        str(get("source_file_id")),
        location_key(get("reference_location")),
    )


def missing_requirement_key(item: Any) -> str:
    if hasattr(item, "target_fact_id"):
        return str(item.target_fact_id)
    return str(item["target_fact_id"])


def validate_mapping_review_coverage(
    mapping: Any,
    review: Any,
    reference_file_id: str,
) -> None:
    """Require a one-to-one review decision for every mapping proposal."""

    if (
        mapping.reference_file_id != reference_file_id
        or review.reference_file_id != reference_file_id
    ):
        raise EvidenceValidationError(
            "mapping and review reference file does not match",
            code="MAPPING_FILE_ID_MISMATCH",
        )
    proposal_keys = [mapping_proposal_key(item) for item in mapping.mappings]
    review_keys = [mapping_proposal_key(item) for item in review.decisions]
    requirement_keys = [missing_requirement_key(item) for item in mapping.missing_requirements]
    requirement_review_keys = [
        missing_requirement_key(item) for item in review.missing_requirement_decisions
    ]
    if (
        len(proposal_keys) != len(set(proposal_keys))
        or len(review_keys) != len(set(review_keys))
        or set(proposal_keys) != set(review_keys)
        or len(requirement_keys) != len(set(requirement_keys))
        or len(requirement_review_keys) != len(set(requirement_review_keys))
        or set(requirement_keys) != set(requirement_review_keys)
    ):
        raise EvidenceValidationError(
            "mapping review does not cover exactly every proposal and requirement",
            code="MAPPING_REVIEW_INCOMPLETE",
        )


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
    return [
        {
            key: candidate[key]
            for key in ("raw_value", "candidate_kind", "location")
        }
        for candidate in selected
    ]


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
                context = text[max(0, start - 16) : min(len(text), end + 16)]
                bare_number_without_context = kind == "NUMBER" and not _BARE_NUMBER_CONTEXT.search(
                    context
                )
                if (structural and kind != "IDENTIFIER") or bare_number_without_context:
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
    evidence_blocks: list[dict[str, Any]] = []
    row_only = any(block.location.row is not None for block in blocks)
    for block in blocks:
        if block.table is None:
            continue
        for row in block.table.rows:
            if block.location.row is not None:
                evidence_blocks.append(
                    {
                        "block_id": f"{block.block_id}_row_{row.row:04d}",
                        "type": "TABLE_ROW",
                        "text": "\t".join(cell.raw_text for cell in row.cells),
                        "location": compact_location(
                            DocumentLocation(table_index=block.table.table_index, row=row.row)
                        ),
                    }
                )
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
        "table_location_mode": "ROW_ONLY" if row_only and not any(
            block.location.column is not None for block in blocks
        ) else "ROW_OR_CELL",
    }
    return payload


def stable_unit_id(block: DocumentBlock) -> str:
    canonical = {
        "block_id": block.block_id,
        "location": location_key(block.location),
        "text": normalize_text(block.raw_text),
    }
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:20]
    return f"unit_{digest}"


EXTRACTION_VERSION = "structured-map-reduce-v2"
PROFILE_EXTRACTION_VERSION = "profile-v2"
NUMERIC_EXTRACTION_VERSION = "numeric-v2"
TEXT_EXTRACTION_VERSION = "text-v4"
FACT_REVIEW_CHECKPOINT_VERSION = "fact-review-v1"
TEXT_FACT_VALUE_TYPES = frozenset({"TEXT", "ENTITY", "UNKNOWN"})
# A TABLE_STRUCTURE_EXPANDED diff has no template-aligned fact-level unit
# identity.  The deterministic template diff remains authoritative for it;
# row/cell changes with a concrete location remain ordinary text candidates.


def stable_batch_id(
    file_sha256: str,
    blocks: list[DocumentBlock],
    extraction_version: str = EXTRACTION_VERSION,
) -> str:
    """Derive a retry/idempotency key from content, not task-local file IDs."""

    unit_ids = sorted(stable_unit_id(block) for block in blocks)
    digest = hashlib.sha256(
        json.dumps(
            [file_sha256, unit_ids, extraction_version],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:24]
    return f"batch_{digest}"


def stable_review_batch_id(document: ParsedDocument, payload: dict[str, Any]) -> str:
    """Derive an idempotent review key from file content and review identities."""

    review_identity = {
        "file_sha256": document.sha256,
        "facts": [
            {
                "field_key": fact.get("field_key"),
                "source_file_id": fact.get("source_file_id"),
                "location": fact.get("location"),
            }
            for fact in payload.get("facts", [])
            if isinstance(fact, dict)
        ],
        "semantic_concepts": [
            item.get("concept_id")
            for item in payload.get("semantic_concepts", [])
            if isinstance(item, dict)
        ],
        "validation_specs": [
            item.get("validation_id")
            for item in payload.get("validation_specs", [])
            if isinstance(item, dict)
        ],
        "version": FACT_REVIEW_CHECKPOINT_VERSION,
    }
    digest = hashlib.sha256(
        json.dumps(
            review_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:24]
    return f"review_{digest}"


def review_payload_digest(payload: dict[str, Any]) -> str:
    """Hash the complete review input so changed evidence cannot reuse a result."""

    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]


def estimate_extraction_output_tokens(max_facts: int, numeric_candidate_count: int) -> int:
    """Estimate compact JSON output using a conservative fixed upper bound."""

    estimated_chars = 256 + max_facts * 220 + numeric_candidate_count * 48
    return ceil(estimated_chars / 2)


def estimate_simplified_output_tokens(
    *, numeric_candidate_count: int, max_text_facts: int
) -> int:
    """Fixed conservative bound for the two compact response protocols."""

    # This intentionally counts JSON punctuation and key names.  It is a
    # planning guard, not a truncation mechanism.
    # 160 characters covers the bounded key/value fields of one numeric item
    # (including JSON punctuation and the 64-character semantic key).
    numeric_chars = 64 + numeric_candidate_count * 160
    text_chars = 32 + max_text_facts * 170
    # Numeric and text responses are separate requests.  Their output budgets
    # must be bounded independently; summing them would halve useful batch
    # capacity while overstating any single response.
    return max(ceil(numeric_chars / 2), ceil(text_chars / 2))


def _simplified_units(blocks: list[DocumentBlock]) -> list[dict[str, Any]]:
    return [
        {
            "unit_id": stable_unit_id(block),
            "type": block.type,
            "text": block.raw_text,
            "location": compact_location(block.location),
            "table": (
                {
                    "table_index": block.location.table_index,
                    "row": block.location.row,
                    "column": block.location.column,
                }
                if block.location.table_index is not None
                else None
            ),
        }
        for block in blocks
    ]


def build_numeric_candidate_payload(
    document: ParsedDocument,
    blocks: list[DocumentBlock],
    *,
    batch_id: str,
) -> dict[str, Any]:
    candidates = [
        {"candidate_index": index, **candidate}
        for index, candidate in enumerate(numeric_candidates(blocks), start=1)
    ]
    return {
        "file_id": document.file_id,
        "role": document.role,
        "batch_id": batch_id,
        "units": _simplified_units(blocks),
        "numeric_candidates": candidates,
        "requirements": {
            "max_items": 24,
            "required_decision_count": len(candidates),
            "each_candidate_exactly_once": True,
            "identity_and_evidence_are_program_owned": True,
        },
    }


def numeric_candidate_indexes(payload: dict[str, Any]) -> list[int]:
    """Return the exact candidate indexes expected for one numeric request.

    Normal planner payloads are numbered from one, while a truncation recovery
    child may carry a non-contiguous subset of its parent's indexes.  Keeping
    this detail in the payload lets the wire schema and the application-level
    completeness check agree without changing the identity of ordinary
    batches.
    """

    candidates = payload.get("numeric_candidates", [])
    if not isinstance(candidates, list):
        return []
    explicit = payload.get("_candidate_indexes")
    if (
        isinstance(explicit, list)
        and len(explicit) == len(candidates)
        and all(type(index) is int and index >= 1 for index in explicit)
        and len(set(explicit)) == len(explicit)
    ):
        return list(explicit)
    return list(range(1, len(candidates) + 1))


def build_text_fact_payload(
    document: ParsedDocument,
    blocks: list[DocumentBlock],
    *,
    batch_id: str,
    context_units: list[dict[str, Any]] | None = None,
    max_items: int = 12,
) -> dict[str, Any]:
    return {
        "file_id": document.file_id,
        "role": document.role,
        "batch_id": batch_id,
        "units": _simplified_units(blocks),
        "readonly_context": context_units or [],
        "requirements": {
            "max_items": max_items,
            "quote_must_be_exact_or_format_equivalent_substring": True,
            "facts_must_reference_candidate_units_only": True,
            "readonly_context_cannot_be_used_as_evidence": True,
            "identity_and_location_are_program_owned": True,
        },
    }


def _target_block_for_location(
    document: ParsedDocument,
    location: DocumentLocation,
) -> DocumentBlock | None:
    wanted = location_key(location)
    for block in document.blocks:
        if location_key(block.location) == wanted:
            return block
        if block.table is not None and block.location.table_index == location.table_index:
            for row in block.table.rows:
                for cell in row.cells:
                    if location_key(cell.location) == wanted:
                        return block
    return None


def _context_unit(
    *,
    context_id: str,
    text: str,
    location: DocumentLocation,
) -> dict[str, Any]:
    return {
        "context_id": context_id,
        "type": "READONLY_CONTEXT",
        "text": text,
        "location": compact_location(location),
    }


def _template_candidate_context(
    document: ParsedDocument,
    anchor: DocumentBlock | None,
    target_location: DocumentLocation,
    candidate_text: str,
) -> tuple[dict[str, Any], ...]:
    if anchor is None:
        return ()
    # A table-level structural delta is already a synthetic INSERT fragment,
    # not a row/cell unit.  Supplying the whole owning table as context makes
    # it easy for the model to quote unrelated text.  Keep the candidate
    # isolated; deterministic template comparison still carries the table
    # structure result.
    if (
        target_location.table_index is not None
        and target_location.row is None
        and target_location.column is None
    ):
        return ()
    context: list[dict[str, Any]] = []
    # The complete owning block is read-only context.  The candidate unit is
    # still the only legal source for a returned quote.
    context_text = anchor.raw_text
    if len(context_text) > 2000:
        anchor_position = context_text.find(candidate_text.strip())
        if anchor_position < 0:
            anchor_position = 0
        context_start = max(0, anchor_position - 600)
        context_end = min(len(context_text), anchor_position + len(candidate_text) + 600)
        context_text = context_text[context_start:context_end]
    context.append(
        _context_unit(
            context_id=f"context_{anchor.block_id}",
            text=context_text,
            location=anchor.location,
        )
    )
    anchor_index = document.blocks.index(anchor)
    for previous in reversed(document.blocks[max(0, anchor_index - 3) : anchor_index]):
        if previous.type in {"HEADER", "PARAGRAPH"} and previous.raw_text.strip():
            context.append(
                _context_unit(
                    context_id=f"context_{previous.block_id}",
                    text=previous.raw_text,
                    location=previous.location,
                )
            )
            if len(context) >= 3:
                break
    if anchor.table is not None and target_location.row is not None:
        header_row = next(
            (row for row in anchor.table.rows if row.row < target_location.row),
            None,
        )
        if header_row is not None:
            context.append(
                _context_unit(
                    context_id=(
                        f"context_table_{anchor.table.table_index}_row_{header_row.row}"
                    ),
                    text="\t".join(cell.raw_text for cell in header_row.cells),
                    location=DocumentLocation(
                        table_index=anchor.table.table_index,
                        row=header_row.row,
                    ),
                )
            )
    deduplicated: dict[str, dict[str, Any]] = {}
    for item in context:
        deduplicated.setdefault(str(item["context_id"]), item)
    return tuple(deduplicated.values())


def _grounded_template_candidate_text(
    document: ParsedDocument,
    location: DocumentLocation,
    preferred_text: str,
    fallback_text: str,
) -> str | None:
    """Return a candidate fragment that is already grounded in the target.

    Template diff segments can be normalized, split, or otherwise non-contiguous
    even when the target-side location is valid.  Such a segment must not be
    sent to the text extractor as if it were source text.  Prefer the segment
    when it uniquely maps to the declared location, then fall back to the
    target-side text.  The matcher returns the original source slice, so later
    evidence rehydration remains deterministic.
    """

    sources = _evidence_at(document).get(location_key(location), [])
    for candidate in (preferred_text, fallback_text):
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        grounded = _unique_grounded_quote(sources, candidate)
        if grounded is not None:
            return grounded
    return None


def build_template_text_candidates(
    template_review: Any,
    target: ParsedDocument,
) -> list[TextExtractionCandidate]:
    """Convert target-side template deltas into bounded text candidates.

    This function consumes only deterministic comparison output.  Deleted
    template content has no target evidence and is intentionally excluded.
    Duplicate locations/texts are merged while retaining their diff IDs.
    """

    diffs: list[Any] = list(getattr(template_review, "diff_items", []))
    diagnostics = getattr(template_review, "diagnostics", None)
    diffs.extend(
        item.diff
        for item in getattr(diagnostics, "filtered_diff_items", [])
        if getattr(item, "diff", None) is not None
    )
    candidates: dict[tuple[tuple[object, ...], str], TextExtractionCandidate] = {}
    for diff in diffs:
        target_side = getattr(diff, "target", None)
        if target_side is None or getattr(diff, "diff_type", None) == "DELETED":
            continue
        segments = [
            segment.text
            for segment in getattr(diff, "segments", [])
            if getattr(segment, "operation", None) == "INSERT" and segment.text.strip()
            and getattr(diff, "diff_type", None) != "TABLE_STRUCTURE_EXPANDED"
        ]
        fallback_text = (
            target_side.text
            if target_side.text.strip()
            and getattr(diff, "diff_type", None) != "TABLE_STRUCTURE_EXPANDED"
            else ""
        )
        grounded_segments = [
            grounded
            for segment in segments
            if (
                grounded := _grounded_template_candidate_text(
                    target,
                    target_side.location,
                    segment,
                    "",
                )
            )
            is not None
        ]
        texts = grounded_segments or [
            grounded
            for grounded in [
                _grounded_template_candidate_text(
                    target,
                    target_side.location,
                    fallback_text,
                    "",
                )
            ]
            if grounded is not None
        ]
        anchor = _target_block_for_location(target, target_side.location)
        for segment_index, text in enumerate(texts):
            context = _template_candidate_context(
                target,
                anchor,
                target_side.location,
                text,
            )
            normalized = normalize_text(text)
            key = (location_key(target_side.location), normalized)
            digest = hashlib.sha256(
                json.dumps(
                    [diff.diff_id, segment_index, location_key(target_side.location), normalized],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:20]
            candidate_block = DocumentBlock(
                block_id=f"candidate_{digest}",
                type=anchor.type if anchor is not None else (
                    "TABLE" if target_side.location.table_index is not None else "PARAGRAPH"
                ),
                order=anchor.order if anchor is not None else len(target.blocks),
                raw_text=text,
                normalized_text=normalize_text(text),
                location=target_side.location,
            )
            existing = candidates.get(key)
            if existing is None:
                candidates[key] = TextExtractionCandidate(
                    block=candidate_block,
                    diff_ids=(diff.diff_id,),
                    context_units=context,
                )
            else:
                candidates[key] = TextExtractionCandidate(
                    block=existing.block,
                    diff_ids=tuple(dict.fromkeys([*existing.diff_ids, diff.diff_id])),
                    context_units=tuple(
                        {
                            str(item["context_id"]): item
                            for item in [*existing.context_units, *context]
                        }.values()
                    ),
                )
    return list(candidates.values())


_ZERO_WIDTH_CHARS = frozenset(
    {
        "\u200b",
        "\u200c",
        "\u200d",
        "\u2060",
        "\ufeff",
    }
)
_BREAK_TAG = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)


def _normalised_text_with_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Normalize only presentation differences and retain source spans.

    This is deliberately not a fuzzy matcher.  Every emitted normalized
    character points to the original character(s), so a unique hit can be
    returned as an exact source slice and all ambiguous hits are rejected.
    """

    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        tag = _BREAK_TAG.match(text, index)
        if tag:
            chars.append(" ")
            spans.append((index, tag.end()))
            index = tag.end()
            continue
        source_char = text[index]
        if source_char in _ZERO_WIDTH_CHARS or source_char.isspace():
            normalized = " "
        else:
            normalized = unicodedata.normalize("NFKC", source_char)
        for char in normalized:
            chars.append(char)
            spans.append((index, index + 1))
        index += 1

    collapsed_chars: list[str] = []
    collapsed_spans: list[tuple[int, int]] = []
    for char, span in zip(chars, spans, strict=True):
        if char == " " and collapsed_chars and collapsed_chars[-1] == " ":
            start, _end = collapsed_spans[-1]
            collapsed_spans[-1] = (start, span[1])
            continue
        collapsed_chars.append(char)
        collapsed_spans.append(span)
    return "".join(collapsed_chars), collapsed_spans


def match_quote_to_source(source: str, quote: str) -> str | None:
    """Return a uniquely grounded original slice, or ``None``.

    Exact matching is attempted first.  The fallback permits only NFKC and
    presentation whitespace/``<br>``/zero-width normalization.  Rewritten
    text and ambiguous matches never pass this function.
    """

    def unique_slice(haystack: str, needle: str, spans: list[tuple[int, int]] | None) -> str | None:
        if not needle:
            return None
        positions: list[int] = []
        cursor = haystack.find(needle)
        while cursor >= 0:
            positions.append(cursor)
            cursor = haystack.find(needle, cursor + 1)
        if len(positions) != 1:
            return None
        start = positions[0]
        if spans is None:
            return source[start : start + len(needle)]
        end = start + len(needle)
        return source[spans[start][0] : spans[end - 1][1]]

    exact = unique_slice(source, quote, None)
    if exact is not None:
        return exact
    normalized_source, source_spans = _normalised_text_with_spans(source)
    normalized_quote, _quote_spans = _normalised_text_with_spans(quote)
    return unique_slice(normalized_source, normalized_quote, source_spans)


def _unique_grounded_quote(sources: list[str], quote: str) -> str | None:
    matches = [match_quote_to_source(source, quote) for source in sources]
    matches = [match for match in matches if match is not None]
    return next(iter(set(matches))) if len(set(matches)) == 1 else None


def rehydrate_fact_evidence(
    document: ParsedDocument,
    facts: list[FactCandidate],
) -> list[FactCandidate]:
    """Re-anchor compact facts to the complete parsed document.

    Candidate-based target batches may contain a diff fragment rather than the
    complete paragraph.  Before Reduce or checkpoint reuse, resolve the
    program-owned location against the full document and retain only a unique
    exact/format-equivalent source slice.
    """

    evidence = _evidence_at(document)
    physical_locations = _physical_locations_at(document)
    rehydrated: list[FactCandidate] = []
    for fact in facts:
        bound_location = _bind_physical_page(fact.location, physical_locations)
        sources = evidence.get(location_key(bound_location), [])
        grounded_raw = _unique_grounded_quote(sources, fact.raw_value)
        grounded_evidence = _unique_grounded_quote(sources, fact.evidence_text)
        if grounded_raw is None:
            raise EvidenceValidationError(
                "fact evidence is not present at the declared document location",
                code="FACT_VALUE_NOT_GROUNDED",
            )
        # A legacy candidate checkpoint can contain a fragment that was valid
        # in the diff slice but not in the complete paragraph.  The raw value
        # is independently grounded above, so it is the only safe fallback
        # evidence fragment; never synthesize surrounding text.
        grounded_evidence = grounded_evidence or grounded_raw
        rehydrated.append(
            fact.model_copy(
                update={
                    "raw_value": grounded_raw,
                    "normalized_hint": normalize_text(grounded_raw),
                    "evidence_text": grounded_evidence,
                    "location": bound_location,
                }
            )
        )
    return rehydrated


def rehydrate_numeric_fact_evidence(
    document: ParsedDocument,
    facts: list[FactCandidate],
) -> list[FactCandidate]:
    """Re-anchor numeric facts by their program-owned declared location.

    Numeric raw values originate from the program scanner, which retains the
    candidate span.  A short value may occur more than once in that same
    paragraph or row, so requiring a unique substring match here would reject
    a valid candidate.  The location and raw value remain unchanged; only the
    evidence container is rehydrated from that exact location.
    """

    evidence = _evidence_at(document)
    physical_locations = _physical_locations_at(document)
    rehydrated: list[FactCandidate] = []
    for fact in facts:
        bound_location = _bind_physical_page(fact.location, physical_locations)
        sources = evidence.get(location_key(bound_location), [])
        source = next(
            (
                item
                for item in sources
                if normalize_text(fact.raw_value) in normalize_text(item)
            ),
            None,
        )
        if source is None:
            raise EvidenceValidationError(
                "numeric fact value is not present at the declared document location",
                code="FACT_VALUE_NOT_GROUNDED",
            )
        rehydrated.append(
            fact.model_copy(
                update={
                    "normalized_hint": normalize_text(fact.raw_value),
                    "evidence_text": source,
                    "location": bound_location,
                }
            )
        )
    return rehydrated


def expand_numeric_candidate_response(
    payload: dict[str, Any], value: Any
) -> tuple[list[FactCandidate], set[int]]:
    response = NumericCandidateExtraction.model_validate(value)
    candidates = payload.get("numeric_candidates", [])
    expected = set(numeric_candidate_indexes(payload))
    actual = [item.candidate_index for item in response.items]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise EvidenceValidationError(
            "numeric candidates must be classified exactly once",
            code="NUMERIC_CANDIDATE_UNCLASSIFIED",
        )
    facts: list[FactCandidate] = []
    by_index = {item["candidate_index"]: item for item in candidates}
    evidence_by_location = {
        location_key(item["location"]): item.get("text", "")
        for item in payload.get("units", [])
        if isinstance(item, dict) and isinstance(item.get("location"), dict)
    }
    for item in response.items:
        if item.decision != "FACT":
            continue
        candidate = by_index[item.candidate_index]
        location = DocumentLocation.model_validate(candidate["location"])
        raw_value = str(candidate["raw_value"])
        facts.append(
            FactCandidate(
                field_key=item.semantic_key,
                display_name=item.display_name,
                value_type=item.value_type,
                raw_value=raw_value,
                normalized_hint=normalize_text(raw_value),
                source_file_id=str(payload.get("file_id", "")),
                evidence_text=evidence_by_location.get(location_key(location), raw_value),
                location=location,
                confidence=item.confidence,
            )
        )
    return facts, expected


def expand_text_fact_response(
    payload: dict[str, Any], value: Any
) -> list[FactCandidate]:
    response = TextFactExtraction.model_validate(value)
    # Numeric/date/identifier values are owned by numeric-v2.  The text model
    # can still emit one defensively, especially when a target-side fallback
    # contains a number, but it must not create a second fact identity at the
    # same location.  Numeric-v2 has already classified the program-scanned
    # candidate and remains the sole source for those values.
    response = response.model_copy(
        update={
            "items": [
                item
                for item in response.items
                if item.value_type in TEXT_FACT_VALUE_TYPES
            ]
        }
    )
    try:
        max_items = int(payload.get("requirements", {}).get("max_items", 12))
    except (AttributeError, TypeError, ValueError):
        max_items = 12
    max_items = max(1, min(max_items, 12))
    if response.has_more:
        raise EvidenceValidationError(
            "text fact batch reached its saturation limit",
            code="FACT_BATCH_SATURATED",
        )
    units = {
        item["unit_id"]: item for item in payload.get("units", []) if isinstance(item, dict)
    }
    facts: list[FactCandidate] = []
    identities: set[tuple[str, str]] = set()
    for item in response.items:
        unit = units.get(item.unit_id)
        if unit is None:
            raise EvidenceValidationError(
                "text fact references an unknown unit", code="FACT_UNIT_NOT_FOUND"
            )
        text = unit.get("text", "")
        if not isinstance(text, str):
            raise EvidenceValidationError(
                "text fact quote is not an exact substring",
                code="FACT_QUOTE_NOT_GROUNDED",
            )
        location = DocumentLocation.model_validate(unit["location"])
        grounded_quote = match_quote_to_source(text, item.quote)
        if grounded_quote is None:
            raise EvidenceValidationError(
                "text fact quote is not an exact substring or uniquely grounded",
                code="FACT_QUOTE_NOT_GROUNDED",
            )
        identity = (item.semantic_key, item.unit_id)
        if identity in identities:
            raise EvidenceValidationError(
                "text fact identity is duplicated", code="FACT_IDENTITY_DUPLICATED"
            )
        identities.add(identity)
        facts.append(
            FactCandidate(
                field_key=item.semantic_key,
                display_name=item.display_name,
                value_type=item.value_type,
                raw_value=grounded_quote,
                normalized_hint=normalize_text(grounded_quote),
                source_file_id=str(payload.get("file_id", "")),
                evidence_text=grounded_quote,
                location=location,
                confidence=item.confidence,
            )
        )
    return facts


def filter_text_fact_evidence(
    document: ParsedDocument,
    payload: dict[str, Any],
    value: Any,
) -> tuple[list[FactCandidate], dict[str, int]]:
    """Keep only text candidates that survive deterministic evidence checks.

    Text extraction is a candidate generator. A valid response may still
    contain one hallucinated quote or a location that cannot be rehydrated
    against the complete document. Those candidates are discarded
    independently; the remaining candidates, including an empty set, remain
    a valid result for the structural unit. Response-level schema and
    saturation failures stay strict because they do not identify an
    independently safe candidate set.
    """

    if payload.get("file_id") != document.file_id:
        raise EvidenceValidationError(
            "text fact payload does not belong to the parsed document",
            code="FACT_SOURCE_FILE_MISMATCH",
        )

    response = TextFactExtraction.model_validate(value)
    response = response.model_copy(
        update={
            "items": [
                item
                for item in response.items
                if item.value_type in TEXT_FACT_VALUE_TYPES
            ]
        }
    )
    try:
        max_items = int(payload.get("requirements", {}).get("max_items", 12))
    except (AttributeError, TypeError, ValueError):
        max_items = 12
    max_items = max(1, min(max_items, 12))
    if response.has_more:
        raise EvidenceValidationError(
            "text fact batch reached its saturation limit",
            code="FACT_BATCH_SATURATED",
        )

    units = {
        item["unit_id"]: item
        for item in payload.get("units", [])
        if isinstance(item, dict)
    }
    discarded: dict[str, int] = {}
    accepted: list[FactCandidate] = []
    seen_model_identities: set[tuple[str, str]] = set()
    seen_fact_values: dict[tuple[Any, ...], str] = {}

    def discard(code: str) -> None:
        discarded[code] = discarded.get(code, 0) + 1

    for item in response.items:
        model_identity = (item.semantic_key, item.unit_id)
        if model_identity in seen_model_identities:
            discard("FACT_IDENTITY_DUPLICATED")
            continue

        unit = units.get(item.unit_id)
        if unit is None:
            discard("FACT_UNIT_NOT_FOUND")
            continue
        source_text = unit.get("text", "")
        if not isinstance(source_text, str):
            discard("FACT_QUOTE_NOT_GROUNDED")
            continue
        if not isinstance(item.quote, str) or not item.quote:
            discard("FACT_QUOTE_NOT_GROUNDED")
            continue
        grounded_quote = match_quote_to_source(source_text, item.quote)
        if grounded_quote is None:
            discard("FACT_QUOTE_NOT_GROUNDED")
            continue
        try:
            location = DocumentLocation.model_validate(unit["location"])
            fact = FactCandidate(
                field_key=item.semantic_key,
                display_name=item.display_name,
                value_type=item.value_type,
                raw_value=grounded_quote,
                normalized_hint=normalize_text(grounded_quote),
                source_file_id=str(payload.get("file_id", "")),
                evidence_text=grounded_quote,
                location=location,
                confidence=item.confidence,
            )
            fact = rehydrate_fact_evidence(document, [fact])[0]
        except EvidenceValidationError as exc:
            discard(exc.code)
            continue
        except (TypeError, ValueError):
            discard("LLM_RESPONSE_SCHEMA_INVALID")
            continue

        identity = (
            fact.field_key,
            fact.source_file_id,
            fact.value_type,
            location_key(fact.location),
        )
        previous = seen_fact_values.get(identity)
        if previous is not None:
            if previous != fact.raw_value:
                discard("FACT_IDENTITY_CONFLICT")
            else:
                discard("FACT_IDENTITY_DUPLICATED")
            continue
        seen_fact_values[identity] = fact.raw_value
        seen_model_identities.add(model_identity)
        accepted.append(fact)

    return accepted, discarded


def build_document_overview_payload(
    document: ParsedDocument,
    *,
    max_blocks: int = 64,
    max_chars: int = 6000,
) -> dict[str, Any]:
    """Build a bounded outline for the one-time document profile call."""

    if not document.blocks:
        raise EvidenceValidationError("document has no structural units")
    max_blocks = max(1, max_blocks)
    max_chars = max(1, max_chars)
    selected: list[DocumentBlock] = []
    seen: set[str] = set()
    # Put the opening context first so a tight profile budget never loses the
    # document title/parties before clause headings are considered.
    candidates = list(document.blocks[:4])
    candidates.extend(
        block
        for block in document.blocks
        if block.location.section or block.type in {"TABLE", "HEADER"}
    )
    candidates.extend(document.blocks[-4:])
    for block in candidates:
        if block.block_id in seen:
            continue
        seen.add(block.block_id)
        selected.append(block)
    if len(selected) > max_blocks:
        # Keep both the opening/title context and the closing/signature context
        # when a document contains many clause headings.
        head_count = (max_blocks + 1) // 2
        selected = selected[:head_count] + selected[-(max_blocks - head_count) :]
    overview_blocks: list[dict[str, Any]] = []
    remaining = max_chars
    for block in selected:
        text = block.raw_text[: max(1, min(len(block.raw_text), remaining))]
        if not text:
            continue
        overview_blocks.append(
            {
                "unit_id": stable_unit_id(block),
                "type": block.type,
                "text": text,
                "location": compact_location(block.location),
            }
        )
        remaining -= len(text)
        if remaining <= 0:
            break
    return {
        "file_id": document.file_id,
        "role": document.role,
        "overview_blocks": overview_blocks,
        "extraction_requirements": {
            "return_only_document_overview": True,
            "document_kind_is_open_ended": True,
            "identity_is_program_owned": True,
        },
    }


def _fact_batch_units(
    document: ParsedDocument, blocks: list[DocumentBlock]
) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for block in blocks:
        units.append(
            {
                "unit_id": stable_unit_id(block),
                "block_id": block.block_id,
                "type": block.type,
                "text": block.raw_text,
                "location": compact_location(block.location),
            }
        )
    return units


def build_fact_batch_payload(
    document: ParsedDocument,
    blocks: list[DocumentBlock],
    *,
    batch_id: str,
    max_facts: int = MAX_COMPACT_FACTS,
    estimated_output_tokens: int = DEFAULT_ESTIMATED_OUTPUT_TOKEN_LIMIT,
) -> dict[str, Any]:
    candidates = numeric_candidates(blocks)
    indexed_candidates = [
        {"candidate_index": index, **candidate}
        for index, candidate in enumerate(candidates, start=1)
    ]
    evidence_blocks: list[dict[str, Any]] = []
    for block in blocks:
        if block.table is None:
            continue
        for row in block.table.rows:
            evidence_blocks.append(
                {
                    "block_id": f"{block.block_id}_row_{row.row:04d}",
                    "type": "TABLE_ROW",
                    "text": "\t".join(cell.raw_text for cell in row.cells),
                    "location": compact_location(
                        DocumentLocation(table_index=block.table.table_index, row=row.row)
                    ),
                }
            )
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
                        "block_id": f"{block.block_id}_r{row.row:04d}_c{column:04d}",
                        "type": "TABLE_CELL",
                        "text": cell.raw_text,
                        "location": compact_location(location),
                    }
                )
    return {
        "file_id": document.file_id,
        "role": document.role,
        "batch_id": batch_id,
        "units": _fact_batch_units(document, blocks),
        "evidence_blocks": evidence_blocks,
        "numeric_candidates": indexed_candidates,
        "numeric_candidate_metrics": numeric_candidate_metrics(blocks),
        "extraction_requirements": {
            "return_only_facts_and_numeric_candidate_decisions": True,
            "facts_max_items": max_facts,
            "estimated_output_tokens": estimated_output_tokens,
            "identity_is_program_owned": True,
            "evidence_is_program_owned": True,
            "table_positions_must_be_from_input": True,
        },
    }


def plan_document_batches(
    document: ParsedDocument,
    *,
    max_payload_chars: int,
    max_numeric_candidates: int,
    max_facts: int = MAX_COMPACT_FACTS,
    max_output_tokens: int = 6144,
    estimated_output_token_limit: int = DEFAULT_ESTIMATED_OUTPUT_TOKEN_LIMIT,
) -> list[dict[str, Any]]:
    """Plan leaf batches without truncating structural units or candidates."""

    max_unit_chars = max(1000, max_payload_chars - 1500)
    units = extraction_units(document, max_unit_chars=max_unit_chars)
    planned: list[dict[str, Any]] = []
    current: list[DocumentBlock] = []

    def describe(blocks: list[DocumentBlock]) -> dict[str, Any]:
        batch_id = stable_batch_id(document.sha256, blocks)
        candidates = numeric_candidates(blocks)
        estimated = estimate_extraction_output_tokens(max_facts, len(candidates))
        payload = build_fact_batch_payload(
            document,
            blocks,
            batch_id=batch_id,
            max_facts=max_facts,
            estimated_output_tokens=estimated,
        )
        return {
            "batch_id": batch_id,
            "document_id": document.file_id,
            "blocks": blocks,
            "unit_ids": [stable_unit_id(block) for block in blocks],
            "payload": payload,
            "numeric_candidate_count": len(candidates),
            "estimated_output_tokens": estimated,
            "depth": 0,
            "parent_batch_id": None,
        }

    for unit in units:
        candidate = [*current, unit]
        candidate_payload = describe(candidate)
        over = (
            extraction_payload_chars(candidate_payload["payload"]) > max_payload_chars
            or candidate_payload["numeric_candidate_count"] > max_numeric_candidates
            or candidate_payload["estimated_output_tokens"]
            > min(estimated_output_token_limit, max_output_tokens)
        )
        if current and over:
            planned.append(describe(current))
            current = []
            candidate = [unit]
            candidate_payload = describe(candidate)
        if (
            extraction_payload_chars(candidate_payload["payload"]) > max_payload_chars
            or candidate_payload["numeric_candidate_count"] > max_numeric_candidates
            or candidate_payload["estimated_output_tokens"]
            > min(estimated_output_token_limit, max_output_tokens)
        ):
            raise EvidenceValidationError("single extraction unit exceeds batch limits")
        current.append(unit)
    if current:
        planned.append(describe(current))
    return planned


def plan_simplified_document_batches(
    document: ParsedDocument,
    *,
    max_payload_chars: int,
    max_numeric_candidates: int = 24,
    estimated_output_token_limit: int = 2000,
    extraction_version: str = EXTRACTION_VERSION,
) -> list[dict[str, Any]]:
    """Plan one paired numeric/text batch per content slice.

    The two model chains share the same immutable units and stable batch ID.
    Planning measures each actual request payload, so reducing the protocol
    does not rely on an optimistic character estimate.
    """

    units = extraction_units(
        document,
        max_unit_chars=max(1000, max_payload_chars - 1800),
    )
    planned: list[dict[str, Any]] = []

    def describe(blocks: list[DocumentBlock]) -> dict[str, Any]:
        batch_id = stable_batch_id(document.sha256, blocks, extraction_version)
        numeric_payload = build_numeric_candidate_payload(
            document, blocks, batch_id=batch_id
        )
        text_payload = build_text_fact_payload(document, blocks, batch_id=batch_id)
        count = len(numeric_payload["numeric_candidates"])
        estimate = estimate_simplified_output_tokens(
            numeric_candidate_count=count, max_text_facts=min(12, len(blocks))
        )
        return {
            "batch_id": batch_id,
            "document_id": document.file_id,
            "file_sha256": document.sha256,
            "blocks": blocks,
            "unit_ids": [stable_unit_id(block) for block in blocks],
            "numeric_payload": numeric_payload,
            "text_payload": text_payload,
            "payload": numeric_payload,
            "numeric_candidate_count": count,
            "estimated_output_tokens": estimate,
            "depth": 0,
            "parent_batch_id": None,
            "extraction_version": extraction_version,
        }

    current: list[DocumentBlock] = []
    for unit in units:
        candidate = [*current, unit]
        description = describe(candidate)
        over = (
            extraction_payload_chars(description["numeric_payload"]) > max_payload_chars
            or extraction_payload_chars(description["text_payload"]) > max_payload_chars
            or description["numeric_candidate_count"] > max_numeric_candidates
            or description["estimated_output_tokens"] > estimated_output_token_limit
        )
        if current and over:
            planned.append(describe(current))
            current = []
            description = describe([unit])
        if (
            extraction_payload_chars(description["numeric_payload"]) > max_payload_chars
            or extraction_payload_chars(description["text_payload"]) > max_payload_chars
            or description["numeric_candidate_count"] > max_numeric_candidates
            or description["estimated_output_tokens"] > estimated_output_token_limit
        ):
            raise EvidenceValidationError("single extraction unit exceeds simplified batch limits")
        current.append(unit)
    if current:
        planned.append(describe(current))
    return planned


def _plan_independent_batches(
    document: ParsedDocument,
    *,
    chain: str,
    extraction_version: str,
    max_payload_chars: int,
    max_numeric_candidates: int,
    max_text_units: int,
    estimated_output_token_limit: int,
    max_text_facts: int = 12,
    max_numeric_units: int | None = None,
    units_override: list[DocumentBlock] | None = None,
    text_context_by_block_id: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Plan one independently checkpointed numeric or text chain.

    Text batches intentionally have a structural-unit ceiling in addition to
    the payload ceiling.  This keeps a dense text response away from the
    twelve-item saturation boundary while leaving the numeric planner's
    12,000-character/24-candidate policy unchanged.
    """

    # An explicitly supplied empty override is meaningful: a target document
    # with no template-side text deltas must not fall back to a full-document
    # text scan.
    units = (
        units_override
        if units_override is not None
        else extraction_units(
            document,
            max_unit_chars=max(1000, max_payload_chars - 1800),
        )
    )
    text_context_by_block_id = text_context_by_block_id or {}
    planned: list[dict[str, Any]] = []

    def describe(blocks: list[DocumentBlock]) -> dict[str, Any]:
        batch_id = stable_batch_id(document.sha256, blocks, extraction_version)
        if chain == "numeric":
            payload = build_numeric_candidate_payload(document, blocks, batch_id=batch_id)
            estimated = estimate_simplified_output_tokens(
                numeric_candidate_count=len(payload["numeric_candidates"]),
                max_text_facts=0,
            )
            count = len(payload["numeric_candidates"])
        else:
            context_units: list[dict[str, Any]] = []
            seen_context_ids: set[str] = set()
            for block in blocks:
                for item in text_context_by_block_id.get(block.block_id, []):
                    context_id = str(item.get("context_id", ""))
                    if context_id and context_id not in seen_context_ids:
                        seen_context_ids.add(context_id)
                        context_units.append(item)
            payload = build_text_fact_payload(
                document,
                blocks,
                batch_id=batch_id,
                context_units=context_units,
                max_items=max_text_facts,
            )
            # The response is capped by facts, not by unit count.  Unit count
            # remains a separate safety guard; using it in the token estimate
            # would fragment a document into needless calls.
            estimated = estimate_simplified_output_tokens(
                numeric_candidate_count=0,
                max_text_facts=max_text_facts,
            )
            count = 0
        return {
            "batch_id": batch_id,
            "document_id": document.file_id,
            "file_sha256": document.sha256,
            "blocks": blocks,
            "unit_ids": [stable_unit_id(block) for block in blocks],
            "payload": payload,
            "chain": chain,
            "numeric_candidate_count": count,
            "estimated_output_tokens": estimated,
            "depth": 0,
            "parent_batch_id": None,
            "extraction_version": extraction_version,
            "context_units_by_block_id": {
                block.block_id: text_context_by_block_id.get(block.block_id, [])
                for block in blocks
                if text_context_by_block_id.get(block.block_id)
            },
        }

    def exceeds(plan: dict[str, Any]) -> bool:
        return (
            extraction_payload_chars(plan["payload"]) > max_payload_chars
            or plan["numeric_candidate_count"] > max_numeric_candidates
            or plan["estimated_output_tokens"] > estimated_output_token_limit
            or (
                chain == "numeric"
                and max_numeric_units is not None
                and len(plan["blocks"]) > max_numeric_units
            )
            or (chain == "text" and len(plan["blocks"]) > max_text_units)
        )

    current: list[DocumentBlock] = []
    for unit in units:
        candidate = describe([*current, unit])
        if current and exceeds(candidate):
            planned.append(describe(current))
            current = []
            candidate = describe([unit])
        if exceeds(candidate):
            raise EvidenceValidationError("single independent extraction unit exceeds batch limits")
        current.append(unit)
    if current:
        planned.append(describe(current))
    return planned


def plan_numeric_document_batches(
    document: ParsedDocument,
    *,
    max_payload_chars: int = 12000,
    max_numeric_candidates: int = 24,
    max_numeric_units: int = 6,
    estimated_output_token_limit: int = 2000,
    include_empty: bool = False,
) -> list[dict[str, Any]]:
    units = _numeric_planning_units(
        document,
        max_unit_chars=max(1000, max_payload_chars - 1800),
        max_numeric_candidates=max_numeric_candidates,
    )
    all_plans = _plan_independent_batches(
        document,
        chain="numeric",
        extraction_version=NUMERIC_EXTRACTION_VERSION,
        max_payload_chars=max_payload_chars,
        max_numeric_candidates=max_numeric_candidates,
        max_text_units=10**9,
        max_numeric_units=max_numeric_units,
        estimated_output_token_limit=estimated_output_token_limit,
        units_override=units,
    )
    # A structure with no numeric candidate is not a model task.  Keep the
    # pre-filter count only as an internal compatibility value: it is already
    # part of historical payload digests, while the returned plan list is the
    # actual work/coverage set used by the current planner.
    checkpoint_planned_batch_count = len(all_plans)
    if include_empty:
        for plan in all_plans:
            plan["checkpoint_planned_batch_count"] = checkpoint_planned_batch_count
        return all_plans
    plans = [
        plan for plan in all_plans if int(plan.get("numeric_candidate_count", 0)) > 0
    ]
    for plan in plans:
        plan["checkpoint_planned_batch_count"] = checkpoint_planned_batch_count
    return plans


def plan_text_document_batches(
    document: ParsedDocument,
    *,
    max_payload_chars: int = 12000,
    max_text_units: int = 16,
    estimated_output_token_limit: int = 2000,
    max_text_facts: int = 12,
) -> list[dict[str, Any]]:
    return _plan_independent_batches(
        document,
        chain="text",
        extraction_version=TEXT_EXTRACTION_VERSION,
        max_payload_chars=max_payload_chars,
        max_numeric_candidates=10**9,
        max_text_units=max_text_units,
        estimated_output_token_limit=estimated_output_token_limit,
        max_text_facts=max_text_facts,
    )


def plan_text_candidate_batches(
    document: ParsedDocument,
    candidates: list[TextExtractionCandidate],
    *,
    max_payload_chars: int = 12000,
    max_candidates: int = 8,
    estimated_output_token_limit: int = 2000,
    max_text_facts: int = 12,
) -> list[dict[str, Any]]:
    blocks = [candidate.block for candidate in candidates]
    contexts = {
        candidate.block.block_id: list(candidate.context_units)
        for candidate in candidates
    }
    return _plan_independent_batches(
        document,
        chain="text",
        extraction_version=TEXT_EXTRACTION_VERSION,
        max_payload_chars=max_payload_chars,
        max_numeric_candidates=10**9,
        max_text_units=max_candidates,
        estimated_output_token_limit=estimated_output_token_limit,
        max_text_facts=max_text_facts,
        units_override=blocks,
        text_context_by_block_id=contexts,
    )


def expand_document_overview(
    payload: dict[str, Any],
    value: Any,
) -> DocumentFactExtraction:
    overview = CompactDocumentOverview.model_validate(value)
    allowed = {
        location_key(item["location"])
        for item in payload.get("overview_blocks", [])
        if isinstance(item, dict) and isinstance(item.get("location"), dict)
    }
    locations = [
        location
        for location in overview.evidence_locations
        if location_key(location) in allowed
    ]
    if len(locations) != len(overview.evidence_locations) or not locations:
        raise EvidenceValidationError(
            "document overview evidence location is not in outline",
            code="PROFILE_LOCATION_NOT_FOUND",
        )
    return DocumentFactExtraction(
        profile=DocumentProfile(
            file_id=str(payload["file_id"]),
            document_kind=overview.document_kind,
            title=overview.title,
            confidence=overview.confidence,
            evidence_locations=locations,
        ),
        facts=[],
        missing_field_keys=[],
    )


def expand_fact_batch(
    payload: dict[str, Any],
    value: Any,
) -> DocumentFactExtraction:
    compact = CompactFactBatchExtraction.model_validate(value)
    candidates = payload.get("numeric_candidates", [])
    expected_indices = set(range(1, len(candidates) + 1))
    decisions = compact.numeric_candidate_decisions
    actual_indices = [item.candidate_index for item in decisions]
    if set(actual_indices) != expected_indices or len(actual_indices) != len(set(actual_indices)):
        raise EvidenceValidationError(
            "numeric candidate classification is incomplete",
            code="NUMERIC_CANDIDATE_UNCLASSIFIED",
        )
    fact_indices: list[int] = [index for fact in compact.facts for index in fact.candidate_indices]
    if len(fact_indices) != len(set(fact_indices)) or not set(fact_indices) <= expected_indices:
        raise EvidenceValidationError(
            "numeric candidate fact references are invalid",
            code="NUMERIC_CANDIDATE_INVALID",
        )
    decision_by_index = {item.candidate_index: item.decision for item in decisions}
    for index in expected_indices:
        if (decision_by_index[index] == "FACT") != (index in fact_indices):
            raise EvidenceValidationError(
                "numeric candidate disposition does not match facts",
                code="NUMERIC_CANDIDATE_INVALID",
            )

    evidence_by_location: dict[tuple[object, ...], list[str]] = defaultdict(list)
    allowed_locations: set[tuple[object, ...]] = set()
    for item in [*payload.get("units", []), *payload.get("evidence_blocks", [])]:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        location = item.get("location")
        if not isinstance(location, dict):
            continue
        key = location_key(location)
        allowed_locations.add(key)
        evidence_by_location[key].append(item["text"])
    facts: list[FactCandidate] = []
    for fact in compact.facts:
        fact_location = location_key(fact.location)
        if fact_location not in allowed_locations:
            raise EvidenceValidationError(
                "fact evidence location does not exist",
                code="FACT_LOCATION_NOT_FOUND",
            )
        evidence_text = next(
            (
                text
                for text in evidence_by_location[fact_location]
                if normalize_text(fact.raw_value) in normalize_text(text)
            ),
            None,
        )
        if evidence_text is None:
            raise EvidenceValidationError(
                "fact value is not grounded", code="FACT_VALUE_NOT_GROUNDED"
            )
        facts.append(
            FactCandidate(
                **fact.model_dump(mode="python", exclude={"candidate_indices"}),
                source_file_id=str(payload["file_id"]),
                evidence_text=evidence_text,
                normalized_hint=None,
            )
        )
    first_location = next(iter(allowed_locations), (None, 0, None, None, None))
    return DocumentFactExtraction(
        profile=DocumentProfile(
            file_id=str(payload["file_id"]),
            document_kind="UNKNOWN",
            title=None,
            confidence=0.0,
            evidence_locations=[DocumentLocation(
                page=first_location[0],
                paragraph_index=first_location[1],
                table_index=first_location[2],
                row=first_location[3],
                column=first_location[4],
            )],
        ),
        facts=facts,
        missing_field_keys=[],
    )


def _table_row_unit(
    block: DocumentBlock,
    row: TableRow,
    *,
    cells: list[Any] | None = None,
    group_index: int | None = None,
) -> DocumentBlock:
    """Represent one table row or a deterministic column group."""

    table_index = block.table.table_index if block.table is not None else None
    if table_index is None:
        raise EvidenceValidationError("table row is missing table_index")
    selected_cells = cells or row.cells
    row_text = "\t".join(cell.raw_text for cell in selected_cells)
    row_location = DocumentLocation(
        table_index=table_index,
        row=row.row,
        column=(selected_cells[0].location.column if cells else None),
    )
    if block.table is None:
        raise EvidenceValidationError("table row has no parent table")
    row_table = ParsedTable(
        table_index=table_index,
        rows=[TableRow(row=row.row, cells=selected_cells)],
    )
    suffix = f"_g{group_index:04d}" if group_index is not None else ""
    return DocumentBlock(
        block_id=f"{block.block_id}_r{row.row:06d}{suffix}",
        type="TABLE",
        order=block.order,
        raw_text=row_text,
        normalized_text=normalize_text(row_text),
        location=row_location,
        table=row_table,
    )


def split_table_text_unit(block: DocumentBlock) -> list[DocumentBlock]:
    """Split one failed table-row text unit into independently grounded cells.

    Rows remain the normal planning unit.  This narrow recovery path is only
    used after a row-level quote/identity failure, where repeated merged-cell
    text can make an otherwise valid quote ambiguous.  Each child retains the
    original table, row and column coordinates.
    """

    if block.table is None or len(block.table.rows) != 1:
        return []
    row = block.table.rows[0]
    if len(row.cells) < 2:
        return []
    return [
        _table_row_unit(block, row, cells=[cell], group_index=column)
        for column, cell in enumerate(row.cells)
    ]


_NUMERIC_STRUCTURE_SPLIT_CHARS = frozenset("。！？；!?;\n\r")
_NUMERIC_CLAUSE_SPLIT_CHARS = frozenset("，,、")


def _split_text_fragments(text: str, separators: frozenset[str]) -> list[str]:
    fragments: list[str] = []
    start = 0
    for index, char in enumerate(text):
        if char not in separators:
            continue
        fragment = text[start : index + 1].strip()
        if fragment:
            fragments.append(fragment)
        start = index + 1
    tail = text[start:].strip()
    if tail:
        fragments.append(tail)
    return fragments if len(fragments) > 1 else []


def _split_numeric_text_fragments(text: str) -> list[str]:
    fragments = _split_text_fragments(text, _NUMERIC_STRUCTURE_SPLIT_CHARS)
    if not fragments:
        fragments = _split_text_fragments(text, _NUMERIC_CLAUSE_SPLIT_CHARS)
    if len(fragments) > 1:
        return fragments
    stripped = text.strip()
    if len(stripped) < 2:
        return []

    # Preserve every character while preferring a safe lexical boundary.  If
    # the nearest boundary falls inside a number, move to the closest safe
    # boundary instead of manufacturing a new numeric candidate.
    midpoint = len(text) // 2
    boundaries = [
        index
        for index, char in enumerate(text, start=1)
        if char.isspace() or char in _NUMERIC_CLAUSE_SPLIT_CHARS
    ]
    boundary = min(boundaries, key=lambda index: abs(index - midpoint)) if boundaries else midpoint
    boundary = max(1, min(boundary, len(text) - 1))
    if boundary < len(text) and text[boundary - 1].isdigit() and text[boundary].isdigit():
        safe = [
            index
            for index in boundaries
            if not (text[index - 1].isdigit() and index < len(text) and text[index].isdigit())
        ]
        if not safe:
            return []
        boundary = min(safe, key=lambda index: abs(index - midpoint))
    left = text[:boundary].strip()
    right = text[boundary:].strip()
    return [left, right] if left and right else []


def split_numeric_structure_unit(block: DocumentBlock) -> list[DocumentBlock]:
    """Split one numeric input without dropping source text or candidates."""

    if block.table is not None:
        if len(block.table.rows) > 1:
            return [_table_row_unit(block, row) for row in block.table.rows]
        if len(block.table.rows) == 1 and len(block.table.rows[0].cells) > 1:
            return split_table_text_unit(block)

    fragments = _split_numeric_text_fragments(block.raw_text)
    return [
        block.model_copy(
            update={
                "block_id": f"{block.block_id}_n{index:04d}",
                "raw_text": fragment,
                "normalized_text": normalize_text(fragment),
            }
        )
        for index, fragment in enumerate(fragments)
    ]


def _numeric_planning_units(
    document: ParsedDocument,
    *,
    max_unit_chars: int,
    max_numeric_candidates: int,
) -> list[DocumentBlock]:
    """Expand dense structures before the numeric batch planner runs.

    The candidate ceiling is a safety limit, not a licence to omit values. A
    row or paragraph that is too dense is recursively partitioned using the
    same table/row/cell and text-boundary rules used by truncation recovery.
    If the source has no safe boundary, fail before dispatching an oversized
    request.
    """

    def expand(block: DocumentBlock) -> list[DocumentBlock]:
        if len(numeric_candidates([block])) <= max_numeric_candidates:
            return [block]
        children = split_numeric_structure_unit(block)
        if not children or (
            len(children) == 1
            and children[0].raw_text.strip() == block.raw_text.strip()
        ):
            raise EvidenceValidationError(
                "single numeric structure exceeds candidate limit",
                code="NUMERIC_BATCH_TOO_LARGE",
            )
        expanded: list[DocumentBlock] = []
        for child in children:
            expanded.extend(expand(child))
        return expanded

    units = extraction_units(document, max_unit_chars=max_unit_chars)
    return [child for unit in units for child in expand(unit)]


def extraction_units(
    document: ParsedDocument,
    *,
    max_unit_chars: int | None = None,
) -> list[DocumentBlock]:
    """Return paragraphs and independently addressable table rows/column groups."""

    units: list[DocumentBlock] = []
    for block in document.blocks:
        if block.table is None:
            units.append(block)
            continue
        for row in block.table.rows:
            if max_unit_chars is None or len(
                "\t".join(cell.raw_text for cell in row.cells)
            ) <= max_unit_chars:
                units.append(_table_row_unit(block, row))
                continue
            group: list[Any] = []
            group_chars = 0
            group_index = 0
            for cell in row.cells:
                cell_chars = len(cell.raw_text) + (1 if group else 0)
                if group and group_chars + cell_chars > max_unit_chars:
                    units.append(
                        _table_row_unit(
                            block,
                            row,
                            cells=group,
                            group_index=group_index,
                        )
                    )
                    group = []
                    group_chars = 0
                    group_index += 1
                if len(cell.raw_text) > max_unit_chars:
                    raise EvidenceValidationError(
                        "single table cell exceeds extraction unit limit"
                    )
                group.append(cell)
                group_chars += cell_chars
            if group:
                units.append(
                    _table_row_unit(
                        block,
                        row,
                        cells=group,
                        group_index=group_index,
                    )
                )
    return units


def extraction_payload_chars(payload: dict[str, Any]) -> int:
    """Count the exact compact JSON payload sent as the model user message."""

    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def chunk_document(
    document: ParsedDocument,
    max_chars: int,
    *,
    max_numeric_candidates: int | None = None,
    max_payload_chars: int | None = None,
) -> list[list[DocumentBlock]]:
    chunks: list[list[DocumentBlock]] = []
    current: list[DocumentBlock] = []
    current_chars = 0
    current_numeric_candidates = 0
    for block in extraction_units(document):
        block_chars = len(block.raw_text)
        block_numeric_candidates = len(numeric_candidates([block]))
        over_numeric_limit = (
            max_numeric_candidates is not None
            and current_numeric_candidates + block_numeric_candidates > max_numeric_candidates
        )
        proposed = [*current, block]
        over_payload_limit = (
            max_payload_chars is not None
            and extraction_payload_chars(compact_extraction_payload(document, proposed))
            > max_payload_chars
        )
        if current and (
            current_chars + block_chars > max_chars
            or over_numeric_limit
            or over_payload_limit
        ):
            chunks.append(current)
            current = []
            current_chars = 0
            current_numeric_candidates = 0
            proposed = [block]
        if max_payload_chars is not None:
            single_payload_chars = extraction_payload_chars(
                compact_extraction_payload(document, [block])
            )
            if single_payload_chars > max_payload_chars:
                raise EvidenceValidationError(
                    "single extraction unit exceeds payload limit"
                )
        if (
            max_numeric_candidates is not None
            and block_numeric_candidates > max_numeric_candidates
        ):
            raise EvidenceValidationError(
                "single extraction unit exceeds numeric candidate limit"
            )
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
    evidence_items = [
        *payload.get("blocks", []),
        *payload.get("evidence_blocks", []),
    ]
    for item in evidence_items:
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


def _compact_review_blocks(
    document: ParsedDocument,
    facts: list[FactCandidate],
    concepts: list[SemanticConcept],
    specs: list[ValidationSpec],
) -> list[dict[str, Any]]:
    """Build bounded, exact evidence blocks for oversized review contexts."""

    evidence = _evidence_at(document)
    blocks: dict[tuple[tuple[object, ...], str], dict[str, Any]] = {}

    def add(location: DocumentLocation, text: str) -> None:
        if not text:
            return
        key = (location_key(location), text)
        digest = hashlib.sha256(
            json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:20]
        blocks.setdefault(
            key,
            {
                "block_id": f"review_evidence_{digest}",
                "type": "EVIDENCE",
                "text": text,
                "location": location.model_dump(mode="json", exclude_none=True),
            },
        )

    for fact in facts:
        # Fact evidence has already passed strict source rehydration and is
        # therefore the smallest safe representation for a review batch.
        add(fact.location, fact.evidence_text)
    for item in [*concepts, *specs]:
        for location in item.evidence_locations:
            source = next(iter(evidence.get(location_key(location), [])), None)
            if source is not None:
                add(location, source)
    return list(blocks.values())


def _compact_review_payload(
    document: ParsedDocument,
    facts: list[FactCandidate],
    concepts: list[SemanticConcept],
    specs: list[ValidationSpec],
) -> dict[str, Any]:
    """Keep review evidence exact while excluding oversized neighboring blocks."""

    return {
        "file_id": document.file_id,
        "role": document.role,
        "blocks": _compact_review_blocks(document, facts, concepts, specs),
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
    current_compact = False

    def flush_current() -> None:
        nonlocal current_blocks, current_facts, current_concepts, current_specs
        nonlocal current_compact
        if not (current_facts or current_concepts or current_specs):
            return
        batches.append(
            _compact_review_payload(
                document,
                current_facts,
                current_concepts,
                current_specs,
            )
            if current_compact
            else _review_payload(
                document,
                current_blocks,
                current_facts,
                current_concepts,
                current_specs,
            )
        )
        current_blocks = set()
        current_facts = []
        current_concepts = []
        current_specs = []
        current_compact = False

    for block_indexes, facts, concepts, specs in units:
        proposed = _review_payload(
            document,
            current_blocks | block_indexes,
            [*current_facts, *facts],
            [*current_concepts, *concepts],
            [*current_specs, *specs],
        )
        if current_compact:
            proposed = _compact_review_payload(
                document,
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
            compact_proposed = _compact_review_payload(
                document,
                [*current_facts, *facts],
                [*current_concepts, *concepts],
                [*current_specs, *specs],
            )
            if _payload_chars(compact_proposed) <= max_chars:
                current_compact = True
                current_blocks |= block_indexes
                current_facts.extend(facts)
                current_concepts.extend(concepts)
                current_specs.extend(specs)
                continue
            flush_current()
        single = _review_payload(document, block_indexes, facts, concepts, specs)
        if _payload_chars(single) > max_chars:
            single = _compact_review_payload(document, facts, concepts, specs)
            if _payload_chars(single) > max_chars:
                raise EvidenceValidationError("single review unit exceeds review batch limit")
            current_compact = True
        current_blocks = set(block_indexes)
        current_facts = list(facts)
        current_concepts = list(concepts)
        current_specs = list(specs)
    flush_current()
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


def _logical_location_key(
    location: DocumentLocation | dict[str, Any],
) -> tuple[object, ...]:
    if isinstance(location, dict):
        location = DocumentLocation.model_validate(location)
    return (
        location.paragraph_index,
        location.table_index,
        location.row,
        location.column,
    )


def _location_pages(location: DocumentLocation) -> set[int]:
    pages = {
        page
        for page in (location.page, *location.physical_pages)
        if isinstance(page, int) and not isinstance(page, bool) and page >= 1
    }
    return pages


def _table_row_location(
    block: DocumentBlock,
    row: TableRow,
) -> DocumentLocation:
    pages: set[int] = set()
    for cell in row.cells:
        pages.update(_location_pages(cell.location))
    if not pages:
        pages.update(_location_pages(block.location))
    page = next(iter(pages)) if len(pages) == 1 else None
    return DocumentLocation(
        page=page,
        table_index=block.table.table_index if block.table is not None else None,
        row=row.row,
        physical_pages=(page,) if page is not None else (),
    )


def _physical_locations_at(
    document: ParsedDocument,
) -> dict[tuple[object, ...], list[DocumentLocation]]:
    locations: dict[tuple[object, ...], list[DocumentLocation]] = defaultdict(list)
    for block in document.blocks:
        locations[_logical_location_key(block.location)].append(block.location)
        if block.table is None:
            continue
        for row in block.table.rows:
            row_location = _table_row_location(block, row)
            locations[_logical_location_key(row_location)].append(row_location)
            for cell in row.cells:
                locations[_logical_location_key(cell.location)].append(cell.location)
    return locations


def _bind_physical_page(
    location: DocumentLocation,
    physical_locations: dict[tuple[object, ...], list[DocumentLocation]],
) -> DocumentLocation:
    if isinstance(location.page, int) and not isinstance(location.page, bool):
        return location
    candidates = physical_locations.get(_logical_location_key(location), [])
    pages = {page for candidate in candidates for page in _location_pages(candidate)}
    if len(pages) != 1:
        return location
    page = next(iter(pages))
    return location.model_copy(update={"page": page, "physical_pages": (page,)})


def bind_physical_page(
    document: ParsedDocument,
    location: DocumentLocation,
) -> DocumentLocation:
    """Restore one unambiguous parser-owned physical page to a logical location."""

    return _bind_physical_page(location, _physical_locations_at(document))


def rehydrate_extraction_page_locations(
    document: ParsedDocument,
    extraction: DocumentFactExtraction,
) -> DocumentFactExtraction:
    """Rebind cached logical extraction locations to current parser pagination."""

    physical_locations = _physical_locations_at(document)

    def bind(location: DocumentLocation) -> DocumentLocation:
        return _bind_physical_page(location, physical_locations)

    return extraction.model_copy(
        update={
            "profile": extraction.profile.model_copy(
                update={
                    "evidence_locations": [
                        bind(location) for location in extraction.profile.evidence_locations
                    ]
                }
            ),
            "facts": [
                fact.model_copy(
                    update={"location": bind(fact.location)}
                )
                for fact in extraction.facts
            ],
            "semantic_concepts": [
                concept.model_copy(
                    update={
                        "evidence_locations": [
                            bind(location) for location in concept.evidence_locations
                        ]
                    }
                )
                for concept in extraction.semantic_concepts
            ],
            "validation_specs": [
                spec.model_copy(
                    update={
                        "evidence_locations": [
                            bind(location) for location in spec.evidence_locations
                        ]
                    }
                )
                for spec in extraction.validation_specs
            ],
        }
    )


def _evidence_at(document: ParsedDocument) -> dict[tuple[object, ...], list[str]]:
    evidence: dict[tuple[object, ...], list[str]] = defaultdict(list)
    for block in document.blocks:
        evidence[location_key(block.location)].append(block.raw_text)
        if block.table:
            for row in block.table.rows:
                row_text = "\t".join(cell.raw_text for cell in row.cells)
                logical_row_location = DocumentLocation(
                    table_index=block.table.table_index, row=row.row
                )
                evidence[location_key(logical_row_location)].append(row_text)
                physical_row_location = _table_row_location(block, row)
                if location_key(physical_row_location) != location_key(
                    logical_row_location
                ):
                    evidence[location_key(physical_row_location)].append(row_text)
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
    fact_values_by_identity: dict[tuple[object, ...], str] = {}
    for extraction in extractions:
        for fact in extraction.facts:
            key = (
                fact.field_key,
                normalize_text(fact.raw_value),
                location_key(fact.location),
            )
            identity = (
                fact.field_key,
                fact.source_file_id,
                fact.value_type,
                location_key(fact.location),
            )
            normalized_value = normalize_text(fact.raw_value)
            previous_value = fact_values_by_identity.get(identity)
            if previous_value is not None and previous_value != normalized_value:
                raise EvidenceValidationError(
                    "conflicting duplicate fact identity",
                    code="FACT_IDENTITY_CONFLICT",
                )
            fact_values_by_identity[identity] = normalized_value
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
    include_required_missing: bool | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if include_required_missing is None:
        include_required_missing = include_uncertain
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
        if include_required_missing and required_missing_relations:
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
