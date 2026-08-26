import asyncio
import json

import httpx
import pytest

from app.adapters.llm.base import LlmResult
from app.core.config import Settings
from app.core.errors import WorkflowError
from scripts.draft_review_real_diagnostic import (
    RecordingTransport,
    SafeMetrics,
    SafeMetricWriter,
    claim_once,
    host_diagnostic_database_url,
)
from scripts.llm_structured_output_probe import probe_mode


@pytest.mark.asyncio
async def test_recording_transport_counts_one_request_and_flushes_safe_metrics(
    tmp_path,
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": "hidden-model",
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            },
            request=request,
        )

    output_path = tmp_path / "metrics.jsonl"
    writer = SafeMetricWriter(output_path)
    metrics = SafeMetrics(writer=writer, file_names={"fil_target": "target.docx"})
    metrics.begin_logical("FACT_EXTRACTION", "fil_target")
    transport = RecordingTransport(
        httpx.MockTransport(handler), metrics, read_timeout=0.1
    )
    try:
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            response = await client.post(
                "https://llm.example.com/v1/chat/completions",
                content=b'{"secret":"request-body"}',
            )
            assert response.status_code == 200
        metrics.end_logical()
    finally:
        await transport.close_all()
        writer.emit("final_summary", http_calls=metrics.http_calls)
        writer.close()

    assert calls == 1
    assert metrics.http_calls == 1
    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6
    serialized = "\n".join(lines)
    assert "hidden-model" not in serialized
    assert "secret" not in serialized
    assert all("request-body" not in line for line in lines)
    assert json.loads(lines[-1]) == {"event": "final_summary", "http_calls": 1}


class HangingResponseStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self):
        yield b"{"
        await asyncio.sleep(1)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_recording_transport_times_out_and_closes_response(tmp_path) -> None:
    stream = HangingResponseStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    writer = SafeMetricWriter(tmp_path / "timeout.jsonl")
    metrics = SafeMetrics(writer=writer, file_names={})
    transport = RecordingTransport(
        httpx.MockTransport(handler), metrics, read_timeout=0.01
    )
    try:
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            with pytest.raises(TimeoutError):
                await client.get("https://llm.example.com/v1/chat/completions")
    finally:
        await transport.close_all()
        writer.emit("final_summary", http_calls=metrics.http_calls)
        writer.close()

    assert stream.closed is True
    lines = (tmp_path / "timeout.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1]) == {"event": "final_summary", "http_calls": 1}


@pytest.mark.asyncio
async def test_recording_transport_cancellation_closes_response_and_flushes(
    tmp_path,
) -> None:
    stream = HangingResponseStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    output_path = tmp_path / "cancelled.jsonl"
    writer = SafeMetricWriter(output_path)
    metrics = SafeMetrics(writer=writer, file_names={})
    transport = RecordingTransport(
        httpx.MockTransport(handler), metrics, read_timeout=1
    )
    request_task = None
    try:
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            request_task = asyncio.create_task(
                client.get("https://llm.example.com/v1/chat/completions")
            )
            await asyncio.sleep(0.01)
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
    finally:
        await transport.close_all()
        writer.emit("final_summary", http_calls=metrics.http_calls)
        writer.close()

    assert stream.closed is True
    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1]) == {"event": "final_summary", "http_calls": 1}


def test_one_shot_lock_refuses_a_second_real_run(tmp_path) -> None:
    lock_path = tmp_path / "real-run.lock"
    claim_once(lock_path)

    with pytest.raises(RuntimeError, match="second run"):
        claim_once(lock_path)


def test_host_diagnostic_uses_published_postgres_port() -> None:
    assert host_diagnostic_database_url(
        "postgresql+asyncpg://user:password@postgres:5432/contract_review"
    ) == "postgresql+asyncpg://user:password@127.0.0.1:15432/contract_review"
    external_url = "postgresql+asyncpg://user:password@db.example:5432/contract_review"
    assert host_diagnostic_database_url(external_url) == external_url


def test_real_diagnostic_blocks_calls_after_the_configured_limit(tmp_path) -> None:
    writer = SafeMetricWriter(tmp_path / "limit.jsonl")
    metrics = SafeMetrics(writer=writer, file_names={}, max_logical_calls=1)
    try:
        metrics.begin_logical("FACT_MAPPING", "fil_reference")
        metrics.end_logical()
        with pytest.raises(WorkflowError, match="调用上限"):
            metrics.begin_logical("AI_ADVICE", None)
    finally:
        writer.close()

    assert metrics.logical_calls == 1


