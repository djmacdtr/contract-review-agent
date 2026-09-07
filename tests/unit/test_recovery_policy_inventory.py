import inspect
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from app.adapters.llm.openai_client import OpenAIContractLlmClient
from app.core.config import Settings
from app.core.enums import TaskType
from app.core.errors import WorkflowError
from app.core.reliability import RecoveryBudget
from app.draft_review import extraction
from app.services.downloader import SafeFileDownloadService
from app.worker.runner import WorkerRunner
from app.workflows import draft_review


def test_recovery_policy_inventory_has_explicit_bounded_stages() -> None:
    settings = Settings(_env_file=None)
    assert settings.WORKFLOW_RECOVERY_MAX_EXTRA_SECONDS == 600
    assert settings.LLM_TEXT_RECOVERY_MAX_DEPTH == 4
    assert settings.LLM_EXTRACTION_LEAF_RETRY_ATTEMPTS == 1
    assert settings.DOWNLOAD_RETRY_ATTEMPTS == 2
    assert settings.TASK_TRANSIENT_RESUME_ATTEMPTS == 1
    assert settings.DB_WRITE_RETRY_ATTEMPTS == 1
    assert extraction.TEXT_MAX_RECOVERY_DEPTH == 4
    assert "allow_stage" in inspect.getsource(RecoveryBudget)
    assert "_map_facts_with_recovery" in inspect.getsource(OpenAIContractLlmClient)
    assert "recovery_budget" in inspect.signature(
        SafeFileDownloadService.prepare
    ).parameters
    assert "TASK_TRANSIENT_RESUME_ATTEMPTS" in inspect.getsource(WorkerRunner.process)


def test_draft_review_advice_uses_shared_bounded_generator() -> None:
    source = inspect.getsource(draft_review)

    assert "from app.results.advice_batches import generate_advice_in_batches" in source
    assert "generate_advice_in_batches(" in source
    assert "async def process_batch" not in source
    assert "advice_payload(" not in source


def test_recovery_budget_reports_only_safe_diagnostics() -> None:
    budget = RecoveryBudget(600)
    assert budget.allow_stage("TEXT_LEAF", "LLM_OUTPUT_TRUNCATED")
    budget.record_logical_call("TEXT")
    budget.record_split_depth(4)
    result = budget.as_dict()
    assert result["recovery_stage_counts"] == {"TEXT_LEAF": 1}
    assert result["logical_call_counts"] == {"TEXT": 1}
    assert result["max_split_depth"] == 4
    assert all("payload" not in key.lower() for key in result)


class _SessionContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    def begin(self):
        return self


class _RunnerRepository:
    def __init__(self, *, fail_first_complete: bool = False) -> None:
        self.complete_calls = 0
        self.fail_first_complete = fail_first_complete

    async def heartbeat(self, *_args, **_kwargs) -> bool:
        return True

    async def complete(self, *_args, **_kwargs) -> bool:
        self.complete_calls += 1
        if self.fail_first_complete and self.complete_calls == 1:
            raise OperationalError("write", {}, ConnectionError("temporary"))
        return True

    async def fail(self, *_args, **_kwargs) -> bool:
        return False


class _ResumeWorkflow:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            raise WorkflowError(
                "DYNAMIC_CHECK_INCOMPLETE",
                "模型服务暂时不可用",
                details={"failure_code": "LLM_NETWORK_ERROR"},
            )
        return {
            "schema_version": "2.1",
            "conclusion": "PASS",
            "summary": {"statistics": {"risk_count": 0, "review_count": 0}},
            "metadata": {},
        }


@pytest.mark.asyncio
async def test_worker_resumes_same_task_once_for_transport_failure() -> None:
    workflow = _ResumeWorkflow()
    repository = _RunnerRepository()
    settings = Settings(
        _env_file=None,
        TASK_TRANSIENT_RESUME_ATTEMPTS=1,
        DB_WRITE_RETRY_ATTEMPTS=0,
        WORKER_HEARTBEAT_INTERVAL_SECONDS=3600,
    )
    runner = WorkerRunner(
        settings,
        repository=repository,
        workflow=workflow,
        session_factory=lambda: _SessionContext(),
    )
    task = SimpleNamespace(
        id="tsk_resume",
        task_type=TaskType.DRAFT_REVIEW,
        files=[],
        options={},
        source_task_id=None,
    )

    assert await runner.process(task) is True
    assert workflow.calls == 2
    assert repository.complete_calls == 1


@pytest.mark.asyncio
async def test_worker_retries_one_transient_result_write() -> None:
    workflow = _ResumeWorkflow()
    workflow.calls = 1
    repository = _RunnerRepository(fail_first_complete=True)
    settings = Settings(
        _env_file=None,
        TASK_TRANSIENT_RESUME_ATTEMPTS=0,
        DB_WRITE_RETRY_ATTEMPTS=1,
        WORKER_HEARTBEAT_INTERVAL_SECONDS=3600,
    )
    runner = WorkerRunner(
        settings,
        repository=repository,
        workflow=workflow,
        session_factory=lambda: _SessionContext(),
    )
    task = SimpleNamespace(
        id="tsk_db_retry",
        task_type=TaskType.DRAFT_REVIEW,
        files=[],
        options={},
        source_task_id=None,
    )

    assert await runner.process(task) is True
    assert repository.complete_calls == 2
