import json
from pathlib import Path

import httpx
import pytest

from app.adapters.llm.openai_client import LlmClientError, OpenAIContractLlmClient
from app.core.config import Settings
from app.core.enums import TaskStage
from app.core.errors import WorkflowError
from app.core.reliability import RecoveryBudget
from app.documents.models import ParsedDocument
from app.documents.page_locations import DocxPageLocationSidecar
from app.draft_review.checkpoints import mapping_checkpoint_identity
from app.services.downloader import DOCX_MIME, PDF_MIME, LocalFile
from app.workflows.page_recovery import (
    enrich_result_with_page_recovery,
    page_free_result_copy,
    recover_missing_page_sidecars,
)


async def no_sleep(_delay: float) -> None:
    return None


def llm_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "LLM_ENABLED": True,
        "LLM_BASE_URL": "https://llm.example.test/v1",
        "LLM_API_KEY": "unit-key",
        "LLM_HTTP_RETRY_ATTEMPTS": 0,
        "LLM_STRUCTURE_RETRY_ATTEMPTS": 0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


async def test_network_request_error_retries_twice_then_succeeds() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("temporary network failure", request=request)
        return httpx.Response(
            200,
            json={"data": [{"id": "model-a"}]},
            request=request,
        )

    client = OpenAIContractLlmClient(
        llm_settings(LLM_NETWORK_RETRY_ATTEMPTS=2),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )
    budget = RecoveryBudget(600)
    client.set_recovery_budget(budget)

    assert await client.probe_models() == ["model-a"]
    assert calls == 3
    assert budget.as_dict()["http_request_attempts"] == 3


async def test_network_request_error_stops_after_three_total_attempts() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("persistent network failure", request=request)

    client = OpenAIContractLlmClient(
        llm_settings(LLM_NETWORK_RETRY_ATTEMPTS=2),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    with pytest.raises(LlmClientError) as raised:
        await client.probe_models()

    assert raised.value.code == "LLM_NETWORK_ERROR"
    assert raised.value.request_attempts == 3
    assert calls == 3


def mapping_payload() -> dict[str, object]:
    return {
        "reference_file_id": "fil_reference",
        "target_facts": [],
        "reference_facts": [],
    }


def mapping_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "mapping-model",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": content},
                }
            ],
        },
    )


