from __future__ import annotations

import pytest

from app.adapters.llm.base import LlmResult
from app.core.config import Settings
from app.core.errors import WorkflowError
from app.documents.models import DocumentBlock, DocumentLocation, ParsedDocument
from app.draft_review.extraction import extract_documents_with_independent_map_reduce


def budget_document() -> ParsedDocument:
    block = DocumentBlock(
        block_id="budget_block",
        type="PARAGRAPH",
        order=0,
        raw_text="租赁金额为100万元。",
        normalized_text="租赁金额为100万元。",
        location=DocumentLocation(paragraph_index=0),
    )
    return ParsedDocument(
        file_id="budget_file",
        role="REFERENCE",
        file_name="budget.docx",
        sha256="b" * 64,
        page_count=None,
        parser_name="fixture",
        blocks=[block],
    )


class NeverCalled:
    profile_calls = 0
    numeric_calls = 0
    text_calls = 0

    async def extract_document_profile(self, payload: dict) -> LlmResult:
        self.profile_calls += 1
        raise AssertionError("profile must not run after a zero-call preflight failure")

    async def extract_numeric_candidates(self, payload: dict) -> LlmResult:
        self.numeric_calls += 1
        raise AssertionError("numeric must not run after a zero-call preflight failure")

    async def extract_text_facts(self, payload: dict) -> LlmResult:
        self.text_calls += 1
        raise AssertionError("text must not run after a zero-call preflight failure")


@pytest.mark.asyncio
async def test_call_budget_preflight_fails_before_any_llm_call() -> None:
    llm = NeverCalled()
    with pytest.raises(WorkflowError) as caught:
        await extract_documents_with_independent_map_reduce(
            settings=Settings(
                _env_file=None,
                LLM_EXTRACTION_MAX_LOGICAL_CALLS_TOTAL=1,
            ),
            documents=[budget_document()],
            llm=llm,  # type: ignore[arg-type]
        )

    assert caught.value.details["failure_code"] == "EXTRACTION_CALL_BUDGET_EXHAUSTED"
    assert caught.value.details["total_cache_miss_count"] == 3
    assert llm.profile_calls == llm.numeric_calls == llm.text_calls == 0


@pytest.mark.asyncio
async def test_per_document_budget_preflight_fails_before_any_llm_call() -> None:
    llm = NeverCalled()
    with pytest.raises(WorkflowError) as caught:
        await extract_documents_with_independent_map_reduce(
            settings=Settings(
                _env_file=None,
                LLM_EXTRACTION_ABSOLUTE_MAX_REQUESTS_PER_DOCUMENT=1,
            ),
            documents=[budget_document()],
            llm=llm,  # type: ignore[arg-type]
        )

    assert caught.value.details["failure_code"] == "EXTRACTION_CALL_BUDGET_EXHAUSTED"
    assert caught.value.details["max_document_cache_miss_count"] == 3
    assert llm.profile_calls == llm.numeric_calls == llm.text_calls == 0


def test_total_call_budget_default_is_256() -> None:
    assert Settings(_env_file=None).LLM_EXTRACTION_MAX_LOGICAL_CALLS_TOTAL == 256
