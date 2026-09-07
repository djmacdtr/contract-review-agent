"""Bounded, safe recovery primitives shared by workflow stages.

The recovery budget deliberately measures only time after the first recovery
is requested.  Normal document processing is therefore not penalized, while
all bounded retries in a task still share one monotonic deadline.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

LLM_TRANSPORT_ERROR_CODES = frozenset(
    {
        "LLM_NETWORK_ERROR",
        "LLM_TIMEOUT",
        "LLM_RATE_LIMITED",
        "LLM_UPSTREAM_ERROR",
    }
)
LLM_MODEL_OUTPUT_ERROR_CODES = frozenset(
    {
        "LLM_INVALID_JSON",
        "LLM_RESPONSE_INVALID",
        "LLM_SCHEMA_INVALID",
        "LLM_OUTPUT_TRUNCATED",
        "LLM_REVIEW_INCOMPLETE",
    }
)
PAGE_REFRESH_ERROR_CODES = frozenset(
    {
        "SIDECAR_MISSING",
        "PUBLIC_LOCATION_UNMAPPED",
        "PUBLIC_DIFF_PAGE_MISSING",
        "PUBLIC_EVIDENCE_PAGE_MISSING",
        "PUBLIC_EVIDENCE_PAGE_OUT_OF_RANGE",
    }
)


def is_retryable_model_output_error(exc: BaseException) -> bool:
    """Return whether a model response may receive one complete retry."""

    return getattr(exc, "code", None) in LLM_MODEL_OUTPUT_ERROR_CODES or (
        isinstance(exc, ValueError)
        and type(exc).__name__ == "ValidationError"
    )


def is_page_refreshable_error(exc: BaseException) -> bool:
    details = getattr(exc, "details", None)
    return isinstance(details, dict) and details.get("failure_code") in PAGE_REFRESH_ERROR_CODES


@dataclass
class RecoveryStats:
    """Safe counters only; never store request bodies or model responses."""

    recovery_attempts: int = 0
    http_request_attempts: int = 0
    page_sidecar_rebuild_attempts: int = 0
    ocr_refresh_attempts: int = 0
    checkpoint_reused: bool = False
    page_sidecar_rebuilt: bool = False
    ocr_cache_refreshed: bool = False
    first_failure_code: str | None = None
    last_failure_code: str | None = None
    recovery_stage_counts: dict[str, int] = field(default_factory=dict)
    logical_call_counts: dict[str, int] = field(default_factory=dict)
    max_split_depth: int = 0

    def failure(self, code: Any) -> None:
        safe_code = str(code)[:64] if code else "UNKNOWN"
        if self.first_failure_code is None:
            self.first_failure_code = safe_code
        self.last_failure_code = safe_code

    def as_dict(self, budget: RecoveryBudget) -> dict[str, Any]:
        return {
            "recovery_attempts": self.recovery_attempts,
            "http_request_attempts": self.http_request_attempts,
            "recovery_elapsed_ms": budget.elapsed_ms,
            "recovery_budget_exhausted": budget.exhausted,
            "checkpoint_reused": self.checkpoint_reused,
            "first_failure_code": self.first_failure_code,
            "last_failure_code": self.last_failure_code,
            "page_sidecar_rebuilt": self.page_sidecar_rebuilt,
            "ocr_cache_refreshed": self.ocr_cache_refreshed,
            "recovery_stage_counts": dict(self.recovery_stage_counts),
            "logical_call_counts": dict(self.logical_call_counts),
            "max_split_depth": self.max_split_depth,
        }


class RecoveryBudget:
    """A monotonic, task-scoped budget for additional recovery work."""

    def __init__(
        self,
        max_extra_seconds: float = 600.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_extra_seconds = max(0.0, float(max_extra_seconds))
        self._clock = clock
        self._started_at: float | None = None
        self.exhausted = False
        self.stats = RecoveryStats()

    @property
    def started(self) -> bool:
        return self._started_at is not None

    @property
    def elapsed_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return max(0.0, self._clock() - self._started_at)

    @property
    def elapsed_ms(self) -> int:
        return round(self.elapsed_seconds * 1000)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.max_extra_seconds - self.elapsed_seconds)

    def record_failure(self, code: Any) -> None:
        self.stats.failure(code)

    def record_http_request(self) -> None:
        self.stats.http_request_attempts += 1

    def record_logical_call(self, stage: str) -> None:
        safe_stage = str(stage)[:64]
        self.stats.logical_call_counts[safe_stage] = (
            self.stats.logical_call_counts.get(safe_stage, 0) + 1
        )

    def record_split_depth(self, depth: int) -> None:
        self.stats.max_split_depth = max(self.stats.max_split_depth, int(depth))

    def allow_stage(self, stage: str, code: Any) -> bool:
        """Reserve one additional stage operation under the shared deadline."""

        if not self.allow(code):
            return False
        safe_stage = str(stage)[:64]
        self.stats.recovery_stage_counts[safe_stage] = (
            self.stats.recovery_stage_counts.get(safe_stage, 0) + 1
        )
        return True

    def allow_page_sidecar_rebuild(self, code: Any) -> bool:
        if self.stats.page_sidecar_rebuild_attempts >= 1:
            return False
        if not self.allow(code):
            return False
        self.stats.page_sidecar_rebuild_attempts += 1
        return True

    def allow_ocr_refresh(self, code: Any) -> bool:
        if self.stats.ocr_refresh_attempts >= 1:
            return False
        if not self.allow(code):
            return False
        self.stats.ocr_refresh_attempts += 1
        return True

    def allow(self, code: Any) -> bool:
        """Start recovery and reserve one bounded recovery operation."""

        self.record_failure(code)
        if self._started_at is None:
            self._started_at = self._clock()
        if self.remaining_seconds <= 0:
            self.exhausted = True
            return False
        self.stats.recovery_attempts += 1
        return True

    async def sleep(
        self,
        delay_seconds: float,
        sleeper: Callable[[float], Awaitable[None]],
    ) -> bool:
        """Sleep without crossing the recovery deadline."""

        remaining = self.remaining_seconds
        if remaining <= 0:
            self.exhausted = True
            return False
        await sleeper(min(max(0.0, delay_seconds), remaining))
        if self.remaining_seconds <= 0:
            self.exhausted = True
            return False
        return True

    def safe_timeout(self, default_seconds: float) -> float:
        """Cap an external operation after recovery has started."""

        if not self.started:
            return max(0.001, float(default_seconds))
        return max(0.001, min(float(default_seconds), self.remaining_seconds))

    def as_dict(self) -> dict[str, Any]:
        return self.stats.as_dict(self)
