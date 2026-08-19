import pytest

from app.core.config import Settings
from app.core.enums import TaskStage, TaskType
from app.schemas.results import TaskResultData
from app.workflows.mock_graphs import MockWorkflowExecutor


def settings() -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://x:x@db/test",
        MOCK_STAGE_DELAY_SECONDS=0,
    )


def files(task_type: TaskType) -> list[dict]:
    roles = ["TARGET", "TEMPLATE", "REFERENCE"] if task_type == TaskType.DRAFT_REVIEW else ["BASELINE", "TARGET"]
    return [
        {"file_id": f"fil_{i}", "role": role, "file_name": f"file-{i}.docx", "safe_url": f"https://files.example.com/{i}.docx"}
        for i, role in enumerate(roles)
    ]


@pytest.mark.parametrize("task_type", [TaskType.DRAFT_REVIEW, TaskType.FINAL_COMPARE])
async def test_mock_graph_success_has_explicit_mock_result(task_type: TaskType) -> None:
    updates: list[tuple[TaskStage, int]] = []

    async def progress(stage: TaskStage, value: int, message: str) -> None:
        updates.append((stage, value))

    executor = MockWorkflowExecutor(settings())
    result = await executor.run(
        task_id="tsk_01KTESTMOCKRESULT0000000",
        task_type=task_type,
        files=files(task_type),
        progress_callback=progress,
    )
    validated = TaskResultData.model_validate(result)
    assert validated.mock is True
    assert result["metadata"]["execution_mode"] == "MOCK"
    assert result["metadata"]["model_runs"][0]["actual_model"] is None
    assert result["warnings"][0]["code"] == "MOCK_RESULT"
    assert updates[-1][0] == TaskStage.PERSISTING_RESULT
    for check in result["rule_checks"]:
        for key in ("expected", "actual", "tolerance"):
            assert isinstance(check[key], str)


async def test_mock_graph_failure_is_injectable_without_public_api_backdoor() -> None:
    async def progress(stage: TaskStage, value: int, message: str) -> None:
        return None

    executor = MockWorkflowExecutor(settings(), fail_stage=TaskStage.PARSING)
    with pytest.raises(RuntimeError, match="injected mock failure"):
        await executor.run(
            task_id="tsk_01KTESTFAIL000000000000",
            task_type=TaskType.DRAFT_REVIEW,
            files=files(TaskType.DRAFT_REVIEW),
            progress_callback=progress,
        )

