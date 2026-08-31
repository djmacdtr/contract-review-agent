"""Bounded, item-scoped Advice generation for result workflows."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.adapters.llm.schemas import AdviceResponse
from app.results.advice import (
    advice_payload,
    ensure_fallback_risk_advices,
    validate_advice_item,
)

ADVICE_BATCH_SIZE = 8
ADVICE_RECOVERY_BATCH_SIZE = 4
ADVICE_MAX_LOGICAL_CALLS = 48
ADVICE_MAX_CONCURRENCY = 2
ADVICE_DIAGNOSTIC_CODES = (
    "MULTI_SENTENCE",
    "DUPLICATED",
    "INTERNAL_ID",
    "TECHNICAL_TERM",
    "NOT_SPECIFIC",
    "RISK_ID_INVALID",
)


@dataclass(frozen=True)
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
        }


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _safe_failure_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code[:64]
    return type(exc).__name__[:64]


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


async def generate_advice_in_batches(
    result: dict[str, Any],
    llm: Any,
    *,
    batch_size: int = ADVICE_BATCH_SIZE,
    recovery_batch_size: int = ADVICE_RECOVERY_BATCH_SIZE,
    max_logical_calls: int = ADVICE_MAX_LOGICAL_CALLS,
    max_concurrency: int = ADVICE_MAX_CONCURRENCY,
    require_dynamic_anchor: bool = True,
) -> AdviceBatchStats:
    """Generate Advice in bounded batches and accept items independently.

    Initial batches are submitted with at most ``max_concurrency`` concurrent
    requests. Missing or rejected items receive one recovery pass in batches
    of four. A failed batch never invalidates accepted items from another
    batch; unresolved items receive the deterministic fallback.
    """

    risks = [item for item in result.get("risk_items", []) if isinstance(item, dict)]
    risk_ids = [str(item.get("risk_id")) for item in risks if item.get("risk_id")]
    risk_by_id = {str(item["risk_id"]): item for item in risks}
    for risk in risks:
        risk["analysis_advice"] = None

    quality_rejections = Counter({code: 0 for code in ADVICE_DIAGNOSTIC_CODES})
    finish_reasons: Counter[str] = Counter()
    failure_codes: Counter[str] = Counter()
    accepted_ids: set[str] = set()
    accepted_advice_texts: set[str] = set()
    accepted_items: dict[str, dict[str, Any]] = {}
    returned_count = 0
    normalized_count = 0
    logical_call_count = 0
    recovery_call_count = 0
    general_advice: dict[str, Any] | None = None
    configured_model: str | None = None
    actual_model: str | None = None

    async def execute_batch(batch: list[str]) -> tuple[list[str], int]:
        nonlocal returned_count, normalized_count, logical_call_count
        nonlocal general_advice, configured_model, actual_model
        if not batch or logical_call_count >= max_logical_calls:
            return [], 0
        logical_call_count += 1
        try:
            generated = await llm.generate_advice(
                advice_payload(result, risk_ids=set(batch))
            )
            configured_model = configured_model or getattr(generated, "configured_model", None)
            actual_model = actual_model or getattr(generated, "actual_model", None)
            finish_reason = getattr(generated, "finish_reason", None) or "unknown"
            finish_reasons[str(finish_reason)] += 1
            response = AdviceResponse.model_validate(generated.value)
            if general_advice is None:
                general_advice = response.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 - safe per-batch degradation
            failure_codes[_safe_failure_code(exc)] += 1
            return [], 0

        returned_count += len(response.risk_advices)
        local_seen: set[str] = set()
        local_texts: set[str] = set()
        accepted: list[str] = []
        for item in response.risk_advices:
            risk_id = str(item.risk_id)
            if risk_id not in risk_by_id or risk_id not in set(batch):
                quality_rejections["RISK_ID_INVALID"] += 1
                continue
            outcome = validate_advice_item(
                result,
                item,
                seen_risk_ids=local_seen | accepted_ids,
                seen_advice_texts=local_texts | accepted_advice_texts,
                require_dynamic_anchor=require_dynamic_anchor,
            )
            local_seen.add(risk_id)
            if not outcome.accepted:
                quality_rejections[outcome.reason_code or "RISK_ID_INVALID"] += 1
                continue
            accepted_ids.add(risk_id)
            local_texts.add(outcome.normalized_advice)
            accepted_advice_texts.add(outcome.normalized_advice)
            accepted_items[risk_id] = item.model_copy(
                update={"analysis_advice": outcome.normalized_advice}
            ).model_dump(mode="json")
            accepted.append(risk_id)
            normalized_count += int(outcome.normalized_multi_sentence)
        return accepted, len(response.risk_advices)

    initial_batches = _chunks(risk_ids, batch_size)
    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def bounded_batch(batch: list[str]) -> tuple[list[str], int]:
        async with semaphore:
            return await execute_batch(batch)

    if initial_batches:
        await asyncio.gather(*(bounded_batch(batch) for batch in initial_batches))

    missing_ids = [risk_id for risk_id in risk_ids if risk_id not in accepted_ids]
    recovery_batches = _chunks(missing_ids, recovery_batch_size)
    for batch in recovery_batches:
        if logical_call_count >= max_logical_calls:
            break
        accepted, _returned = await execute_batch(batch)
        recovery_call_count += int(bool(batch))
        del accepted

    for risk_id, item in accepted_items.items():
        risk_by_id[risk_id]["analysis_advice"] = item["analysis_advice"]
    ensure_fallback_risk_advices(result)

    fallback_count = sum(
        1 for risk in risks if not risk_id_is_model_owned(risk, accepted_items)
    )
    advice = _default_advice(result)
    if general_advice is not None:
        for key in ("overall_advice", "priority_actions", "manual_review_focus", "limitations"):
            if general_advice.get(key):
                advice[key] = general_advice[key]
    advice["risk_advices"] = list(accepted_items.values())
    result["advice"] = advice

    if fallback_count:
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
        fallback_count=fallback_count,
        quality_rejections=dict(quality_rejections),
        finish_reasons=dict(finish_reasons),
        failure_codes=dict(failure_codes),
    )
    result.setdefault("metadata", {})["advice_coverage"] = stats.as_dict()
    if logical_call_count:
        metadata = result["metadata"]
        model_runs = metadata.setdefault("model_runs", [])
        model_runs[:] = [
            item
            for item in model_runs
            if not (isinstance(item, dict) and item.get("purpose") == "RISK_ADVICE")
        ]
        model_runs.append(
            {
                "purpose": "RISK_ADVICE",
                "configured_model": configured_model,
                "actual_model": actual_model,
                "status": "SUCCEEDED" if accepted_items else "PARTIAL",
                "batch_count": len(initial_batches) + recovery_call_count,
                "logical_call_count": logical_call_count,
                "request_attempts": logical_call_count,
                "structure_retries": 0,
                "finish_reasons": dict(finish_reasons),
            }
        )
    return stats


def risk_id_is_model_owned(risk: dict[str, Any], accepted_items: dict[str, dict[str, Any]]) -> bool:
    risk_id = str(risk.get("risk_id"))
    return risk_id in accepted_items and bool(risk.get("analysis_advice"))
