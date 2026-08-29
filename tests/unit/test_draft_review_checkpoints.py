import hashlib
import json

import pytest

from app.adapters.llm.base import LlmResult
from app.core.config import Settings
from app.documents.models import DocumentBlock, DocumentLocation, ParsedDocument
from app.draft_review.checkpoints import (
    ExtractionCheckpoint,
    InMemoryExtractionCheckpointStore,
)
from app.draft_review.extraction import extract_documents_with_map_reduce
from app.workflows.draft_review import (
    PRE_PAGE_RESULT_SNAPSHOT_VERSION,
    DraftReviewWorkflowExecutor,
)


@pytest.mark.asyncio
async def test_checkpoint_write_is_idempotent_and_recovery_read_is_stable() -> None:
    store = InMemoryExtractionCheckpointStore()
    checkpoint = ExtractionCheckpoint(
        batch_id="batch_stable",
        payload_digest="digest_1",
        status="SUCCEEDED",
        value={"facts": []},
    )

    await store.save(checkpoint)
    await store.save(checkpoint)

    assert await store.load("batch_stable") == checkpoint

    with pytest.raises(ValueError, match="different result"):
        await store.save(
            ExtractionCheckpoint(
                batch_id="batch_stable",
                payload_digest="digest_2",
                status="SUCCEEDED",
                value={"facts": []},
            )
        )


@pytest.mark.asyncio
async def test_checkpoint_recovery_prefers_current_task_and_matches_payload() -> None:
    store = InMemoryExtractionCheckpointStore()
    source = ExtractionCheckpoint(
        task_id="source_task",
        batch_id="batch_shared",
        file_sha256="f" * 64,
        extraction_version="text-v4",
        payload_digest="source_digest",
        status="SUCCEEDED",
        value={"owner": "source"},
    )
    current = ExtractionCheckpoint(
        task_id="current_task",
        batch_id="batch_shared",
        file_sha256="f" * 64,
        extraction_version="text-v4",
        payload_digest="current_digest",
        status="SUCCEEDED",
        value={"owner": "current"},
    )
    await store.save(source)
    await store.save(current)

    recovered = await store.load(
        "batch_shared",
        task_id="current_task",
        source_task_id="source_task",
        file_sha256="f" * 64,
        extraction_version="text-v4",
        payload_digest="current_digest",
    )
    assert recovered == current

    source_recovered = await store.load(
        "batch_shared",
        task_id="current_task",
        source_task_id="source_task",
        file_sha256="f" * 64,
        extraction_version="text-v4",
        payload_digest="source_digest",
    )
    assert source_recovered == source


class CheckpointFixtureLlm:
    def __init__(self, *, fail_batch: bool = False) -> None:
        self.fail_batch = fail_batch
        self.profile_calls = 0
        self.batch_calls = 0

    async def extract_document_profile(self, payload: dict) -> LlmResult:
        self.profile_calls += 1
        return LlmResult(
            value={
                "document_kind": "资料",
                "title": None,
                "confidence": 0.9,
                "evidence_locations": [payload["overview_blocks"][0]["location"]],
            },
            configured_model="checkpoint-fixture",
            actual_model="checkpoint-fixture",
            mock=False,
        )

    async def extract_fact_batch(self, payload: dict) -> LlmResult:
        self.batch_calls += 1
        if self.fail_batch:
            raise AssertionError("checkpoint should have avoided the model call")
        return LlmResult(
            value={
                "facts": [],
                "numeric_candidate_decisions": [
                    {
                        "candidate_index": candidate["candidate_index"],
                        "decision": "IGNORE",
                        "reason_code": "FIXTURE_IGNORE",
                    }
                    for candidate in payload["numeric_candidates"]
                ],
            },
            configured_model="checkpoint-fixture",
            actual_model="checkpoint-fixture",
            mock=False,
        )


@pytest.mark.asyncio
async def test_map_reduce_reuses_succeeded_batch_checkpoint() -> None:
    document = ParsedDocument(
        file_id="fil_checkpoint",
        role="REFERENCE",
        file_name="reference.docx",
        sha256="a" * 64,
        page_count=None,
        parser_name="fixture",
        blocks=[
            DocumentBlock(
                block_id="block_1",
                type="PARAGRAPH",
                order=0,
                raw_text="融资金额为100万元",
                normalized_text="融资金额为100万元",
                location=DocumentLocation(paragraph_index=0),
            )
        ],
    )
    settings = Settings(_env_file=None, LLM_ENABLED=False)
    store = InMemoryExtractionCheckpointStore()
    first_llm = CheckpointFixtureLlm()

    await extract_documents_with_map_reduce(
        settings=settings,
        documents=[document],
        llm=first_llm,  # type: ignore[arg-type]
        checkpoint_store=store,
    )
    second_llm = CheckpointFixtureLlm(fail_batch=True)
    await extract_documents_with_map_reduce(
        settings=settings,
        documents=[document],
        llm=second_llm,  # type: ignore[arg-type]
        checkpoint_store=store,
    )

    assert first_llm.batch_calls == 1
    assert second_llm.batch_calls == 0


@pytest.mark.asyncio
async def test_pre_page_result_snapshot_is_content_addressed_and_page_free() -> None:
    store = InMemoryExtractionCheckpointStore()
    executor = DraftReviewWorkflowExecutor(
        Settings(_env_file=None),
        checkpoint_store=store,
    )
    result = {
        "files": [{"file_id": "fil_page_snapshot"}],
        "risk_items": [],
        "diff_items": [],
    }
    document = ParsedDocument(
        file_id="fil_page_snapshot",
        role="TARGET",
        file_name="target.docx",
        sha256="a" * 64,
        page_count=4,
        parser_name="fixture",
        blocks=[],
    )

    await executor._save_pre_page_result_snapshot(
        task_id="task_page_snapshot",
        result=result,
        documents=[document],
    )

    identity = {
        "version": PRE_PAGE_RESULT_SNAPSHOT_VERSION,
        "file_sha256": "a" * 64,
        "result": result,
    }
    digest = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    checkpoint = await store.load(
        f"page_result_{digest[:32]}",
        task_id="task_page_snapshot",
        file_sha256="a" * 64,
        extraction_version=PRE_PAGE_RESULT_SNAPSHOT_VERSION,
        payload_digest=digest,
    )

    assert checkpoint is not None
    assert checkpoint.value == {
        "file_id": "fil_page_snapshot",
        "result": result,
    }
    result["files"][0]["page_count"] = 99
    assert checkpoint.value["result"]["files"][0].get("page_count") is None