async def test_mapping_model_output_recovery_has_two_logical_calls() -> None:
    calls = 0
    valid = json.dumps(
        {"reference_file_id": "fil_reference", "mappings": [], "missing_requirements": []}
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return mapping_response("not-json" if calls == 1 else valid)

    client = OpenAIContractLlmClient(
        llm_settings(LLM_STRUCTURE_RETRY_ATTEMPTS=0, LLM_MAPPING_RECOVERY_ATTEMPTS=1),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    result = await client.map_facts(mapping_payload())

    assert result.value["reference_file_id"] == "fil_reference"
    assert result.response_metadata["mapping_recovery_attempts"] == 1
    assert calls == 2


async def test_mapping_model_output_recovery_never_makes_a_third_logical_call() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return mapping_response("not-json")

    client = OpenAIContractLlmClient(
        llm_settings(LLM_STRUCTURE_RETRY_ATTEMPTS=0, LLM_MAPPING_RECOVERY_ATTEMPTS=1),
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
    )

    with pytest.raises(LlmClientError) as raised:
        await client.map_facts(mapping_payload())

    assert raised.value.code == "LLM_INVALID_JSON"
    assert raised.value.mapping_recovery_attempted is True
    assert calls == 2


def test_recovery_budget_uses_monotonic_deadline() -> None:
    now = [10.0]
    budget = RecoveryBudget(2.0, clock=lambda: now[0])

    assert budget.allow("LLM_NETWORK_ERROR") is True
    now[0] = 11.5
    assert budget.remaining_seconds == 0.5
    assert budget.allow("LLM_NETWORK_ERROR") is True
    now[0] = 12.1
    assert budget.allow("LLM_NETWORK_ERROR") is False
    assert budget.exhausted is True
    assert budget.as_dict()["recovery_attempts"] == 2


def test_mapping_checkpoint_identity_excludes_local_ids_and_physical_pages() -> None:
    payload = {
        "reference_file_id": "fil_reference_a",
        "target_facts": [
            {
                "target_fact_id": "target_fact_000001",
                "source_file_id": "fil_target_a",
                "location": {"paragraph_index": 1, "page": 2},
            }
        ],
        "reference_facts": [
            {
                "source_file_id": "fil_reference_a",
                "location": {"paragraph_index": 3, "page": 7},
            }
        ],
    }
    same_content_other_task = json.loads(json.dumps(payload))
    same_content_other_task["reference_file_id"] = "fil_reference_b"
    same_content_other_task["target_facts"][0]["source_file_id"] = "fil_target_b"
    same_content_other_task["target_facts"][0]["location"]["page"] = 9
    same_content_other_task["reference_facts"][0]["source_file_id"] = "fil_reference_b"
    same_content_other_task["reference_facts"][0]["location"]["page"] = 11

    first = mapping_checkpoint_identity(
        target_sha256="t" * 64,
        reference_sha256="r" * 64,
        model_name="model-a",
        payload=payload,
        rules_version="rules-v1",
    )
    second = mapping_checkpoint_identity(
        target_sha256="t" * 64,
        reference_sha256="r" * 64,
        model_name="model-a",
        payload=same_content_other_task,
        rules_version="rules-v1",
    )
    assert first == second


class RebuildingRouter:
    def __init__(self, sidecar: DocxPageLocationSidecar) -> None:
        self.page_location_sidecars = {"fil_target": sidecar}
        self.calls: list[bool] = []
        self.commits = 0

    async def rebuild_docx_page_location(
        self, file: LocalFile, *, refresh_ocr: bool, persist: bool
    ) -> ParsedDocument:
        self.calls.append(refresh_ocr)
        self.page_location_sidecars["fil_target"] = DocxPageLocationSidecar(
            file_id=file.file_id,
            page_count=1,
            mappings={(0, None, None, None): (1,)},
            required_location_count=1,
            candidate_mapping_count=1,
            local_structure_count=1,
            external_structure_count=1,
            external_detail_page_count=1,
        )
        return ParsedDocument(
            file_id=file.file_id,
            role=file.role,
            file_name=file.file_name,
            sha256=file.sha256,
            page_count=None,
            blocks=[],
            parser_name="test",
        )

    async def commit_docx_page_location(self, _file: LocalFile) -> None:
        self.commits += 1


class RefreshingRouter(RebuildingRouter):
    async def rebuild_docx_page_location(
        self, file: LocalFile, *, refresh_ocr: bool, persist: bool
    ) -> ParsedDocument:
        self.calls.append(refresh_ocr)
        self.page_location_sidecars["fil_target"] = DocxPageLocationSidecar(
            file_id=file.file_id,
            page_count=1,
            mappings={(0, None, None, None): (1,)} if refresh_ocr else {},
            required_location_count=1,
            candidate_mapping_count=1 if refresh_ocr else 0,
            local_structure_count=1,
            external_structure_count=1 if refresh_ocr else 0,
            external_detail_page_count=1 if refresh_ocr else 0,
        )
        return ParsedDocument(
            file_id=file.file_id,
            role=file.role,
            file_name=file.file_name,
            sha256=file.sha256,
            page_count=None,
            blocks=[],
            parser_name="test",
        )


def test_page_free_copy_preserves_pdf_pages_and_strips_only_docx_locations() -> None:
    result = {
        "diff_items": [
            {
                "diff_id": "diff_1",
                "baseline": {
                    "file_id": "fil_pdf",
                    "location": {"table_index": 0, "row": 2, "page": 4},
                },
                "target": {
                    "file_id": "fil_docx",
                    "location": {
                        "table_index": 0,
                        "row": 2,
                        "page": 9,
                        "target_anchor_before_page": 8,
                    },
                },
            }
        ],
        "risk_items": [
            {
                "related_diff_ids": ["diff_1"],
                "source_evidence": [
                    {
                        "file_id": "fil_pdf",
                        "location": {"table_index": 0, "row": 2, "page": 4},
                    }
                ],
            }
        ],
    }

    copied = page_free_result_copy(result, docx_file_ids={"fil_docx"})

    assert copied["diff_items"][0]["baseline"]["location"]["page"] == 4
    assert copied["diff_items"][0]["target"]["location"] == {
        "table_index": 0,
        "row": 2,
    }
    assert copied["risk_items"][0]["source_evidence"][0]["location"]["page"] == 4
    assert result["diff_items"][0]["target"]["location"]["page"] == 9


class NoPageRecoveryRouter:
    page_location_sidecars: dict[str, DocxPageLocationSidecar] = {}

    def __init__(self) -> None:
        self.calls = 0

    async def rebuild_docx_page_location(self, *_args, **_kwargs) -> ParsedDocument:
        self.calls += 1
        raise AssertionError("PDF page failure must not trigger DOCX recovery")


async def test_pdf_missing_page_fails_without_docx_recovery_or_ocr(tmp_path: Path) -> None:
    path = tmp_path / "review.pdf"
    path.write_bytes(b"pdf")
    local_file = LocalFile(
        file_id="fil_pdf",
        role="REFERENCE",
        file_name=path.name,
        safe_url="http://fixture/review.pdf",
        path=path,
        file_size=path.stat().st_size,
        sha256="p" * 64,
        detected_mime_type=PDF_MIME,
    )
    router = NoPageRecoveryRouter()

    with pytest.raises(WorkflowError) as raised:
        await enrich_result_with_page_recovery(
            {
                "files": [{"file_id": "fil_pdf", "page_count": 1}],
                "diff_items": [
                    {
                        "diff_id": "diff_pdf",
                        "target": {
                            "file_id": "fil_pdf",
                            "location": {"table_index": 0, "row": 2},
                        },
                    }
                ],
                "risk_items": [],
            },
            documents=[],
            local_files=[local_file],
            router=router,
            sidecars={},
            budget=RecoveryBudget(600),
            ocr_refresh_attempts=1,
        )

    assert raised.value.code == "DOCX_PAGE_LOCATION_INCOMPLETE"
    assert raised.value.details["failure_code"] == "PUBLIC_DIFF_PAGE_MISSING"
    assert raised.value.details["public_evidence_file_id"] == "fil_pdf"
    assert raised.value.details["public_evidence_location"] == {
        "table_index": 0,
        "row": 2,
    }
    assert router.calls == 0


async def test_page_recovery_rebuilds_from_page_free_result(tmp_path: Path) -> None:
    path = tmp_path / "target.docx"
    path.write_bytes(b"docx")
    local_file = LocalFile(
        file_id="fil_target",
        role="TARGET",
        file_name=path.name,
        safe_url="http://fixture/target.docx",
        path=path,
        file_size=path.stat().st_size,
        sha256="t" * 64,
        detected_mime_type=DOCX_MIME,
    )
    old_sidecar = DocxPageLocationSidecar(
        file_id="fil_target",
        page_count=1,
        mappings={},
        required_location_count=1,
        candidate_mapping_count=0,
        local_structure_count=1,
        external_structure_count=0,
        external_detail_page_count=0,
    )
    router = RebuildingRouter(old_sidecar)
    updates: list[tuple[TaskStage | None, int, str]] = []

    async def progress(stage: TaskStage | None, value: int, message: str) -> None:
        updates.append((stage, value, message))

    result = {
        "files": [{"file_id": "fil_target", "page_count": 1}],
        "diff_items": [
            {
                "diff_id": "diff_1",
                "target": {
                    "file_id": "fil_target",
                    "text": "变化",
                    "location": {
                        "paragraph_index": 0,
                        "page": 99,
                    },
                },
            }
        ],
        "risk_items": [],
    }

    recovered = await enrich_result_with_page_recovery(
        result,
        documents=[],
        local_files=[local_file],
        router=router,
        sidecars=router.page_location_sidecars,
        budget=RecoveryBudget(600),
        ocr_refresh_attempts=1,
        progress_callback=progress,
        progress_stage=TaskStage.PERSISTING_RESULT,
    )

    assert recovered.result["diff_items"][0]["target"]["location"]["page"] == 1
    assert result["diff_items"][0]["target"]["location"]["page"] == 99
    assert router.calls == [False]
    assert router.commits == 1
    assert updates == [
        (TaskStage.PERSISTING_RESULT, 95, "正在校验并补全公开证据页码")
    ]


async def test_page_recovery_budget_exhaustion_skips_external_recovery(tmp_path: Path) -> None:
    path = tmp_path / "target.docx"
    path.write_bytes(b"docx")
    local_file = LocalFile(
        file_id="fil_target",
        role="TARGET",
        file_name=path.name,
        safe_url="http://fixture/target.docx",
        path=path,
        file_size=path.stat().st_size,
        sha256="t" * 64,
        detected_mime_type=DOCX_MIME,
    )
    router = RebuildingRouter(
        DocxPageLocationSidecar(
            file_id="fil_target",
            page_count=1,
            mappings={},
            required_location_count=1,
            candidate_mapping_count=0,
            local_structure_count=1,
            external_structure_count=0,
            external_detail_page_count=0,
        )
    )

    with pytest.raises(WorkflowError):
        await enrich_result_with_page_recovery(
            {
                "files": [{"file_id": "fil_target", "page_count": 1}],
                "diff_items": [
                    {
                        "target": {
                            "file_id": "fil_target",
                            "location": {"paragraph_index": 0},
                        }
                    }
                ],
            },
            documents=[],
            local_files=[local_file],
            router=router,
            sidecars=router.page_location_sidecars,
            budget=RecoveryBudget(0),
            ocr_refresh_attempts=1,
        )

    assert router.calls == []


async def test_missing_sidecar_is_rebuilt_before_workflow_logic(tmp_path: Path) -> None:
    path = tmp_path / "target.docx"
    path.write_bytes(b"docx")
    local_file = LocalFile(
        file_id="fil_target",
        role="TARGET",
        file_name=path.name,
        safe_url="http://fixture/target.docx",
        path=path,
        file_size=path.stat().st_size,
        sha256="t" * 64,
        detected_mime_type=DOCX_MIME,
    )
    router = RebuildingRouter(
        DocxPageLocationSidecar(
            file_id="fil_target",
            page_count=1,
            mappings={},
            required_location_count=0,
            candidate_mapping_count=0,
            local_structure_count=0,
            external_structure_count=0,
            external_detail_page_count=0,
        )
    )
    router.page_location_sidecars = {}

    recovered = await recover_missing_page_sidecars(
        documents=[],
        local_files=[local_file],
        router=router,
        sidecars={},
        budget=RecoveryBudget(600),
        ocr_refresh_attempts=1,
    )

    assert recovered.sidecars["fil_target"].page_count == 1
    assert router.calls == [False]


async def test_page_recovery_refreshes_ocr_once_when_cached_sidecar_is_insufficient(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target.docx"
    path.write_bytes(b"docx")
    local_file = LocalFile(
        file_id="fil_target",
        role="TARGET",
        file_name=path.name,
        safe_url="http://fixture/target.docx",
        path=path,
        file_size=path.stat().st_size,
        sha256="t" * 64,
        detected_mime_type=DOCX_MIME,
    )
    old_sidecar = DocxPageLocationSidecar(
        file_id="fil_target",
        page_count=1,
        mappings={},
        required_location_count=1,
        candidate_mapping_count=0,
        local_structure_count=1,
        external_structure_count=0,
        external_detail_page_count=0,
    )
    router = RefreshingRouter(old_sidecar)
    result = {
        "files": [{"file_id": "fil_target", "page_count": 1}],
        "diff_items": [
            {
                "target": {
                    "file_id": "fil_target",
                    "location": {"paragraph_index": 0},
                }
            }
        ]
    }
    budget = RecoveryBudget(600)

    recovered = await enrich_result_with_page_recovery(
        result,
        documents=[],
        local_files=[local_file],
        router=router,
        sidecars=router.page_location_sidecars,
        budget=budget,
        ocr_refresh_attempts=1,
    )

    assert recovered.result["diff_items"][0]["target"]["location"]["page"] == 1
    assert router.calls == [False, True]
    assert router.commits == 1
    assert budget.stats.page_sidecar_rebuild_attempts == 1
    assert budget.stats.ocr_refresh_attempts == 1
