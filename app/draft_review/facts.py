from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.adapters.llm.schemas import (
    DocumentFactExtraction,
    DocumentProfile,
    FactCandidate,
    SemanticConcept,
    ValidationSpec,
)
from app.documents.models import DocumentBlock, DocumentLocation, ParsedDocument
from app.documents.normalization import normalize_text


class EvidenceValidationError(ValueError):
    pass


NUMERIC_VALUE_TYPES = {
    "MONEY",
    "PERCENTAGE",
    "RATE",
    "DURATION",
    "NUMBER",
    "QUANTITY",
}


def target_fact_catalog(extraction: DocumentFactExtraction) -> list[dict[str, Any]]:
    return [
        {
            "target_fact_id": f"target_fact_{index:06d}",
            **fact.model_dump(mode="json"),
        }
        for index, fact in enumerate(extraction.facts, start=1)
    ]


def chunk_document(document: ParsedDocument, max_chars: int) -> list[list[DocumentBlock]]:
    chunks: list[list[DocumentBlock]] = []
    current: list[DocumentBlock] = []
    current_chars = 0
    for block in document.blocks:
        block_chars = len(block.raw_text)
        if current and current_chars + block_chars > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(block)
        current_chars += block_chars
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
        raise EvidenceValidationError("profile file_id does not match parsed document")
    evidence = _evidence_at(document)
    for location in extraction.profile.evidence_locations:
        if location_key(location) not in evidence:
            raise EvidenceValidationError("profile evidence location does not exist")
    for fact in extraction.facts:
        if fact.source_file_id != document.file_id:
            raise EvidenceValidationError("fact source_file_id does not match parsed document")
        candidates = evidence.get(location_key(fact.location), [])
        normalized_fact_evidence = normalize_text(fact.evidence_text)
        if not candidates or not any(
            normalized_fact_evidence in normalize_text(candidate) for candidate in candidates
        ):
            raise EvidenceValidationError("fact evidence is not present at the declared location")
    for concept in extraction.semantic_concepts:
        for location in concept.evidence_locations:
            if location_key(location) not in evidence:
                raise EvidenceValidationError("semantic concept evidence location does not exist")
    for spec in extraction.validation_specs:
        for location in spec.evidence_locations:
            if location_key(location) not in evidence:
                raise EvidenceValidationError("validation evidence location does not exist")


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
        if item["status"] == "CONFLICT":
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
        if item["status"] == "UNCERTAIN" or uncertain_relations:
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
        if required_missing_relations:
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
