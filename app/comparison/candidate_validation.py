"""Bounded LLM validation for opt-in FINAL_COMPARE candidates."""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.adapters.llm.schemas import FinalCompareCandidateValidationResponse
from app.comparison.models import ComparisonResult, DiffItem, DiffSide


def _chunks(values: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _location_signature(side: DiffSide | None) -> tuple[Any, ...]:
    if side is None:
        return ()
    locations = side.locations or [side.location]
    return (
        side.file_id,
        side.text,
        tuple(
            (
                location.page,
                location.paragraph_index,
                location.table_index,
                location.row,
                location.column,
                location.source,
            )
            for location in locations
        ),
    )


def _normalized_side_text(side: DiffSide | None) -> str | None:
    if side is None:
        return None
    from app.comparison.reliable import comparison_normalize

    return comparison_normalize(side.text)[1]


def _diff_equivalent(left: DiffItem, right: DiffItem) -> bool:
    # A model may only delete a candidate when the program-owned text on both
    # sides is identical and both candidates refer to the same logical table
    # area.  Physical coordinates alone are insufficient for merged cells.
    if _normalized_side_text(left.baseline) != _normalized_side_text(right.baseline):
        return False
    if _normalized_side_text(left.target) != _normalized_side_text(right.target):
        return False
    if left.logical_area_key and right.logical_area_key:
        return left.logical_area_key == right.logical_area_key
    return _location_signature(left.baseline) == _location_signature(
        right.baseline
    ) and _location_signature(left.target) == _location_signature(right.target)


def _safe_failure_code(error: BaseException) -> str:
    code = getattr(error, "failure_code", None) or getattr(error, "code", None)
    return str(code)[:80] if code else type(error).__name__[:80]


async def validate_final_compare_candidates(
    comparison: ComparisonResult,
    llm: Any,
    *,
    batch_size: int = 8,
    recovery_batch_size: int = 4,
) -> ComparisonResult:
    """Validate ambiguous candidates without allowing the model to delete evidence.

    A failed or incomplete response leaves its candidates in the result and
    marks them ``REVIEW_REQUIRED``. A duplicate is removed only when the
    referenced candidate has the same program-owned text and logical area.
    """

    records = [
        record
        for record in comparison.candidate_records
        if isinstance(record, dict) and record.get("candidate_id")
    ]
    stats = Counter(comparison.validation_stats)
    stats.setdefault("llm_reviewed_count", 0)
    stats.setdefault("llm_duplicate_removed_count", 0)
    stats.setdefault("candidate_validation_failures", 0)
    metadata: dict[str, Any] = {
        "purpose": "FINAL_COMPARE_CANDIDATE_VALIDATION",
        "logical_call_count": 0,
        "configured_model": None,
        "actual_model": None,
        "finish_reasons": {},
        "response_formats": {},
    }
    if not records or not hasattr(llm, "validate_final_compare_candidates"):
        for diff in comparison.diff_items:
            if diff.candidate_id:
                stats["candidate_validation_failures"] += 1
                diff.validation_status = "REVIEW_REQUIRED"
                diff.validation_reason_code = "LLM_VALIDATION_UNAVAILABLE"
        comparison.validation_stats = dict(stats)
        comparison.validation_metadata = metadata
        return comparison

    record_by_id = {str(record["candidate_id"]): record for record in records}
    diff_by_candidate = {
        diff.candidate_id: diff for diff in comparison.diff_items if diff.candidate_id
    }
    removed_ids: set[str] = set()
    missing_ids: list[str] = []

    async def execute(batch: list[dict[str, Any]]) -> None:
        nonlocal missing_ids
        batch_ids = {str(item["candidate_id"]) for item in batch}
        safe_payload = {
            "candidates": batch,
            "requirements": {
                "decisions_for_each_candidate": True,
                "allowed_decisions": ["KEEP_CHANGE", "DUPLICATE_OF", "UNCERTAIN"],
                "evidence_is_program_owned": True,
                "batch_size": len(batch),
            },
        }
        try:
            response = await llm.validate_final_compare_candidates(safe_payload)
            metadata["logical_call_count"] += 1
            metadata["configured_model"] = metadata["configured_model"] or getattr(
                response, "configured_model", None
            )
            metadata["actual_model"] = metadata["actual_model"] or getattr(
                response, "actual_model", None
            )
            finish_reason = str(getattr(response, "finish_reason", None) or "unknown")
            finish_reasons = metadata["finish_reasons"]
            finish_reasons[finish_reason] = finish_reasons.get(finish_reason, 0) + 1
            response_format = str(getattr(response, "response_format", None) or "unknown")
            response_formats = metadata["response_formats"]
            response_formats[response_format] = response_formats.get(response_format, 0) + 1
            raw = response.value if hasattr(response, "value") else response
            validated = FinalCompareCandidateValidationResponse.model_validate(raw)
            decisions = validated.model_dump(mode="json")["decisions"]
            response_by_id: dict[str, dict[str, Any]] = {}
            duplicate_response_count = 0
            for decision in decisions:
                if not isinstance(decision, dict):
                    continue
                candidate_id = str(decision.get("candidate_id", ""))
                if candidate_id not in batch_ids:
                    stats["candidate_validation_failures"] += 1
                    continue
                if candidate_id in response_by_id:
                    duplicate_response_count += 1
                    continue
                response_by_id[candidate_id] = decision
            if duplicate_response_count:
                stats["candidate_validation_failures"] += duplicate_response_count
            missing = sorted(batch_ids - response_by_id.keys())
            missing_ids.extend(missing)
            stats["llm_reviewed_count"] += len(response_by_id)
            for candidate_id in batch_ids:
                diff = diff_by_candidate.get(candidate_id)
                if diff is None:
                    stats["candidate_validation_failures"] += 1
                    continue
                decision = response_by_id.get(candidate_id)
                if decision is None:
                    diff.validation_status = "REVIEW_REQUIRED"
                    diff.validation_reason_code = "LLM_VALIDATION_INCOMPLETE"
                    continue
                action = decision.get("decision")
                if action == "KEEP_CHANGE":
                    diff.validation_status = "CONFIRMED"
                    diff.validation_source = "RULE_AND_LLM"
                    diff.validation_reason_code = None
                    continue
                if action == "DUPLICATE_OF":
                    duplicate_of = str(decision.get("duplicate_of", ""))
                    duplicate_diff = diff_by_candidate.get(duplicate_of)
                    try:
                        confidence = float(decision.get("confidence", 0))
                    except (TypeError, ValueError):
                        confidence = 0.0
                    if (
                        duplicate_diff is not None
                        and duplicate_of != candidate_id
                        and confidence >= 0.95
                        and _diff_equivalent(diff, duplicate_diff)
                    ):
                        removed_ids.add(candidate_id)
                        stats["llm_duplicate_removed_count"] += 1
                        continue
                    diff.validation_status = "REVIEW_REQUIRED"
                    diff.validation_reason_code = "LLM_DUPLICATE_UNSAFE"
                    stats["candidate_validation_failures"] += 1
                    continue
                stats["candidate_validation_failures"] += 1
                diff.validation_status = "REVIEW_REQUIRED"
                diff.validation_reason_code = "LLM_UNCERTAIN"
        except Exception as error:  # noqa: BLE001 - candidate validation is non-fatal
            stats["candidate_validation_failures"] += 1
            stats[f"failure_{_safe_failure_code(error)}"] += 1
            for candidate_id in batch_ids:
                diff = diff_by_candidate.get(candidate_id)
                if diff is not None:
                    diff.validation_status = "REVIEW_REQUIRED"
                    diff.validation_reason_code = "LLM_VALIDATION_FAILED"

    for batch in _chunks(records, batch_size):
        await execute(batch)
    if missing_ids:
        missing_records = [record_by_id[item] for item in dict.fromkeys(missing_ids)]
        for batch in _chunks(missing_records, recovery_batch_size):
            await execute(batch)

    if removed_ids:
        comparison.diff_items = [
            diff for diff in comparison.diff_items if diff.candidate_id not in removed_ids
        ]
    stats["review_required_count"] = sum(
        diff.validation_status == "REVIEW_REQUIRED" for diff in comparison.diff_items
    )
    stats["final_diff_count"] = len(comparison.diff_items)
    comparison.diagnostics = comparison.diagnostics.model_copy(
        update={"emitted_diff_count": len(comparison.diff_items)}
    )
    comparison.validation_stats = dict(stats)
    metadata["status"] = "SUCCEEDED" if not stats["candidate_validation_failures"] else "PARTIAL"
    comparison.validation_metadata = metadata
    return comparison
