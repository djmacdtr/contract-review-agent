from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from app.adapters.llm.schemas import FactCandidate
from app.draft_review.facts import compare_facts, normalize_fact, stable_fact_id

MAX_REFERENCE_FACTS_PER_TARGET = 5
MAX_TARGET_FACTS_PER_BATCH = 8

_NUMERIC_TYPES = {"MONEY", "PERCENTAGE", "RATE", "DURATION", "DATE", "NUMBER", "QUANTITY"}


def reference_fact_id(fact: FactCandidate) -> str:
    return stable_fact_id(fact)


def _tokens(value: str) -> set[str]:
    normalized = re.sub(r"\s+", "", value).casefold()
    latin = set(re.findall(r"[a-z0-9_]+", normalized))
    chinese = set(re.findall(r"[\u4e00-\u9fff]{2}", normalized))
    return latin | chinese


def _fact_text(fact: FactCandidate) -> str:
    return " ".join(
        value
        for value in (
            fact.field_key,
            fact.concept_id or "",
            fact.display_name,
            fact.raw_value,
            fact.normalized_hint or "",
            fact.evidence_text,
        )
        if value
    )


def _retrieval_score(target: FactCandidate, reference: FactCandidate) -> float:
    score = 0.0
    if target.field_key == reference.field_key:
        score += 12.0
    if target.concept_id and target.concept_id == reference.concept_id:
        score += 10.0
    if target.value_type == reference.value_type:
        score += 6.0
    elif target.value_type in _NUMERIC_TYPES and reference.value_type in _NUMERIC_TYPES:
        score += 2.0
    if target.display_name.casefold() == reference.display_name.casefold():
        score += 6.0
    target_tokens = _tokens(_fact_text(target))
    reference_tokens = _tokens(_fact_text(reference))
    if target_tokens and reference_tokens:
        score += 8.0 * len(target_tokens & reference_tokens) / len(target_tokens | reference_tokens)
    if normalize_fact(target) == normalize_fact(reference):
        score += 5.0
    deterministic = compare_facts(target, reference)
    if deterministic is not None:
        score += 2.0
    return score


def retrieve_reference_facts(
    target: FactCandidate,
    references: Iterable[FactCandidate],
    *,
    limit: int = MAX_REFERENCE_FACTS_PER_TARGET,
) -> list[FactCandidate]:
    ranked = sorted(
        ((_retrieval_score(target, fact), fact) for fact in references),
        key=lambda item: (-item[0], reference_fact_id(item[1])),
    )
    positive = [fact for score, fact in ranked if score > 0]
    return positive[:limit]


def compact_target_fact(target_fact_id: str, fact: FactCandidate) -> dict[str, Any]:
    return {
        "target_fact_id": target_fact_id,
        "field_key": fact.field_key,
        "concept_id": fact.concept_id,
        "display_name": fact.display_name,
        "value_type": fact.value_type,
        "raw_value": fact.raw_value,
        "normalized_hint": fact.normalized_hint,
        "evidence_text": fact.evidence_text,
    }


def compact_reference_fact(fact: FactCandidate) -> dict[str, Any]:
    return {
        "reference_fact_id": reference_fact_id(fact),
        "field_key": fact.field_key,
        "concept_id": fact.concept_id,
        "display_name": fact.display_name,
        "value_type": fact.value_type,
        "raw_value": fact.raw_value,
        "normalized_hint": fact.normalized_hint,
        "evidence_text": fact.evidence_text,
    }


def build_mapping_batches(
    target_facts: list[tuple[str, FactCandidate]],
    reference_facts: list[FactCandidate],
    *,
    reference_file_id: str,
    max_payload_chars: int,
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for target_fact_id, target in target_facts:
        candidates = retrieve_reference_facts(target, reference_facts)
        if not candidates:
            continue
        groups.append(
            {
                "target": compact_target_fact(target_fact_id, target),
                "references": [compact_reference_fact(fact) for fact in candidates],
            }
        )

    batches: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for group in groups:
        candidate = [*current, group]
        payload = {"reference_file_id": reference_file_id, "groups": candidate}
        if current and (
            len(candidate) > MAX_TARGET_FACTS_PER_BATCH
            or len(json.dumps(payload, ensure_ascii=False, default=str)) > max_payload_chars
        ):
            batches.append({"reference_file_id": reference_file_id, "groups": current})
            current = [group]
        else:
            current = candidate
    if current:
        batches.append({"reference_file_id": reference_file_id, "groups": current})
    return batches


def expected_mapping_pairs(payload: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (str(group["target"]["target_fact_id"]), str(reference["reference_fact_id"]))
        for group in payload.get("groups", [])
        for reference in group.get("references", [])
    }


def subset_mapping_payload(
    payload: dict[str, Any], pairs: set[tuple[str, str]]
) -> dict[str, Any]:
    groups: list[dict[str, Any]] = []
    for group in payload.get("groups", []):
        target_id = str(group["target"]["target_fact_id"])
        references = [
            reference
            for reference in group.get("references", [])
            if (target_id, str(reference["reference_fact_id"])) in pairs
        ]
        if references:
            groups.append({"target": group["target"], "references": references})
    return {"reference_file_id": payload["reference_file_id"], "groups": groups}