def test_safe_aggregate_metrics_keep_only_fact_and_mapping_counts(tmp_path) -> None:
    writer = SafeMetricWriter(tmp_path / "aggregates.jsonl")
    metrics = SafeMetrics(writer=writer, file_names={"fil_reference": "reference.docx"})
    try:
        metrics.record_extraction_value(
            "target.docx",
            {"facts": [{"field_key": "secret_value", "raw_value": "contract text"}]},
        )
        metrics.record_fact_review_value(
            "target.docx",
            {"decisions": [{"field_key": "secret_value", "decision": "ACCEPT"}]},
        )
        metrics.record_mapping_payload(
            "reference.docx",
            {"target_facts": [{"fact_id": "target_fact_000001"}], "reference_facts": []},
        )
        metrics.record_mapping_result(
            "reference.docx",
            {"mappings": [{"target_fact_id": "target_fact_000001"}], "missing_requirements": []},
        )
        metrics.record_mapping_review(
            "reference.docx",
            {
                "proposed_mapping": {
                    "mappings": [{"target_fact_id": "target_fact_000001"}],
                    "missing_requirements": [],
                }
            },
            {"decisions": [{"target_fact_id": "target_fact_000001", "decision": "ACCEPT"}]},
        )
        aggregates = metrics.safe_aggregate_summary()
    finally:
        writer.close()

    assert aggregates["extraction_fact_counts"] == {"target.docx": 1}
    assert aggregates["fact_review_counts"]["target.docx"]["accept"] == 1
    assert aggregates["fact_review_counts"]["target.docx"]["uncovered"] == 0
    assert aggregates["mapping_target_fact_counts"] == {"reference.docx": 1}
    assert aggregates["mapping_proposal_counts"] == {"reference.docx": 1}
    assert aggregates["mapping_review_counts"]["reference.docx"]["accept"] == 1
    serialized = (tmp_path / "aggregates.jsonl").read_text(encoding="utf-8")
    assert "contract text" not in serialized


@pytest.mark.asyncio
async def test_concurrent_recording_keeps_http_calls_with_their_logical_file(
    tmp_path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "{}"}}
                ]
            },
            request=request,
        )

    output_path = tmp_path / "concurrent.jsonl"
    writer = SafeMetricWriter(output_path)
    metrics = SafeMetrics(
        writer=writer,
        file_names={"fil_a": "a.docx", "fil_b": "b.docx"},
    )
    transport = RecordingTransport(
        httpx.MockTransport(handler), metrics, read_timeout=0.1
    )

    async def call(file_id: str) -> None:
        payload = {
            "batch_id": f"batch_{file_id}",
            "planned_batch_count": 1,
            "parent_batch_id": None,
        }
        metrics.begin_logical("FACT_EXTRACTION", file_id, payload)
        try:
            async with httpx.AsyncClient(
                transport=transport, trust_env=False
            ) as client:
                await client.post(
                    "https://llm.example.com/v1/chat/completions",
                    json=payload,
                )
        finally:
            metrics.end_logical(
                result=LlmResult(
                    value={},
                    configured_model="fixture",
                    actual_model="fixture",
                    mock=False,
                )
            )

    try:
        await asyncio.gather(call("fil_a"), call("fil_b"))
    finally:
        await transport.close_all()
        writer.close()

    events = [
        json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    requests = [event for event in events if event["event"] == "http_request_started"]
    finishes = [event for event in events if event["event"] == "logical_call_finished"]
    assert {event["file_name"] for event in requests} == {"a.docx", "b.docx"}
    assert {event["file_name"] for event in finishes} == {"a.docx", "b.docx"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
async def test_structured_probe_validates_actual_synthetic_json_and_emits_no_response(
    mode: str,
) -> None:
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"marker":"synthetic","ok":true}'},
                    }
                ]
            },
        )

    settings = Settings(
        _env_file=None,
        LLM_ENABLED=True,
        LLM_BASE_URL="https://llm.example.com",
        LLM_API_KEY="synthetic-key",
    )
    result = await probe_mode(
        settings,
        mode,
        transport=httpx.MockTransport(handler),
    )

    assert result["json_valid"] is True
    assert result["schema_valid"] is True
    assert result["finish_reason"] == "stop"
    assert "synthetic-key" not in json.dumps(result)
    assert "synthetic" in json.dumps(requests[0])
    assert requests[0]["response_format"]["type"] == mode
