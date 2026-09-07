"""Bounded, item-scoped Advice generation for result workflows."""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.adapters.llm.schemas import AdviceResponse, SingleRiskAdviceResponse
from app.core.reliability import RecoveryBudget
from app.results.advice import (
    ADVICE_QUALITY_CODES,
    advice_payload,
    ensure_fallback_risk_advices,
    validate_advice_item,
)

ADVICE_BATCH_SIZE = 8
ADVICE_RECOVERY_BATCH_SIZE = 4
ADVICE_MAX_LOGICAL_CALLS = 48
ADVICE_MAX_CONCURRENCY = 2
ADVICE_MAX_ITEM_REPAIRS = 16
ADVICE_DIAGNOSTIC_CODES = (
    "MULTI_SENTENCE",
    "DUPLICATED",
    "INTERNAL_ID",
    "TECHNICAL_TERM",
    "NOT_SPECIFIC",
    "RISK_ID_INVALID",
)


@dataclass
class AdviceBatchStats:
    """Safe Advice generation metrics; no model or document content."""

    risk_count: int
    initial_batch_count: int
    recovery_batch_count: int
    logical_call_count: int
    returned_count: int
    accepted_count: int
    normalized_count: int
    fallback_count: int
    quality_rejections: dict[str, int]
    finish_reasons: dict[str, int]
    failure_codes: dict[str, int]
    advice_batch_failure_counts: dict[str, int] = field(default_factory=dict)
    advice_item_repair_attempted: int = 0
    advice_item_repair_succeeded: int = 0
    advice_item_repair_failed: int = 0
    fallback_risk_ids: list[str] = field(default_factory=list)
    fallback_failure_codes: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        model_rate = self.accepted_count / self.risk_count if self.risk_count else 1.0
        return {
            "risk_count": self.risk_count,
            "initial_batch_count": self.initial_batch_count,
            "recovery_batch_count": self.recovery_batch_count,
            "batch_count": self.initial_batch_count + self.recovery_batch_count,
            "logical_call_count": self.logical_call_count,
            "returned_count": self.returned_count,
            "accepted_count": self.accepted_count,
            "normalized_count": self.normalized_count,
            "model_count": self.accepted_count,
            "fallback_count": self.fallback_count,
            "model_rate": round(model_rate, 4),
            "fallback_rate": round(1.0 - model_rate, 4),
            "quality_rejections": dict(self.quality_rejections),
            "finish_reasons": dict(self.finish_reasons),
            "failure_codes": dict(self.failure_codes),
            "advice_batch_failure_counts": dict(self.advice_batch_failure_counts),
            "advice_item_repair_attempted": self.advice_item_repair_attempted,
            "advice_item_repair_succeeded": self.advice_item_repair_succeeded,
            "advice_item_repair_failed": self.advice_item_repair_failed,
            "fallback_risk_ids": list(self.fallback_risk_ids),
            "fallback_failure_codes": dict(self.fallback_failure_codes),
        }


def _chunks(values: list[str], size: int) -> list[list[str]]:
    safe_size = max(1, int(size))
    return [values[index : index + safe_size] for index in range(0, len(values), safe_size)]


def _safe_failure_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code[:64]
    if isinstance(exc, ValidationError):
        return "LLM_SCHEMA_INVALID"
    return type(exc).__name__[:64]


