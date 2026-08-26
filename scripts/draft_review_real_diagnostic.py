"""Run one safe real three-file DRAFT_REVIEW diagnostic.

This runner is intentionally separate from the application workflow. It keeps
the real result in memory, writes only structural metrics, and refuses to run
again after its one-shot marker has been created.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import hashlib
import json
import socket
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.adapters.llm.base import LlmResult
from app.adapters.llm.openai_client import LlmClientError, OpenAIContractLlmClient
from app.core.config import Settings
from app.core.enums import TaskStage, TaskType
from app.core.errors import WorkflowError
from app.documents.parsers import DocxParser
from app.draft_review.checkpoints import (
    ExtractionCheckpoint,
    SqlAlchemyExtractionCheckpointStore,
)
from app.draft_review.facts import (
    TEXT_EXTRACTION_VERSION,
    EvidenceValidationError,
    build_template_text_candidates,
    build_text_fact_payload,
    expand_text_fact_response,
    extraction_units,
    numeric_candidates,
    plan_text_candidate_batches,
    plan_text_document_batches,
    stable_batch_id,
    stable_unit_id,
)
from app.draft_review.template_checks import analyze_template
from app.schemas.results import TaskResultData
from app.services.downloader import DOCX_MIME, LocalFile
from app.services.temp_files import TaskWorkspace
from app.workflows.draft_review import DraftReviewWorkflowExecutor
from scripts.llm_structured_output_probe import run_production_probe

SAMPLE_DIR = Path(r"D:\work\contract_review\脱敏真实合同")
REAL_FILES = (
    ("融资租赁合同（回租）.docx", "TARGET"),
    ("融资租赁合同（回租）模版.docx", "TEMPLATE"),
    ("项目方案确认函.docx", "REFERENCE"),
)
DEFAULT_OUTPUT = Path(".real-diagnostic-temp") / "draft-review-real-safe.jsonl"
DEFAULT_LOCK = Path(".real-diagnostic-temp") / "draft-review-real.lock"
MAX_REAL_LOGICAL_CALLS = 3
MAX_REAL_RUNTIME_SECONDS = 10 * 60


def safe_error_code(exc: BaseException) -> str:
    if isinstance(exc, LlmClientError):
        return exc.code
    if isinstance(exc, WorkflowError):
        return exc.code
    if isinstance(exc, EvidenceValidationError):
        return exc.code
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    if isinstance(exc, socket.gaierror):
        # The diagnostic must expose a stable infrastructure category rather
        # than a DNS/database implementation exception or hidden host detail.
        return "CHECKPOINT_UNAVAILABLE"
    return type(exc).__name__


def safe_failure_code(exc: BaseException) -> str:
    """Return the most specific internal code without exposing model data."""

    if isinstance(exc, LlmClientError):
        return exc.failure_code or exc.code
    if isinstance(exc, WorkflowError):
        if isinstance(exc.details, dict) and isinstance(exc.details.get("failure_code"), str):
            return str(exc.details["failure_code"])
        if exc.__cause__ is not None:
            return safe_failure_code(exc.__cause__)
    return safe_error_code(exc)


def host_diagnostic_database_url(database_url: str) -> str:
    """Route the host-run diagnostic to the published PostgreSQL port only."""

    url = make_url(database_url)
    if url.host == "postgres" and url.port == 5432:
        return url.set(host="127.0.0.1", port=15432).render_as_string(
            hide_password=False
        )
    return database_url


class SafeMetricWriter:
    """Append-only JSONL writer that never receives model or document text."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._stream = path.open("x", encoding="utf-8", newline="\n")

    def emit(self, event: str, **fields: Any) -> None:
        payload = {"event": event, **fields}
        self._stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()


