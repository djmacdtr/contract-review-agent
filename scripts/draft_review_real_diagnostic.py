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
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.adapters.llm.base import LlmResult
from app.adapters.llm.openai_client import LlmClientError, OpenAIContractLlmClient
from app.core.config import Settings
from app.core.enums import TaskStage, TaskType
from app.core.errors import WorkflowError
from app.documents.parsers import DocxParser
from app.draft_review.facts import (
    build_numeric_candidate_payload,
    build_text_fact_payload,
    expand_numeric_candidate_response,
    expand_text_fact_response,
    extraction_units,
    numeric_candidates,
    stable_batch_id,
    stable_unit_id,
)
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


def safe_error_code(exc: BaseException) -> str:
    if isinstance(exc, LlmClientError):
        return exc.code
    if isinstance(exc, WorkflowError):
        return exc.code
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    return type(exc).__name__


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
                "TEXT_FACT_EXTRACTION",
            }:
                file_name = self.file_names.get(file_id, "任务级")
                self.completed_batch_counts[file_name] += 1
            fields.update(
                request_attempts=result.request_attempts,
                structure_retries=result.structure_retries,
            )
        if error is not None:
            fields["error_code"] = safe_error_code(error)
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
        usage: dict[str, int] = {}
        if response.status_code < 400:
            try:
                body = json.loads(content)
                choices = body.get("choices") if isinstance(body, dict) else None
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    candidate = choices[0].get("finish_reason")
                    if isinstance(candidate, str):
                        finish_reason = candidate
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

    async def extract_text_facts(self, payload: dict[str, Any]) -> LlmResult:
        return await self._call(
            "TEXT_FACT_EXTRACTION",
            payload,
            self.client.extract_text_facts,
            file_id=payload.get("file_id"),
        )

    async def review_facts(self, payload: dict[str, Any]) -> LlmResult:
        return await self._call(
            "FACT_REVIEW",
            payload,
            self.client.review_facts,
            file_id=payload.get("file_id"),
        )

    async def map_facts(self, payload: dict[str, Any]) -> LlmResult:
        return await self._call(
            "FACT_MAPPING",
            payload,
            self.client.map_facts,
            file_id=payload.get("reference_file_id"),
        )

    async def review_mappings(self, payload: dict[str, Any]) -> LlmResult:
        return await self._call(
            "FACT_MAPPING_REVIEW",
            payload,
            self.client.review_mappings,
            file_id=payload.get("reference_file_id"),
        )

    async def plan_semantics(self, payload: dict[str, Any]) -> LlmResult:
        return await self._call(
            "SEMANTIC_PLAN",
            payload,
            self.client.plan_semantics,
            file_id=payload.get("file_id"),
        )

    async def generate_advice(self, payload: dict[str, Any]) -> LlmResult:
        return await self._call("AI_ADVICE", payload, self.client.generate_advice)


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
    return Settings(
        OCR_ENABLED=False,
        LLM_MAX_OUTPUT_TOKENS=4096,
        LLM_STRUCTURE_RETRY_ATTEMPTS=1,
        LLM_EXTRACTION_PAYLOAD_MAX_CHARS=12000,
        LLM_EXTRACTION_MAX_NUMERIC_CANDIDATES=48,
        LLM_EXTRACTION_MAX_FACTS=24,
        LLM_EXTRACTION_ESTIMATED_OUTPUT_TOKENS=4800,
        LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS=2000,
        LLM_EXTRACTION_TASK_CONCURRENCY=2,
        LLM_EXTRACTION_WAVE_SIZE=6,
        LLM_EXTRACTION_MAX_LOGICAL_CALLS_TARGET=40,
        LLM_EXTRACTION_MAX_LOGICAL_CALLS_TOTAL=50,
        LLM_EXTRACTION_MAX_SPLIT_DEPTH=8,
        LLM_EXTRACTION_ABSOLUTE_MAX_REQUESTS_PER_DOCUMENT=128,
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
    advice_items = (result.get("advice") or {}).get("risk_advices", [])
    advice_count = sum(
        isinstance(item, dict)
        and isinstance(item.get("analysis_advice"), str)
        and bool(item["analysis_advice"].strip())
        for item in advice_items
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
        for item in advice_items
    )
    return {
        "result_schema_valid": True,
        "execution_mode": result.get("metadata", {}).get("execution_mode"),
        "formal_diff_count": len(diff_items),
        "diff_evidence_pair_count": evidence_pairs,
        "ai_advice_count": advice_count,
        "short_ai_advice_count": short_advice_count,
        "conclusion": result.get("conclusion"),
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
) -> int:
    """Run the one-shot five-batch business canary; no structure retries."""

    claim_once(lock_path)
    writer = SafeMetricWriter(output_path)
    try:
        settings = diagnostic_settings()
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
        settings.LLM_RESPONSE_FORMAT = probe["selected_response_format"]
        settings.LLM_NATIVE_STRUCTURED_OUTPUT = probe["selected_response_format"] == "json_schema"
        target_path = sample_dir / REAL_FILES[0][0]
        content = target_path.read_bytes()
        local_file = LocalFile(
            file_id="canary_target",
            role="TARGET",
            file_name=target_path.name,
            safe_url="local-diagnostic://redacted",
            path=target_path,
            file_size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            detected_mime_type=DOCX_MIME,
        )
        document = await DocxParser().parse(local_file)
        units = _canary_units(document)
        if len(units) != 5:
            writer.emit("canary_finished", status="BLOCKED", reason_code="CANARY_UNIT_SELECTION")
            return 2
        client = OpenAIContractLlmClient(settings)
        results: list[dict[str, Any]] = []
        for index, unit in enumerate(units, start=1):
            batch_id = stable_batch_id(document.sha256, [unit])
            candidates = numeric_candidates([unit])
            try:
                if candidates:
                    payload = build_numeric_candidate_payload(document, [unit], batch_id=batch_id)
                    result = await client.extract_numeric_candidates(
                        payload, allow_structure_correction=False
                    )
                    expand_numeric_candidate_response(payload, result.value)
                    chain = "numeric"
                else:
                    payload = build_text_fact_payload(document, [unit], batch_id=batch_id)
                    result = await client.extract_text_facts(
                        payload, allow_structure_correction=False
                    )
                    expand_text_fact_response(payload, result.value)
                    chain = "text"
                results.append(
                    {
                        "sample_index": index,
                        "unit_type": unit.type,
                        "chain": chain,
                        "status": "SUCCEEDED",
                        "finish_reason": result.finish_reason,
                    }
                )
            except BaseException as exc:
                results.append(
                    {
                        "sample_index": index,
                        "unit_type": unit.type,
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
                )
                return 2
        writer.emit(
            "canary_finished",
            status="SUCCEEDED",
            business_calls=len(results),
            passed=len(results),
            batches=results,
        )
        return 0
    except BaseException as exc:
        writer.emit("canary_finished", status="FAILED", error_code=safe_error_code(exc))
        return 2
    finally:
        writer.close()


async def run_once(
    *,
    output_path: Path = DEFAULT_OUTPUT,
    lock_path: Path = DEFAULT_LOCK,
    sample_dir: Path = SAMPLE_DIR,
) -> int:
    claim_once(lock_path)

    writer = SafeMetricWriter(output_path)
    metrics = SafeMetrics(writer=writer, file_names={})
    result_summary: dict[str, Any] = {}
    failure: str | None = None
    transport: RecordingTransport | None = None
    try:
        settings = diagnostic_settings()
        if not settings.llm_configured:
            raise RuntimeError("LLM gateway is not configured")
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
        settings.LLM_RESPONSE_FORMAT = probe["selected_response_format"]
        settings.LLM_NATIVE_STRUCTURED_OUTPUT = (
            probe["selected_response_format"] == "json_schema"
        )
        writer.emit(
            "structured_output_probe",
            json_schema=probe["json_schema"],
            json_object=probe["json_object"],
            selected_response_format=probe["selected_response_format"],
            production_gate_passed=probe["production_gate_passed"],
            probe_http_calls=metrics.probe_http_calls,
        )
        source_by_name = {file_name: sample_dir / file_name for file_name, _role in REAL_FILES}
        missing = [file_name for file_name, path in source_by_name.items() if not path.is_file()]
        if missing:
            raise FileNotFoundError("real diagnostic sample is incomplete")
        files = _file_inputs(metrics)
        writer.emit("run_started", file_count=len(files), ocr_enabled=False)
        transport = RecordingTransport(
            httpx.AsyncHTTPTransport(retries=0),
            metrics,
            read_timeout=settings.LLM_TIMEOUT_SECONDS,
        )
        client = OpenAIContractLlmClient(settings, transport=transport)
        executor = DraftReviewWorkflowExecutor(
            settings,
            downloader=LocalRealFileDownloader(source_by_name),
            llm=RecordingLlm(client, metrics),
        )

        async def progress(stage: TaskStage, value: int, _message: str) -> None:
            phase = stage.value
            if stage == TaskStage.RULE_CHECKING:
                phase = "NUMERIC_VALIDATION_AND_FORMAL_DIFF"
            metrics.set_phase(phase)
            writer.emit("workflow_progress", stage=stage.value, progress=value)

        output = await executor.run(
            task_id="tsk_real_diagnostic_in_memory",
            task_type=TaskType.DRAFT_REVIEW,
            files=files,
            options={},
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
        if metrics.first_failure_stage is None:
            metrics.first_failure_stage = metrics.current_operation or (
                metrics.phase_events[-1] if metrics.phase_events else None
            )
        writer.emit(
            "run_failed",
            error_code=failure,
            first_failure_stage=metrics.first_failure_stage,
        )
    finally:
        if transport is not None:
            try:
                await transport.close_all()
            except BaseException as exc:
                if failure is None:
                    failure = safe_error_code(exc)
                if metrics.first_failure_stage is None:
                    metrics.first_failure_stage = metrics.current_operation or (
                        metrics.phase_events[-1] if metrics.phase_events else None
                    )
                writer.emit("transport_close_failed", error_code=safe_error_code(exc))
        try:
            writer.emit(
                "final_summary",
                status="FAILED" if failure else "SUCCEEDED",
                first_failure_stage=metrics.first_failure_stage,
                failure_code=failure,
                http_calls=metrics.http_calls + metrics.probe_http_calls,
                task_http_calls=metrics.http_calls,
                probe_http_calls=metrics.probe_http_calls,
                logical_calls=metrics.logical_calls,
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
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    raise SystemExit(
        asyncio.run(
            run_canary_once(
                output_path=arguments.output,
                lock_path=arguments.lock,
                sample_dir=arguments.sample_dir,
            )
            if arguments.canary
            else run_once(
                output_path=arguments.output,
                lock_path=arguments.lock,
                sample_dir=arguments.sample_dir,
            )
        )
    )