def _safe_metric(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _advice_batch_id(risk_ids: list[str]) -> str:
    digest = hashlib.sha256("\0".join(sorted(risk_ids)).encode("utf-8")).hexdigest()
    return f"advice_batch_{digest[:16]}"


def _model_run_record(
    *,
    purpose: str,
    batch_id: str,
    risk_count: int,
    status: str,
    generated: Any = None,
    error: BaseException | None = None,
    item_repair_attempted: bool = False,
) -> dict[str, Any]:
    source = generated if generated is not None else error
    response_metadata = getattr(source, "response_metadata", None)
    if not isinstance(response_metadata, dict):
        response_metadata = {}

    def metric(name: str) -> int:
        value = getattr(source, name, None)
        if value is None:
            value = response_metadata.get(name)
        return _safe_metric(value)

    return {
        "purpose": purpose,
        "batch_id": batch_id,
        "risk_count": risk_count,
        "status": status,
        "configured_model": getattr(source, "configured_model", None),
        "actual_model": getattr(source, "actual_model", None),
        "failure_code": _safe_failure_code(error) if error is not None else None,
        "finish_reason": getattr(source, "finish_reason", None),
        "request_attempts": _safe_metric(getattr(source, "request_attempts", 0)),
        "structure_retries": _safe_metric(getattr(source, "structure_retries", 0)),
        "content_chars": metric("content_chars"),
        "reasoning_content_chars": metric("reasoning_content_chars"),
        "item_repair_attempted": item_repair_attempted,
    }


def _default_advice(result: dict[str, Any]) -> dict[str, Any]:
    existing = result.get("advice") if isinstance(result.get("advice"), dict) else {}
    return {
        "overall_advice": existing.get("overall_advice")
        or "请按来源位置处理确认风险，并单独复核不确定事项。",
        "priority_actions": existing.get("priority_actions") or [],
        "manual_review_focus": existing.get("manual_review_focus") or [],
        "limitations": existing.get("limitations") or [],
        "evidence_refs": [],
        "risk_advices": [],
    }


def _single_risk_payload(result: dict[str, Any], risk_id: str) -> dict[str, Any]:
    payload = advice_payload(result, risk_ids={risk_id})
    selected = payload.get("risk_items", [])
    if len(selected) != 1 or str(selected[0].get("risk_id")) != risk_id:
        raise ValueError("single-risk Advice payload selection is not exact")
    return {
        "risk": selected[0],
        "diff_items": payload.get("diff_items", []),
        "related_facts": payload.get("related_facts", []),
        "files": payload.get("files", []),
    }


async def generate_advice_in_batches(
    result: dict[str, Any],
    llm: Any,
    *,
    batch_size: int = ADVICE_BATCH_SIZE,
    recovery_batch_size: int = ADVICE_RECOVERY_BATCH_SIZE,
    max_logical_calls: int = ADVICE_MAX_LOGICAL_CALLS,
    max_concurrency: int = ADVICE_MAX_CONCURRENCY,
    require_dynamic_anchor: bool = True,
    recovery_budget: RecoveryBudget | None = None,
) -> AdviceBatchStats:
    """Generate Advice in bounded batches, then repair unresolved risks individually."""

    risks = [item for item in result.get("risk_items", []) if isinstance(item, dict)]
    risk_ids = [str(item.get("risk_id")) for item in risks if item.get("risk_id")]
    risk_by_id = {str(item["risk_id"]): item for item in risks}
    for risk in risks:
        risk["analysis_advice"] = None

    quality_rejections = Counter({code: 0 for code in ADVICE_DIAGNOSTIC_CODES})
    finish_reasons: Counter[str] = Counter()
    failure_codes: Counter[str] = Counter()
    batch_failure_counts: Counter[str] = Counter()
    accepted_ids: set[str] = set()
    accepted_advice_texts: set[str] = set()
    accepted_items: dict[str, dict[str, Any]] = {}
    last_failure_code: dict[str, str] = {}
    model_runs: list[dict[str, Any]] = []
    returned_count = 0
    normalized_count = 0
    logical_call_count = 0
    recovery_call_count = 0
    item_repair_attempted = 0
    item_repair_succeeded = 0
    item_repair_failed = 0
    general_advice: dict[str, Any] | None = None
    semaphore = asyncio.Semaphore(max(1, min(ADVICE_MAX_CONCURRENCY, max_concurrency)))

    def record_failure(code: str, ids: list[str]) -> None:
        failure_codes[code] += 1
        batch_failure_counts[code] += 1
        for risk_id in ids:
            last_failure_code[risk_id] = code

    def accept_item(item: Any, allowed_ids: set[str]) -> str | None:
        nonlocal normalized_count
        risk_id = str(getattr(item, "risk_id", ""))
        if risk_id not in allowed_ids or risk_id not in risk_by_id:
            quality_rejections["RISK_ID_INVALID"] += 1
            record_failure("RISK_ID_INVALID", [risk_id] if risk_id in risk_by_id else [])
            return None
        outcome = validate_advice_item(
            result,
            item,
            seen_risk_ids=accepted_ids,
            seen_advice_texts=accepted_advice_texts,
            require_dynamic_anchor=require_dynamic_anchor,
        )
        if not outcome.accepted:
            reason = outcome.reason_code or "RISK_ID_INVALID"
            quality_rejections[reason] += 1
            record_failure(reason, [risk_id])
            return None
        if outcome.normalized_multi_sentence:
            quality_rejections["MULTI_SENTENCE"] += 1
        accepted_ids.add(risk_id)
        accepted_advice_texts.add(outcome.normalized_advice)
        accepted_items[risk_id] = {
            "risk_id": risk_id,
            "analysis_advice": outcome.normalized_advice,
        }
        normalized_count += int(outcome.normalized_multi_sentence)
        return risk_id

    async def execute_batch(batch: list[str]) -> tuple[int, set[str]]:
        nonlocal returned_count, logical_call_count, general_advice
        if not batch or logical_call_count >= max_logical_calls:
            return 0, set()
        logical_call_count += 1
        batch_id = _advice_batch_id(batch)
        try:
            generated = await llm.generate_advice(advice_payload(result, risk_ids=set(batch)))
            finish_reason = getattr(generated, "finish_reason", None) or "unknown"
            finish_reasons[str(finish_reason)] += 1
            response = AdviceResponse.model_validate(generated.value)
            if general_advice is None:
                general_advice = response.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 - safe per-batch degradation
            code = _safe_failure_code(exc)
            record_failure(code, batch)
            model_runs.append(
                _model_run_record(
                    purpose="RISK_ADVICE",
                    batch_id=batch_id,
                    risk_count=len(batch),
                    status="FAILED",
                    error=exc,
                )
            )
            return 0, set()

        model_runs.append(
            _model_run_record(
                purpose="RISK_ADVICE",
                batch_id=batch_id,
                risk_count=len(batch),
                status="SUCCEEDED",
                generated=generated,
            )
        )
        returned_count += len(response.risk_advices)
        allowed_ids = set(batch)
        returned_ids: set[str] = set()
        for item in response.risk_advices:
            risk_id = accept_item(item, allowed_ids)
            if risk_id is not None:
                returned_ids.add(risk_id)
        for risk_id in allowed_ids - returned_ids - accepted_ids:
            record_failure("ADVICE_ITEM_MISSING", [risk_id])
        return len(response.risk_advices), returned_ids

    initial_batches = _chunks(risk_ids, batch_size)

    async def bounded_batch(batch: list[str]) -> tuple[int, set[str]]:
        async with semaphore:
            return await execute_batch(batch)

    if initial_batches:
        await asyncio.gather(*(bounded_batch(batch) for batch in initial_batches))

    missing_ids = [risk_id for risk_id in risk_ids if risk_id not in accepted_ids]
    for batch in _chunks(missing_ids, recovery_batch_size):
        if logical_call_count >= max_logical_calls:
            break
        if recovery_budget is not None and not recovery_budget.allow_stage(
            "ADVICE_BATCH_RECOVERY", "LLM_ADVICE_RECOVERY"
        ):
            break
        await execute_batch(batch)
        recovery_call_count += 1

    missing_ids = [risk_id for risk_id in risk_ids if risk_id not in accepted_ids]
    item_repair_ids = missing_ids[:ADVICE_MAX_ITEM_REPAIRS]
    for risk_id in missing_ids[ADVICE_MAX_ITEM_REPAIRS:]:
        last_failure_code.setdefault(risk_id, "ADVICE_ITEM_REPAIR_LIMIT")

    item_llm = getattr(llm, "generate_advice_item", None)

    async def repair_one(risk_id: str) -> None:
        nonlocal logical_call_count, item_repair_attempted, item_repair_succeeded
        nonlocal item_repair_failed
        if logical_call_count >= max_logical_calls:
            last_failure_code[risk_id] = "LLM_ADVICE_LOGICAL_CALL_LIMIT"
            item_repair_failed += 1
            return
        if not callable(item_llm):
            last_failure_code[risk_id] = "LLM_ADVICE_ITEM_REPAIR_UNAVAILABLE"
            item_repair_failed += 1
            return
        if recovery_budget is not None and not recovery_budget.allow_stage(
            "ADVICE_ITEM_REPAIR", last_failure_code.get(risk_id, "ADVICE_ITEM_MISSING")
        ):
            last_failure_code[risk_id] = "RECOVERY_BUDGET_EXHAUSTED"
            item_repair_failed += 1
            return
        logical_call_count += 1
        item_repair_attempted += 1
        batch_id = _advice_batch_id([risk_id])
        try:
            generated = await item_llm(_single_risk_payload(result, risk_id))
            finish_reason = getattr(generated, "finish_reason", None) or "unknown"
            finish_reasons[str(finish_reason)] += 1
            response = SingleRiskAdviceResponse.model_validate(generated.value)
            if response.risk_id != risk_id:
                raise ValueError("single-risk Advice response risk_id mismatch")
            accepted = accept_item(response, {risk_id})
            if accepted is None:
                raise ValueError("single-risk Advice response rejected by quality gate")
            item_repair_succeeded += 1
            model_runs.append(
                _model_run_record(
                    purpose="RISK_ADVICE",
                    batch_id=batch_id,
                    risk_count=1,
                    status="RECOVERED",
                    generated=generated,
                    item_repair_attempted=True,
                )
            )
        except Exception as exc:  # noqa: BLE001 - fallback is deterministic and bounded
            code = _safe_failure_code(exc)
            if code == "ValueError" and "risk_id mismatch" in str(exc):
                code = "RISK_ID_INVALID"
                quality_rejections[code] += 1
            elif code == "ValueError" and "quality gate" in str(exc):
                code = last_failure_code.get(risk_id, "ADVICE_ITEM_REPAIR_REJECTED")
            record_failure(code, [risk_id])
            item_repair_failed += 1
            model_runs.append(
                _model_run_record(
                    purpose="RISK_ADVICE",
                    batch_id=batch_id,
                    risk_count=1,
                    status="FAILED",
                    error=exc,
                    item_repair_attempted=True,
                )
            )

    for wave in _chunks(item_repair_ids, ADVICE_MAX_CONCURRENCY):
        if logical_call_count >= max_logical_calls:
            for risk_id in wave:
                last_failure_code[risk_id] = "LLM_ADVICE_LOGICAL_CALL_LIMIT"
                item_repair_failed += 1
            break
        await asyncio.gather(*(repair_one(risk_id) for risk_id in wave))

    for risk_id, item in accepted_items.items():
        risk_by_id[risk_id]["analysis_advice"] = item["analysis_advice"]
    ensure_fallback_risk_advices(result)

    fallback_ids = [risk_id for risk_id in risk_ids if risk_id not in accepted_ids]
    fallback_failure_codes = {
        risk_id: last_failure_code.get(risk_id, "ADVICE_UNRESOLVED")
        for risk_id in fallback_ids
    }
    advice = _default_advice(result)
    if general_advice is not None:
        for key in ("overall_advice", "priority_actions", "manual_review_focus", "limitations"):
            if general_advice.get(key):
                advice[key] = general_advice[key]
    advice["risk_advices"] = list(accepted_items.values())
    result["advice"] = advice

    if fallback_ids:
        warnings = result.setdefault("warnings", [])
        if not any(
            isinstance(item, dict) and item.get("code") == "LLM_ADVICE_UNAVAILABLE"
            for item in warnings
        ):
            warnings.append(
                {
                    "code": "LLM_ADVICE_UNAVAILABLE",
                    "message": "部分模型建议未完成，已按单项回退确定性分析建议。",
                    "requires_manual_review": False,
                }
            )

    stats = AdviceBatchStats(
        risk_count=len(risks),
        initial_batch_count=len(initial_batches),
        recovery_batch_count=recovery_call_count,
        logical_call_count=logical_call_count,
        returned_count=returned_count,
        accepted_count=len(accepted_items),
        normalized_count=normalized_count,
        fallback_count=len(fallback_ids),
        quality_rejections=dict(quality_rejections),
        finish_reasons=dict(finish_reasons),
        failure_codes=dict(failure_codes),
        advice_batch_failure_counts=dict(batch_failure_counts),
        advice_item_repair_attempted=item_repair_attempted,
        advice_item_repair_succeeded=item_repair_succeeded,
        advice_item_repair_failed=item_repair_failed,
        fallback_risk_ids=fallback_ids,
        fallback_failure_codes=fallback_failure_codes,
    )
    metadata = result.setdefault("metadata", {})
    metadata["advice_coverage"] = stats.as_dict()
    metadata["advice_validation"] = {
        "accepted_count": len(accepted_items),
        **{
            code: int(quality_rejections.get(code, 0))
            for code in ADVICE_QUALITY_CODES
        },
        "not_specific_count": int(quality_rejections.get("NOT_SPECIFIC", 0)),
        "multi_sentence_normalized_count": normalized_count,
    }
    metadata["advice_batch_failure_counts"] = dict(batch_failure_counts)
    metadata["advice_item_repair_attempted"] = item_repair_attempted
    metadata["advice_item_repair_succeeded"] = item_repair_succeeded
    metadata["advice_item_repair_failed"] = item_repair_failed
    metadata["fallback_risk_ids"] = list(fallback_ids)
    metadata["fallback_failure_codes"] = dict(fallback_failure_codes)
    existing_runs = metadata.setdefault("model_runs", [])
    existing_runs[:] = [
        item
        for item in existing_runs
        if not (isinstance(item, dict) and item.get("purpose") == "RISK_ADVICE")
    ]
    existing_runs.extend(model_runs)
    return stats