@dataclass
class SafeMetrics:
    writer: SafeMetricWriter
    file_names: dict[str, str]
    max_logical_calls: int | None = None
    file_names_by_sha: dict[str, str] = field(default_factory=dict)
    http_calls: int = 0
    logical_calls: int = 0
    response_chars: int = 0
    request_chars: int = 0
    phase_events: list[str] = field(default_factory=list)
    logical_counts: Counter[str] = field(default_factory=Counter)
    document_request_counts: Counter[str] = field(default_factory=Counter)
    operation_request_counts: Counter[str] = field(default_factory=Counter)
    planned_batch_counts: Counter[str] = field(default_factory=Counter)
    completed_batch_counts: Counter[str] = field(default_factory=Counter)
    recovery_counts: Counter[str] = field(default_factory=Counter)
    probe_http_calls: int = 0
    checkpoint_reused: int = 0
    checkpoint_saved: int = 0
    extraction_fact_counts: Counter[str] = field(default_factory=Counter)
    extraction_fact_digests: dict[str, set[str]] = field(default_factory=dict)
    fact_review_counts: dict[str, Counter[str]] = field(default_factory=dict)
    fact_review_decision_digests: dict[str, set[str]] = field(default_factory=dict)
    mapping_target_fact_counts: Counter[str] = field(default_factory=Counter)
    mapping_reference_fact_counts: Counter[str] = field(default_factory=Counter)
    mapping_proposal_counts: Counter[str] = field(default_factory=Counter)
    mapping_missing_requirement_counts: Counter[str] = field(default_factory=Counter)
    mapping_decision_counts: dict[str, Counter[str]] = field(default_factory=dict)
    mapping_review_counts: dict[str, Counter[str]] = field(default_factory=dict)
    mapping_review_decision_digests: dict[str, set[str]] = field(default_factory=dict)
    mapping_gate_counts: dict[str, Counter[str]] = field(default_factory=dict)

    def record_mapping_model_gate(
        self, file_name: str, mapping_model: str | None, review_model: str | None
    ) -> None:
        counts = self.mapping_gate_counts.setdefault(file_name, Counter())
        counts["independent_models"] = int(
            bool(mapping_model and review_model and mapping_model != review_model)
        )
    call_context: contextvars.ContextVar[tuple[str | None, str | None, str | None]] = field(
        default_factory=lambda: contextvars.ContextVar(
            "draft_review_diagnostic_call",
            default=(None, None, None),
        ),
        repr=False,
    )
    current_operation: str | None = None
    current_file_id: str | None = None
    current_batch_id: str | None = None
    first_failure_stage: str | None = None

    def _file_name(self, file_id: str | None) -> str:
        return self.file_names.get(file_id, "任务级")

    def record_extraction_value(self, file_name: str, value: Any) -> None:
        facts = value.get("facts") if isinstance(value, dict) else None
        if not isinstance(facts, list):
            return
        digests = self.extraction_fact_digests.setdefault(file_name, set())
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            digest = hashlib.sha256(
                json.dumps(fact, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest()
            digests.add(digest)
        self.extraction_fact_counts[file_name] = len(digests)

    def record_fact_review_value(self, file_name: str, value: Any) -> None:
        decisions = value.get("decisions") if isinstance(value, dict) else None
        if not isinstance(decisions, list):
            return
        counts = self.fact_review_counts.setdefault(file_name, Counter())
        digests = self.fact_review_decision_digests.setdefault(file_name, set())
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            digest = hashlib.sha256(
                json.dumps(
                    {
                        "field_key": decision.get("field_key"),
                        "source_file_id": decision.get("source_file_id"),
                        "location": decision.get("location"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if digest in digests:
                continue
            digests.add(digest)
            counts[str(decision.get("decision", "UNKNOWN")).lower()] += 1
        counts["covered"] = len(digests)
        counts["uncovered"] = max(
            self.extraction_fact_counts.get(file_name, 0) - counts["covered"], 0
        )

    def record_mapping_payload(self, file_name: str, payload: dict[str, Any]) -> None:
        target_facts = payload.get("target_facts")
        reference_facts = payload.get("reference_facts")
        if isinstance(target_facts, list):
            self.mapping_target_fact_counts[file_name] = len(target_facts)
        if isinstance(reference_facts, list):
            self.mapping_reference_fact_counts[file_name] = len(reference_facts)

    def record_mapping_result(self, file_name: str, value: Any) -> None:
        if not isinstance(value, dict):
            return
        mappings = value.get("mappings")
        requirements = value.get("missing_requirements")
        if isinstance(mappings, list):
            self.mapping_proposal_counts[file_name] = len(mappings)
            decision_counts = self.mapping_decision_counts.setdefault(file_name, Counter())
            for mapping in mappings:
                if isinstance(mapping, dict):
                    decision_counts[str(mapping.get("decision", "UNKNOWN")).lower()] += 1
        if isinstance(requirements, list):
            self.mapping_missing_requirement_counts[file_name] = len(requirements)

    def record_mapping_review(self, file_name: str, payload: dict[str, Any], value: Any) -> None:
        proposed = (payload.get("proposed_mapping") or {}) if isinstance(payload, dict) else {}
        proposals = proposed.get("mappings") if isinstance(proposed, dict) else None
        requirements = (
            proposed.get("missing_requirements") if isinstance(proposed, dict) else None
        )
        counts = self.mapping_review_counts.setdefault(file_name, Counter())
        digests = self.mapping_review_decision_digests.setdefault(file_name, set())
        decisions = value.get("decisions") if isinstance(value, dict) else None
        if isinstance(decisions, list):
            for decision in decisions:
                if not isinstance(decision, dict):
                    continue
                digest = hashlib.sha256(
                    json.dumps(
                        {
                            "target_fact_id": decision.get("target_fact_id"),
                            "reference_field_key": decision.get("reference_field_key"),
                            "source_file_id": decision.get("source_file_id"),
                            "reference_location": decision.get("reference_location"),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if digest in digests:
                    continue
                digests.add(digest)
                counts[str(decision.get("decision", "UNKNOWN")).lower()] += 1
        requirement_decisions = (
            value.get("missing_requirement_decisions")
            if isinstance(value, dict)
            else None
        )
        if isinstance(requirement_decisions, list):
            counts["missing_review_covered"] = len(requirement_decisions)
            counts["missing_review_accept"] = sum(
                item.get("decision") == "ACCEPT"
                for item in requirement_decisions
                if isinstance(item, dict)
            )
            counts["missing_review_reject"] = sum(
                item.get("decision") == "REJECT"
                for item in requirement_decisions
                if isinstance(item, dict)
            )
            counts["missing_review_uncertain"] = sum(
                item.get("decision") == "UNCERTAIN"
                for item in requirement_decisions
                if isinstance(item, dict)
            )
        proposal_count = len(proposals) if isinstance(proposals, list) else 0
        requirement_count = len(requirements) if isinstance(requirements, list) else 0
        counts["covered"] = len(digests)
        counts["omitted"] = max(proposal_count - counts["covered"], 0)
        counts["extra"] = max(counts["covered"] - proposal_count, 0)
        counts["missing_review_omitted"] = max(
            requirement_count - counts.get("missing_review_covered", 0), 0
        )
        if isinstance(value, dict):
            counts["review_evidence_complete"] = int(
                value.get("evidence_complete") is True
            )
            counts["review_confidence_qualified"] = int(
                isinstance(value.get("confidence"), (int, float))
                and not isinstance(value.get("confidence"), bool)
                and float(value["confidence"]) >= 0.8
            )
            proposed_items = (
                proposed.get("mappings") if isinstance(proposed, dict) else None
            )
            review_items = value.get("decisions")
            if isinstance(proposed_items, list) and isinstance(review_items, list):
                def identity(item: dict[str, Any]) -> str:
                    return hashlib.sha256(
                        json.dumps(
                            {
                                "target_fact_id": item.get("target_fact_id"),
                                "reference_field_key": item.get("reference_field_key"),
                                "source_file_id": item.get("source_file_id"),
                                "reference_location": item.get("reference_location"),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()

                proposals_by_id = {
                    identity(item): item
                    for item in proposed_items
                    if isinstance(item, dict)
                }
                reviews_by_id = {
                    identity(item): item
                    for item in review_items
                    if isinstance(item, dict)
                }
                qualified = 0
                for item_id, proposal in proposals_by_id.items():
                    review_item = reviews_by_id.get(item_id)
                    if (
                        proposal.get("decision") == "MATCH"
                        and isinstance(proposal.get("confidence"), (int, float))
                        and not isinstance(proposal.get("confidence"), bool)
                        and float(proposal["confidence"]) >= 0.8
                        and isinstance(review_item, dict)
                        and review_item.get("decision") == "ACCEPT"
                        and isinstance(review_item.get("confidence"), (int, float))
                        and not isinstance(review_item.get("confidence"), bool)
                        and float(review_item["confidence"]) >= 0.8
                    ):
                        qualified += 1
                if not (
                    counts["review_evidence_complete"]
                    and counts["review_confidence_qualified"]
                ):
                    qualified = 0
                gate_counts = self.mapping_gate_counts.setdefault(file_name, Counter())
                gate_counts["proposals"] = len(proposals_by_id)
                gate_counts["individually_qualified"] = qualified
                gate_counts["consumed"] = qualified

    def safe_aggregate_summary(self) -> dict[str, Any]:
        return {
            "extraction_fact_counts": dict(self.extraction_fact_counts),
            "fact_review_counts": {
                file_name: dict(counts) for file_name, counts in self.fact_review_counts.items()
            },
            "mapping_target_fact_counts": dict(self.mapping_target_fact_counts),
            "mapping_reference_fact_counts": dict(self.mapping_reference_fact_counts),
            "mapping_proposal_counts": dict(self.mapping_proposal_counts),
            "mapping_missing_requirement_counts": dict(self.mapping_missing_requirement_counts),
            "mapping_decision_counts": {
                file_name: dict(counts)
                for file_name, counts in self.mapping_decision_counts.items()
            },
            "mapping_review_counts": {
                file_name: dict(counts)
                for file_name, counts in self.mapping_review_counts.items()
            },
            "mapping_gate_counts": {
                file_name: dict(counts)
                for file_name, counts in self.mapping_gate_counts.items()
            },
        }

    def set_phase(self, phase: str) -> None:
        if not self.phase_events or self.phase_events[-1] != phase:
            self.phase_events.append(phase)
            self.writer.emit("phase_started", phase=phase)

    def begin_logical(
        self,
        operation: str,
        file_id: str | None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if (
            self.max_logical_calls is not None
            and self.logical_calls >= self.max_logical_calls
        ):
            self.current_operation = operation
            self.current_file_id = file_id
            self.set_phase(operation)
            self.writer.emit(
                "logical_call_blocked",
                operation=operation,
                file_name=self.file_names.get(file_id, "任务级"),
                reason_code="REAL_LOGICAL_CALL_LIMIT_REACHED",
            )
            raise WorkflowError(
                "REAL_LOGICAL_CALL_LIMIT_REACHED",
                "真实任务已达到新增模型调用上限",
            )
        self.logical_calls += 1
        self.logical_counts[operation] += 1
        self.current_operation = operation
        self.current_file_id = file_id
        self.current_batch_id = None
        batch_id: str | None = None
        fact_operations = {
            "FACT_EXTRACTION",
            "NUMERIC_CANDIDATE_EXTRACTION",
            "TEXT_FACT_EXTRACTION",
        }
        if operation in fact_operations and payload:
            payload_batch_id = payload.get("batch_id")
            if isinstance(payload_batch_id, str):
                batch_id = payload_batch_id
                self.current_batch_id = batch_id
                file_name = self.file_names.get(file_id, "任务级")
                planned = payload.get("planned_batch_count")
                if isinstance(planned, int) and planned >= 0:
                    self.planned_batch_counts[file_name] = max(
                        self.planned_batch_counts[file_name], planned
                    )
                if payload.get("parent_batch_id"):
                    self.recovery_counts[file_name] += 1
        self.call_context.set((operation, file_id, batch_id))
        self.set_phase(operation)
        self.writer.emit(
            "logical_call_started",
            operation=operation,
            file_name=self.file_names.get(file_id, "任务级"),
            batch_depth=(
                payload.get("batch_depth")
                if isinstance(payload, dict) and isinstance(payload.get("batch_depth"), int)
                else None
            ),
            unit_count=(
                len(payload.get("units", []))
                if isinstance(payload, dict) and isinstance(payload.get("units"), list)
                else None
            ),
        )

    def end_logical(
        self,
        result: LlmResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        operation, file_id, batch_id = self.call_context.get()
        fields: dict[str, Any] = {
            "operation": operation,
            "file_name": self.file_names.get(file_id, "任务级"),
        }
        if result is not None:
            # A paired v2 batch has numeric and text calls. Count the pair on
            # the text leg only, so per-document completion metrics remain
            # batch metrics rather than double-counting model calls.
            if batch_id is not None and operation in {
                "FACT_EXTRACTION",
                "NUMERIC_CANDIDATE_EXTRACTION",
                "TEXT_FACT_EXTRACTION",
            }:
                file_name = self.file_names.get(file_id, "任务级")
                self.completed_batch_counts[file_name] += 1
            fields.update(
                request_attempts=result.request_attempts,
                structure_retries=result.structure_retries,
                response_item_count=(
                    len(result.value.get("items", []))
                    if isinstance(result.value, dict)
                    and isinstance(result.value.get("items"), list)
                    else None
                ),
            )
        if error is not None:
            fields["error_code"] = safe_error_code(error)
            fields["failure_code"] = safe_failure_code(error)
            if self.first_failure_stage is None:
                self.first_failure_stage = operation
        self.writer.emit("logical_call_finished", **fields)
        self.current_operation = None
        self.current_file_id = None
        self.current_batch_id = None
        self.call_context.set((None, None, None))

    def begin_http(self, request: httpx.Request) -> None:
        self.http_calls += 1
        operation, file_id, _batch_id = self.call_context.get()
        file_name = self.file_names.get(file_id, "任务级")
        self.operation_request_counts[operation or "UNKNOWN"] += 1
        self.document_request_counts[file_name] += 1
        self.request_chars += len(request.content)
        self.writer.emit(
            "http_request_started",
            operation=operation or "UNKNOWN",
            file_name=file_name,
            request_chars=len(request.content),
        )

    def record_http_response(
        self,
        request: httpx.Request,
        response: httpx.Response,
        content: bytes,
    ) -> None:
        self.response_chars += len(content)
        operation, file_id, _batch_id = self.call_context.get()
        finish_reason: str | None = None
        content_chars = 0
        reasoning_content_chars = 0
        content_empty = None
        content_has_code_fence = None
        json_boundary_type = None
        json_error_type = None
        json_error_position_ratio = None
        usage: dict[str, int] = {}
        if response.status_code < 400:
            try:
                body = json.loads(content)
                choices = body.get("choices") if isinstance(body, dict) else None
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    candidate = choices[0].get("finish_reason")
                    if isinstance(candidate, str):
                        finish_reason = candidate
                    message = choices[0].get("message")
                    if isinstance(message, dict):
                        content = message.get("content")
                        if isinstance(content, str):
                            content_chars = len(content)
                            content_empty = not bool(content.strip())
                            content_has_code_fence = "```" in content
                            stripped = content.strip()
                            if stripped.startswith("{") and stripped.endswith("}"):
                                json_boundary_type = "object"
                            elif stripped.startswith("[") and stripped.endswith("]"):
                                json_boundary_type = "array"
                            else:
                                json_boundary_type = "other"
                            try:
                                json.loads(stripped)
                            except json.JSONDecodeError as exc:
                                json_error_type = exc.msg[:80]
                                json_error_position_ratio = round(
                                    exc.pos / max(len(stripped), 1), 6
                                )
                        reasoning = message.get("reasoning_content")
                        if isinstance(reasoning, str):
                            reasoning_content_chars = len(reasoning)
                raw_usage = body.get("usage") if isinstance(body, dict) else None
                if isinstance(raw_usage, dict):
                    usage = {
                        key: value
                        for key, value in raw_usage.items()
                        if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                        and isinstance(value, int)
                    }
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        self.writer.emit(
            "http_response_finished",
            operation=operation or "UNKNOWN",
            file_name=self.file_names.get(file_id, "任务级"),
            status_code=response.status_code,
            request_chars=len(request.content),
            response_chars=len(content),
            content_chars=content_chars,
            reasoning_content_chars=reasoning_content_chars,
            content_empty=content_empty,
            content_has_code_fence=content_has_code_fence,
            json_boundary_type=json_boundary_type,
            json_error_type=json_error_type,
            json_error_position_ratio=json_error_position_ratio,
            finish_reason=finish_reason,
            usage=usage,
        )

    def record_http_error(self, error: BaseException) -> None:
        operation, file_id, _batch_id = self.call_context.get()
        self.writer.emit(
            "http_request_failed",
            operation=operation or "UNKNOWN",
            file_name=self.file_names.get(file_id, "任务级"),
            error_code=safe_error_code(error),
        )


class RecordingTransport(httpx.AsyncBaseTransport):
    """Buffer one response, record only safe metrics, and close the source."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        metrics: SafeMetrics,
        *,
        read_timeout: float,
    ) -> None:
        self.inner = inner
        self.metrics = metrics
        self.read_timeout = read_timeout

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.metrics.begin_http(request)
        try:
            response = await self.inner.handle_async_request(request)
            try:
                async with asyncio.timeout(self.read_timeout):
                    content = await response.aread()
                self.metrics.record_http_response(request, response, content)
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=content,
                    request=request,
                    extensions=response.extensions,
                )
            finally:
                response_to_close = response
                response = None
                await response_to_close.aclose()
        except BaseException as exc:
            self.metrics.record_http_error(exc)
            raise

    async def aclose(self) -> None:
        # OpenAIContractLlmClient creates one AsyncClient per logical call and
        # closes the supplied transport with each client. Keep the underlying
        # transport alive until the whole one-shot task is finished.
        return None

    async def close_all(self) -> None:
        await self.inner.aclose()


class RecordingLlm:
    def __init__(self, client: OpenAIContractLlmClient, metrics: SafeMetrics) -> None:
        self.client = client
        self.metrics = metrics
        self.mapping_models: dict[str, str | None] = {}

    async def _call(
        self,
        operation: str,
        payload: dict[str, Any],
        method: Callable[[dict[str, Any]], Awaitable[LlmResult]],
        *,
        file_id: str | None = None,
    ) -> LlmResult:
        self.metrics.begin_logical(operation, file_id, payload)
        try:
            result = await method(payload)
        except BaseException as exc:
            self.metrics.end_logical(error=exc)
            raise
        else:
            self.metrics.end_logical(result=result)
            return result


    async def extract_facts(self, payload: dict[str, Any]) -> LlmResult:
        return await self._call(
            "FACT_EXTRACTION",
            payload,
            self.client.extract_facts,
            file_id=payload.get("file_id"),
        )

    async def extract_document_profile(self, payload: dict[str, Any]) -> LlmResult:
        return await self._call(
            "DOCUMENT_PROFILE",
            payload,
            self.client.extract_document_profile,
            file_id=payload.get("file_id"),
        )

    async def extract_fact_batch(self, payload: dict[str, Any]) -> LlmResult:
        return await self._call(
            "FACT_EXTRACTION",
            payload,
            self.client.extract_fact_batch,
            file_id=payload.get("file_id"),
        )

    async def extract_numeric_candidates(self, payload: dict[str, Any]) -> LlmResult:
        return await self._call(
            "NUMERIC_CANDIDATE_EXTRACTION",
            payload,
            self.client.extract_numeric_candidates,
            file_id=payload.get("file_id"),
        )

    async def extract_text_facts(
        self,
        payload: dict[str, Any],
        *,
        allow_structure_correction: bool = True,
    ) -> LlmResult:
        return await self._call(
            "TEXT_FACT_EXTRACTION",
            payload,
            lambda value: self.client.extract_text_facts(
                value,
                allow_structure_correction=allow_structure_correction,
            ),
            file_id=payload.get("file_id"),
        )

    async def review_facts(self, payload: dict[str, Any]) -> LlmResult:
        result = await self._call(
            "FACT_REVIEW",
            payload,
            self.client.review_facts,
            file_id=payload.get("file_id"),
        )
        self.metrics.record_fact_review_value(
            self.metrics._file_name(payload.get("file_id")), result.value
        )
        return result

    async def map_facts(self, payload: dict[str, Any]) -> LlmResult:
        result = await self._call(
            "FACT_MAPPING",
            payload,
            self.client.map_facts,
            file_id=payload.get("reference_file_id"),
        )
        file_name = self.metrics._file_name(payload.get("reference_file_id"))
        self.metrics.record_mapping_payload(file_name, payload)
        self.metrics.record_mapping_result(file_name, result.value)
        self.mapping_models[payload["reference_file_id"]] = (
            result.actual_model or result.configured_model
        )
        return result

    async def review_mappings(self, payload: dict[str, Any]) -> LlmResult:
        result = await self._call(
            "FACT_MAPPING_REVIEW",
            payload,
            self.client.review_mappings,
            file_id=payload.get("reference_file_id"),
        )
        self.metrics.record_mapping_review(
            self.metrics._file_name(payload.get("reference_file_id")), payload, result.value
        )
        reference_file_id = payload.get("reference_file_id")
        self.metrics.record_mapping_model_gate(
            self.metrics._file_name(reference_file_id),
            self.mapping_models.get(reference_file_id),
            result.actual_model or result.configured_model,
        )
        return result

    async def plan_semantics(self, payload: dict[str, Any]) -> LlmResult:
        return await self._call(
            "SEMANTIC_PLAN",
            payload,
            self.client.plan_semantics,
            file_id=payload.get("file_id"),
        )

    async def generate_advice(self, payload: dict[str, Any]) -> LlmResult:
        return await self._call("AI_ADVICE", payload, self.client.generate_advice)


class RecordingCheckpointStore:
    """Decorate the SQL store with aggregate-only diagnostic counters."""

    def __init__(self, inner: SqlAlchemyExtractionCheckpointStore, metrics: SafeMetrics) -> None:
        self.inner = inner
        self.metrics = metrics

    async def load(self, batch_id: str, **kwargs: Any) -> ExtractionCheckpoint | None:
        checkpoint = await self.inner.load(batch_id, **kwargs)
        if checkpoint is not None:
            self.metrics.checkpoint_reused += 1
            file_name = self.metrics.file_names_by_sha.get(str(kwargs.get("file_sha256", "")))
            if file_name and checkpoint.value is not None:
                if checkpoint.extraction_version in {"numeric-v2", TEXT_EXTRACTION_VERSION}:
                    self.metrics.record_extraction_value(file_name, checkpoint.value)
                elif checkpoint.extraction_version == "fact-review-v1":
                    self.metrics.record_fact_review_value(file_name, checkpoint.value)
            if checkpoint.extraction_version in {"numeric-v2", TEXT_EXTRACTION_VERSION}:
                if file_name:
                    self.metrics.completed_batch_counts[file_name] += 1
            self.metrics.writer.emit(
                "checkpoint_reused",
                extraction_version=checkpoint.extraction_version,
            )
        return checkpoint

    async def save(self, checkpoint: ExtractionCheckpoint) -> None:
        await self.inner.save(checkpoint)
        self.metrics.checkpoint_saved += 1
        file_name = self.metrics.file_names_by_sha.get(checkpoint.file_sha256 or "")
        if file_name and checkpoint.value is not None:
            if checkpoint.extraction_version in {"numeric-v2", TEXT_EXTRACTION_VERSION}:
                self.metrics.record_extraction_value(file_name, checkpoint.value)
            elif checkpoint.extraction_version == "fact-review-v1":
                self.metrics.record_fact_review_value(file_name, checkpoint.value)


class LocalRealFileDownloader:
    def __init__(self, source_by_name: dict[str, Path]) -> None:
        self.source_by_name = source_by_name

    async def prepare(
        self,
        files: list[dict[str, Any]],
        workspace: TaskWorkspace,
    ) -> list[LocalFile]:
        prepared: list[LocalFile] = []
        for item in files:
            source = self.source_by_name[item["file_name"]]
            content = source.read_bytes()
            prepared.append(
                LocalFile(
                    file_id=item["file_id"],
                    role=item["role"],
                    file_name=item["file_name"],
                    safe_url="local-diagnostic://redacted",
                    path=source,
                    file_size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    detected_mime_type=DOCX_MIME,
                )
            )
        return prepared


def diagnostic_settings() -> Settings:
    base_settings = Settings()
    return Settings(
        DATABASE_URL=host_diagnostic_database_url(base_settings.DATABASE_URL),
        OCR_ENABLED=False,
        LLM_MAX_OUTPUT_TOKENS=4096,
        # The current gateway model list has no GLM reviewer alias.  DeepSeek
        # is the only currently listed, attributable text model with proven
        # JSON-Schema support; Qwen is too slow/unstable and MiniMax is routed
        # to DeepSeek by the gateway.
        LLM_REVIEW_MODEL="DeepSeek-V4-Flash-0731",
        LLM_STRUCTURE_RETRY_ATTEMPTS=1,
        LLM_EXTRACTION_PAYLOAD_MAX_CHARS=12000,
        LLM_EXTRACTION_MAX_NUMERIC_CANDIDATES=48,
        LLM_EXTRACTION_MAX_FACTS=24,
        LLM_EXTRACTION_ESTIMATED_OUTPUT_TOKENS=4800,
        LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS=2000,
        LLM_EXTRACTION_TASK_CONCURRENCY=2,
        LLM_EXTRACTION_WAVE_SIZE=6,
        LLM_EXTRACTION_MAX_LOGICAL_CALLS_TARGET=MAX_REAL_LOGICAL_CALLS,
        LLM_EXTRACTION_MAX_TEXT_UNITS=16,
        LLM_EXTRACTION_MAX_TEXT_CANDIDATES=8,
        LLM_EXTRACTION_MAX_LOGICAL_CALLS_TOTAL=MAX_REAL_LOGICAL_CALLS,
        LLM_EXTRACTION_MAX_SPLIT_DEPTH=8,
        LLM_EXTRACTION_ABSOLUTE_MAX_REQUESTS_PER_DOCUMENT=128,
        LLM_SEMANTIC_FACT_BATCH_SIZE=8,
        LLM_FACT_REVIEW_ENABLED=False,
        LLM_MAPPING_REVIEW_ENABLED=False,
        LLM_SEMANTIC_PLAN_ENABLED=False,
        LLM_REVIEW_BATCH_MAX_CHARS=8000,
        LLM_SAME_MODEL_DIAGNOSTIC=False,
        LLM_REQUIRE_INDEPENDENT_MODEL=True,
    )


def claim_once(lock_path: Path) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("x", encoding="utf-8") as stream:
            json.dump({"status": "STARTED"}, stream)
    except FileExistsError as exc:
        raise RuntimeError("real diagnostic already started; refusing a second run") from exc


def _file_inputs(metrics: SafeMetrics) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for index, (file_name, role) in enumerate(REAL_FILES, start=1):
        file_id = f"fil_real_{index:02d}"
        metrics.file_names[file_id] = file_name
        files.append(
            {
                "file_id": file_id,
                "role": role,
                "file_name": file_name,
                "url": "local-diagnostic://redacted",
                "safe_url": "local-diagnostic://redacted",
            }
        )
    return files


def _location_present(side: Any) -> bool:
    return isinstance(side, dict) and isinstance(side.get("location"), dict)


def _result_summary(result: dict[str, Any], metrics: SafeMetrics) -> dict[str, Any]:
    TaskResultData.model_validate(result)
    diff_items = result.get("diff_items", [])
    risk_items = result.get("risk_items", [])
    model_advice_items = (result.get("advice") or {}).get("risk_advices", [])
    formal_advice_count = sum(
        isinstance(item, dict)
        and isinstance(item.get("analysis_advice"), str)
        and bool(item["analysis_advice"].strip())
        for item in risk_items
    )
    model_advice_count = sum(
        isinstance(item, dict)
        and isinstance(item.get("analysis_advice"), str)
        and bool(item["analysis_advice"].strip())
        for item in model_advice_items
    )
    evidence_pairs = sum(
        _location_present(item.get("baseline")) and _location_present(item.get("target"))
        for item in diff_items
        if isinstance(item, dict)
    )
    short_advice_count = sum(
        isinstance(item, dict)
        and isinstance(item.get("analysis_advice"), str)
        and "\n" not in item["analysis_advice"]
        and 0 < len(item["analysis_advice"]) <= 240
        for item in risk_items
    )
    fact_matrix = result.get("fact_matrix", [])
    fact_matrix_status_counts = Counter(
        str(item.get("status"))
        for item in fact_matrix
        if isinstance(item, dict) and item.get("status")
    )
    consumed_mapping_count = sum(
        relation.get("status") in {"CONSISTENT", "CONFLICT"}
        for item in fact_matrix
        if isinstance(item, dict)
        for relation in item.get("reference_results", [])
        if isinstance(relation, dict)
    )
    return {
        "result_schema_valid": True,
        "execution_mode": result.get("metadata", {}).get("execution_mode"),
        "formal_diff_count": len(diff_items),
        "diff_evidence_pair_count": evidence_pairs,
        "risk_item_count": len(risk_items),
        "ai_advice_count": formal_advice_count,
        "model_advice_count": model_advice_count,
        "fallback_advice_count": max(formal_advice_count - model_advice_count, 0),
        "short_ai_advice_count": short_advice_count,
        "conclusion": result.get("conclusion"),
        "fact_matrix_status_counts": dict(fact_matrix_status_counts),
        "fact_matrix_consumed_mapping_count": consumed_mapping_count,
        "formal_fact_conflict_count": fact_matrix_status_counts.get("CONFLICT", 0),
        "formal_fact_missing_count": fact_matrix_status_counts.get("MISSING", 0),
        "formal_fact_uncertain_count": fact_matrix_status_counts.get("UNCERTAIN", 0),
        "formal_fact_passed_count": sum(
            item.get("module_code") == "FACT_CONSISTENCY"
            for item in result.get("passed_checks", [])
            if isinstance(item, dict)
        ),
        "fact_mapping_aggregates": metrics.safe_aggregate_summary(),
        "document_request_counts": {
            file_name: metrics.document_request_counts.get(file_name, 0)
            for file_name, _role in REAL_FILES
        },
        "document_batch_counts": {
            file_name: {
                "planned": metrics.planned_batch_counts.get(file_name, 0),
                "completed": metrics.completed_batch_counts.get(file_name, 0),
                "recovery": metrics.recovery_counts.get(file_name, 0),
            }
            for file_name, _role in REAL_FILES
        },
        "workflow_phase_events": metrics.phase_events,
    }


def _canary_units(document: Any) -> list[Any]:
    """Select at most five deterministic structural representatives."""

    units = extraction_units(document, max_unit_chars=10000)
    selected: list[Any] = []
    seen: set[str] = set()

    def add(unit: Any) -> None:
        unit_id = stable_unit_id(unit)
        if unit_id not in seen and len(selected) < 5:
            seen.add(unit_id)
            selected.append(unit)

    paragraphs = [unit for unit in units if unit.type == "PARAGRAPH"]
    ordinary = next((unit for unit in paragraphs if len(unit.raw_text) < 240), None)
    if ordinary is not None:
        add(ordinary)
    if paragraphs:
        add(max(paragraphs, key=lambda unit: len(unit.raw_text)))
    numeric = max(units, key=lambda unit: len(numeric_candidates([unit])), default=None)
    if numeric is not None and numeric_candidates([numeric]):
        add(numeric)
    table_units = [unit for unit in units if unit.table is not None]
    if table_units:
        add(table_units[0])
        add(max(table_units, key=lambda unit: len(unit.table.rows[0].cells)))
    for unit in units:
        add(unit)
        if len(selected) == 5:
            break
    return selected


async def run_canary_once(
    *,
    output_path: Path,
    lock_path: Path,
    sample_dir: Path = SAMPLE_DIR,
    force_probe: bool = False,
) -> int:
    """Run the one-shot target-candidate canary; no structure retries."""

    claim_once(lock_path)
    writer = SafeMetricWriter(output_path)
    metrics = SafeMetrics(writer=writer, file_names={"canary_target": REAL_FILES[0][0]})
    transport: RecordingTransport | None = None
    try:
        settings = diagnostic_settings()
        if force_probe:
            probe = await run_production_probe(settings)
            writer.emit(
                "structured_output_probe",
                production_gate_passed=probe.get("production_gate_passed"),
                selected_response_format=probe.get("selected_response_format"),
                probe_http_calls=probe.get("total_http_calls", 0),
            )
            if not probe.get("production_gate_passed"):
                writer.emit("canary_finished", status="BLOCKED", reason_code="PROBE_GATE_FAILED")
                return 2
            selected_format = probe["selected_response_format"]
        else:
            selected_format = "json_schema"
            writer.emit(
                "structured_output_probe_skipped",
                selected_response_format=selected_format,
                reason_code="VALIDATED_DEPLOYMENT_CONFIGURATION",
            )
        settings.LLM_RESPONSE_FORMAT = selected_format
        settings.LLM_NATIVE_STRUCTURED_OUTPUT = selected_format == "json_schema"
        parser = DocxParser()
        target_path = sample_dir / REAL_FILES[0][0]
        template_path = sample_dir / REAL_FILES[1][0]
        target_content = target_path.read_bytes()
        template_content = template_path.read_bytes()
        target_file = LocalFile(
            file_id="canary_target",
            role="TARGET",
            file_name=target_path.name,
            safe_url="local-diagnostic://redacted",
            path=target_path,
            file_size=len(target_content),
            sha256=hashlib.sha256(target_content).hexdigest(),
            detected_mime_type=DOCX_MIME,
        )
        template_file = LocalFile(
            file_id="canary_template",
            role="TEMPLATE",
            file_name=template_path.name,
            safe_url="local-diagnostic://redacted",
            path=template_path,
            file_size=len(template_content),
            sha256=hashlib.sha256(template_content).hexdigest(),
            detected_mime_type=DOCX_MIME,
        )
        document = await parser.parse(target_file)
        template = await parser.parse(template_file)
        template_review = analyze_template(template, document)
        candidates = build_template_text_candidates(template_review, document)
        plans = plan_text_candidate_batches(
            document,
            candidates,
            max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
            max_candidates=settings.LLM_EXTRACTION_MAX_TEXT_CANDIDATES,
            estimated_output_token_limit=settings.LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS,
        )
        selected_plans = plans[:3]
        if not selected_plans:
            writer.emit(
                "canary_finished",
                status="BLOCKED",
                reason_code="CANARY_CANDIDATE_SELECTION",
            )
            return 2
        transport = RecordingTransport(
            httpx.AsyncHTTPTransport(retries=0),
            metrics,
            read_timeout=settings.LLM_TIMEOUT_SECONDS,
        )
        client = OpenAIContractLlmClient(settings, transport=transport)
        recorder = RecordingLlm(client, metrics)
        results: list[dict[str, Any]] = []
        for index, plan in enumerate(selected_plans, start=1):
            payload = dict(plan["payload"])
            payload.update(
                {
                    "batch_depth": 0,
                    "parent_batch_id": None,
                    "planned_batch_count": len(plans),
                }
            )
            try:
                result = await recorder.extract_text_facts(
                    payload, allow_structure_correction=False
                )
                expand_text_fact_response(payload, result.value)
                results.append(
                    {
                        "sample_index": index,
                        "candidate_count": len(plan["blocks"]),
                        "location_type": (
                            "table"
                            if any(
                                block.location.table_index is not None
                                for block in plan["blocks"]
                            )
                            else "paragraph"
                        ),
                        "status": "SUCCEEDED",
                        "finish_reason": result.finish_reason,
                        "response_item_count": len(result.value.get("items", [])),
                    }
                )
            except BaseException as exc:
                results.append(
                    {
                        "sample_index": index,
                        "candidate_count": len(plan["blocks"]),
                        "status": "FAILED",
                        "error_code": safe_error_code(exc),
                    }
                )
                writer.emit("canary_batch_failed", **results[-1])
                writer.emit(
                    "canary_finished",
                    status="FAILED",
                    business_calls=index,
                    passed=sum(item["status"] == "SUCCEEDED" for item in results),
                    http_calls=metrics.http_calls,
                    logical_calls=metrics.logical_calls,
                )
                return 2
        writer.emit(
            "canary_finished",
            status="SUCCEEDED",
            business_calls=len(results),
            passed=len(results),
            candidate_count=len(candidates),
            planned_candidate_batch_count=len(plans),
            http_calls=metrics.http_calls,
            logical_calls=metrics.logical_calls,
            batches=results,
        )
        return 0
    except BaseException as exc:
        writer.emit(
            "canary_finished",
            status="FAILED",
            error_code=safe_error_code(exc),
            http_calls=metrics.http_calls,
            logical_calls=metrics.logical_calls,
        )
        return 2
    finally:
        if transport is not None:
            await transport.close_all()
        writer.close()


async def run_text_diagnosis_once(
    *,
    output_path: Path,
    lock_path: Path,
    sample_dir: Path = SAMPLE_DIR,
) -> int:
    """Diagnose one dense text batch with at most two logical calls."""

    claim_once(lock_path)
    writer = SafeMetricWriter(output_path)
    metrics = SafeMetrics(writer=writer, file_names={})
    transport: RecordingTransport | None = None
    observed: list[dict[str, Any]] = []
    failure: str | None = None
    try:
        settings = diagnostic_settings()
        settings.LLM_RESPONSE_FORMAT = "json_schema"
        settings.LLM_NATIVE_STRUCTURED_OUTPUT = True
        target_path = sample_dir / REAL_FILES[0][0]
        content = target_path.read_bytes()
        metrics.file_names["diagnostic_target"] = target_path.name
        local_file = LocalFile(
            file_id="diagnostic_target",
            role="TARGET",
            file_name=target_path.name,
            safe_url="local-diagnostic://redacted",
            path=target_path,
            file_size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            detected_mime_type=DOCX_MIME,
        )
        document = await DocxParser().parse(local_file)
        plans = plan_text_document_batches(
            document,
            max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
            max_text_units=settings.LLM_EXTRACTION_MAX_TEXT_UNITS,
            estimated_output_token_limit=settings.LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS,
        )
        if not plans:
            raise WorkflowError("DYNAMIC_CHECK_INCOMPLETE", "没有可诊断的文本批次")
        # The largest unit-count batch is the safest deterministic proxy for
        # the previously failed dense first-wave batches.
        selected = max(plans, key=lambda item: (len(item["blocks"]), len(item["payload"]["units"])))
        payload = selected["payload"]
        payload.update(
            {
                "batch_depth": 0,
                "parent_batch_id": None,
                "planned_batch_count": len(plans),
            }
        )
        transport = RecordingTransport(
            httpx.AsyncHTTPTransport(retries=0),
            metrics,
            read_timeout=settings.LLM_TIMEOUT_SECONDS,
        )
        client = OpenAIContractLlmClient(settings, transport=transport)
        recorder = RecordingLlm(client, metrics)
        for attempt in range(1, 3):
            try:
                result = await recorder.extract_text_facts(
                    payload,
                    allow_structure_correction=False,
                )
                facts = expand_text_fact_response(payload, result.value)
                observed.append(
                    {
                        "attempt": attempt,
                        "status": "SUCCEEDED",
                        "response_item_count": len(result.value.get("items", [])),
                        "validated_fact_count": len(facts),
                        "finish_reason": result.finish_reason,
                    }
                )
                break
            except BaseException as exc:
                code = safe_failure_code(exc)
                observed.append(
                    {
                        "attempt": attempt,
                        "status": "FAILED",
                        "error_code": safe_error_code(exc),
                        "failure_code": code,
                        "batch_depth": payload.get("batch_depth", 0),
                        "unit_count": len(payload.get("units", [])),
                    }
                )
                if attempt == 1 and len(selected["blocks"]) > 1 and code in {
                    "FACT_BATCH_SATURATED",
                    "FACT_UNIT_NOT_FOUND",
                    "FACT_QUOTE_NOT_GROUNDED",
                    "FACT_IDENTITY_DUPLICATED",
                }:
                    midpoint = len(selected["blocks"]) // 2
                    child_blocks = selected["blocks"][:midpoint]
                    child_id = stable_batch_id(
                        document.sha256,
                        child_blocks,
                        TEXT_EXTRACTION_VERSION,
                    )
                    payload = build_text_fact_payload(
                        document,
                        child_blocks,
                        batch_id=child_id,
                    )
                    payload.update(
                        {
                            "batch_depth": 1,
                            "parent_batch_id": selected["batch_id"],
                            "planned_batch_count": len(plans),
                        }
                    )
                    continue
                break
        status = "SUCCEEDED" if observed and observed[-1]["status"] == "SUCCEEDED" else "FAILED"
        writer.emit(
            "text_diagnosis_finished",
            status=status,
            planned_text_batch_count=len(plans),
            selected_unit_count=len(selected["blocks"]),
            calls=len(observed),
            observed=observed,
            http_calls=metrics.http_calls,
            logical_calls=metrics.logical_calls,
        )
        return 0 if status == "SUCCEEDED" else 2
    except BaseException as exc:
        failure = safe_error_code(exc)
        writer.emit("text_diagnosis_finished", status="FAILED", error_code=failure)
        return 2
    finally:
        if transport is not None:
            await transport.close_all()
        writer.close()


async def run_once(
    *,
    output_path: Path = DEFAULT_OUTPUT,
    lock_path: Path = DEFAULT_LOCK,
    sample_dir: Path = SAMPLE_DIR,
    force_probe: bool = False,
    source_task_id: str | None = None,
) -> int:
    claim_once(lock_path)

    writer = SafeMetricWriter(output_path)
    metrics = SafeMetrics(
        writer=writer,
        file_names={},
        max_logical_calls=MAX_REAL_LOGICAL_CALLS,
    )
    result_summary: dict[str, Any] = {}
    failure: str | None = None
    failure_subcode: str | None = None
    transport: RecordingTransport | None = None
    checkpoint_engine: AsyncEngine | None = None
    started_at = time.monotonic()
    try:
        settings = diagnostic_settings()
        if not settings.llm_configured:
            raise RuntimeError("LLM gateway is not configured")
        if force_probe:
            probe = await run_production_probe(settings)
            metrics.probe_http_calls = int(probe.get("total_http_calls", 0))
            if not probe.get("production_gate_passed"):
                writer.emit(
                    "structured_output_probe_gate_failed",
                    selected_response_format=probe.get("selected_response_format"),
                    probe_http_calls=metrics.probe_http_calls,
                )
                raise WorkflowError(
                    "DYNAMIC_CHECK_INCOMPLETE",
                    "复杂结构化输出探测未达到两个 Schema 各 3/3 成功门槛",
                )
            selected_format = probe["selected_response_format"]
            writer.emit(
                "structured_output_probe",
                json_schema=probe["json_schema"],
                json_object=probe["json_object"],
                selected_response_format=selected_format,
                production_gate_passed=probe["production_gate_passed"],
                probe_http_calls=metrics.probe_http_calls,
            )
        else:
            selected_format = "json_schema"
            writer.emit(
                "structured_output_probe_skipped",
                selected_response_format=selected_format,
                reason_code="VALIDATED_DEPLOYMENT_CONFIGURATION",
            )
        settings.LLM_RESPONSE_FORMAT = selected_format
        settings.LLM_NATIVE_STRUCTURED_OUTPUT = selected_format == "json_schema"
        source_by_name = {file_name: sample_dir / file_name for file_name, _role in REAL_FILES}
        missing = [file_name for file_name, path in source_by_name.items() if not path.is_file()]
        if missing:
            raise FileNotFoundError("real diagnostic sample is incomplete")
        metrics.file_names_by_sha = {
            hashlib.sha256(path.read_bytes()).hexdigest(): file_name
            for file_name, path in source_by_name.items()
        }
        files = _file_inputs(metrics)
        writer.emit("run_started", file_count=len(files), ocr_enabled=False)
        transport = RecordingTransport(
            httpx.AsyncHTTPTransport(retries=0),
            metrics,
            read_timeout=settings.LLM_TIMEOUT_SECONDS,
        )
        client = OpenAIContractLlmClient(settings, transport=transport)
        checkpoint_engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
        checkpoint_session_factory = async_sessionmaker(
            checkpoint_engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
        checkpoint_store = RecordingCheckpointStore(
            SqlAlchemyExtractionCheckpointStore(checkpoint_session_factory), metrics
        )
        task_id = "real_" + hashlib.sha256(
            str(output_path.resolve()).encode("utf-8")
        ).hexdigest()[:16]
        executor = DraftReviewWorkflowExecutor(
            settings,
            downloader=LocalRealFileDownloader(source_by_name),
            llm=RecordingLlm(client, metrics),
            checkpoint_store=checkpoint_store,
        )

        async def progress(stage: TaskStage, value: int, _message: str) -> None:
            phase = stage.value
            if stage == TaskStage.RULE_CHECKING:
                phase = "NUMERIC_VALIDATION_AND_FORMAL_DIFF"
            metrics.set_phase(phase)
            writer.emit("workflow_progress", stage=stage.value, progress=value)

        async with asyncio.timeout(MAX_REAL_RUNTIME_SECONDS):
            output = await executor.run(
                task_id=task_id,
                task_type=TaskType.DRAFT_REVIEW,
                files=files,
                options={
                    "source_task_id": source_task_id or "tsk_real_diagnostic_in_memory"
                },
                progress_callback=progress,
            )
        result_summary = _result_summary(output.result, metrics)
        writer.emit("result_verified", **result_summary)
    except asyncio.CancelledError:
        failure = "CANCELLED"
        if metrics.first_failure_stage is None:
            metrics.first_failure_stage = metrics.current_operation or (
                metrics.phase_events[-1] if metrics.phase_events else None
            )
    except Exception as exc:
        failure = safe_error_code(exc)
        failure_subcode = safe_failure_code(exc)
        if metrics.first_failure_stage is None:
            metrics.first_failure_stage = metrics.current_operation or (
                metrics.phase_events[-1] if metrics.phase_events else None
            )
        writer.emit(
            "run_failed",
            error_code=failure,
            failure_subcode=failure_subcode,
            first_failure_stage=metrics.first_failure_stage,
            failure_detail_counts=(
                {
                    key: value
                    for key, value in exc.details.items()
                    if isinstance(value, int) and not isinstance(value, bool)
                }
                if isinstance(exc, WorkflowError) and isinstance(exc.details, dict)
                else None
            ),
        )
    finally:
        if transport is not None:
            try:
                await transport.close_all()
            except BaseException as exc:
                if failure is None:
                    failure = safe_error_code(exc)
                    failure_subcode = safe_failure_code(exc)
                if metrics.first_failure_stage is None:
                    metrics.first_failure_stage = metrics.current_operation or (
                        metrics.phase_events[-1] if metrics.phase_events else None
                    )
                writer.emit("transport_close_failed", error_code=safe_error_code(exc))
        if checkpoint_engine is not None:
            try:
                await checkpoint_engine.dispose()
            except BaseException as exc:
                if failure is None:
                    failure = safe_error_code(exc)
                    failure_subcode = safe_failure_code(exc)
                if metrics.first_failure_stage is None:
                    metrics.first_failure_stage = "CHECKPOINT_CLOSE"
                writer.emit("checkpoint_close_failed", error_code=safe_error_code(exc))
        try:
            writer.emit(
                "final_summary",
                status="FAILED" if failure else "SUCCEEDED",
                first_failure_stage=metrics.first_failure_stage,
                failure_code=failure,
                failure_subcode=failure_subcode,
                http_calls=metrics.http_calls + metrics.probe_http_calls,
                task_http_calls=metrics.http_calls,
                probe_http_calls=metrics.probe_http_calls,
                logical_calls=metrics.logical_calls,
                logical_call_limit=MAX_REAL_LOGICAL_CALLS,
                elapsed_seconds=round(time.monotonic() - started_at, 3),
                checkpoint_reused=metrics.checkpoint_reused,
                checkpoint_saved=metrics.checkpoint_saved,
                operation_request_counts=dict(metrics.operation_request_counts),
                document_request_counts={
                    file_name: metrics.document_request_counts.get(file_name, 0)
                    for file_name, _role in REAL_FILES
                },
                document_batch_counts={
                    file_name: {
                        "planned": metrics.planned_batch_counts.get(file_name, 0),
                        "completed": metrics.completed_batch_counts.get(file_name, 0),
                        "recovery": metrics.recovery_counts.get(file_name, 0),
                    }
                    for file_name, _role in REAL_FILES
                },
                workflow_phase_events=metrics.phase_events,
                response_chars=metrics.response_chars,
                request_chars=metrics.request_chars,
                fact_mapping_aggregates=metrics.safe_aggregate_summary(),
                result=result_summary,
            )
        finally:
            writer.close()
    return 2 if failure else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one safe real DRAFT_REVIEW diagnostic")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--sample-dir", type=Path, default=SAMPLE_DIR)
    parser.add_argument("--canary", action="store_true")
    parser.add_argument("--text-diagnosis", action="store_true")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="显式执行部署级结构化输出探测；普通合同任务不重复探测",
    )
    parser.add_argument(
        "--source-task-id",
        default=None,
        help="仅用于从同一文件哈希和抽取版本读取成功 checkpoint",
    )
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    raise SystemExit(
        asyncio.run(
            run_text_diagnosis_once(
                output_path=arguments.output,
                lock_path=arguments.lock,
                sample_dir=arguments.sample_dir,
            )
            if arguments.text_diagnosis
            else run_canary_once(
                output_path=arguments.output,
                lock_path=arguments.lock,
                sample_dir=arguments.sample_dir,
                force_probe=arguments.probe,
            )
            if arguments.canary
            else run_once(
                output_path=arguments.output,
                lock_path=arguments.lock,
                sample_dir=arguments.sample_dir,
                force_probe=arguments.probe,
                source_task_id=arguments.source_task_id,
            )
        )
    )
