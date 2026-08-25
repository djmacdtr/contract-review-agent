import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import TaskFile
from app.db.session import SessionFactory
from app.main import app
from tests.integration.helpers import DRAFT_PAYLOAD, FINAL_PAYLOAD


@pytest.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as value:
        yield value


@pytest.mark.parametrize(
    ("path", "payload", "expected_type"),
    [
        ("/api/v1/draft-reviews", DRAFT_PAYLOAD, "DRAFT_REVIEW"),
        ("/api/v1/final-comparisons", FINAL_PAYLOAD, "FINAL_COMPARE"),
    ],
)
async def test_create_task_returns_202_and_queryable_detail(
    client, path, payload, expected_type
) -> None:
    response = await client.post(path, json=payload)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["code"] == "0"
    assert body["data"]["task_type"] == expected_type
    assert body["data"]["status"] == "PENDING"
    assert response.headers["X-Request-ID"] == body["request_id"]

    task_id = body["data"]["task_id"]
    detail = await client.get(f"/api/v1/tasks/{task_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["stage"] == "QUEUED"

    result = await client.get(f"/api/v1/tasks/{task_id}/result")
    assert result.status_code == 409
    assert result.json()["code"] == "TASK_NOT_FINISHED"


async def test_list_not_found_retry_guard_and_validation_are_stable(client) -> None:
    created = await client.post("/api/v1/final-comparisons", json=FINAL_PAYLOAD)
    task_id = created.json()["data"]["task_id"]
    listed = await client.get("/api/v1/tasks?page=1&page_size=20&status=PENDING")
    assert listed.status_code == 200
    assert listed.json()["data"]["total"] == 1
    assert listed.json()["data"]["items"][0]["task_id"] == task_id
    summary = listed.json()["data"]["items"][0]
    assert {"risk_count", "review_count", "legacy_statistics"} <= summary.keys()
    assert "high_risk_count" not in summary

    missing = await client.get("/api/v1/tasks/tsk_missing")
    assert missing.status_code == 404 and missing.json()["code"] == "TASK_NOT_FOUND"
    guarded = await client.post(f"/api/v1/tasks/{task_id}/retry")
    assert guarded.status_code == 409 and guarded.json()["code"] == "TASK_NOT_RETRYABLE"

    invalid = await client.post(
        "/api/v1/draft-reviews",
        json={**DRAFT_PAYLOAD, "reference_files": []},
    )
    assert invalid.status_code == 400
    text = invalid.text
    assert invalid.json()["code"] == "INVALID_REQUEST"
    assert "target-secret" not in text and "review-secret" not in text


async def test_openapi_contains_required_endpoints_and_descriptions(client) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    for path in (
        "/api/v1/draft-reviews",
        "/api/v1/final-comparisons",
        "/api/v1/tasks",
        "/api/v1/tasks/{task_id}",
        "/api/v1/tasks/{task_id}/result",
        "/api/v1/tasks/{task_id}/retry",
        "/health",
        "/ready",
    ):
        assert path in paths


async def test_draft_legacy_reference_type_is_accepted_but_ignored(client) -> None:
    payload = {
        **DRAFT_PAYLOAD,
        "reference_files": [
            {**DRAFT_PAYLOAD["reference_files"][0], "reference_type": "REVIEW_OPINION"}
        ],
    }
    response = await client.post("/api/v1/draft-reviews", json=payload)
    assert response.status_code == 202
    task_id = response.json()["data"]["task_id"]

    async with SessionFactory() as session:
        files = (
            await session.execute(select(TaskFile).where(TaskFile.task_id == task_id))
        ).scalars().all()
    reference = next(item for item in files if item.role.value == "REFERENCE")
    assert reference.reference_type is None


async def test_draft_reference_limit_uses_runtime_configuration(client) -> None:
    limited = get_settings().model_copy(update={"MAX_REFERENCE_FILES": 2})
    app.dependency_overrides[get_settings] = lambda: limited
    try:
        payload = {
            **DRAFT_PAYLOAD,
            "reference_files": [DRAFT_PAYLOAD["reference_files"][0]] * 3,
        }
        response = await client.post("/api/v1/draft-reviews", json=payload)
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"
    assert response.json()["error"]["details"] == {
        "max_reference_files": 2,
        "actual_reference_files": 3,
    }


async def test_draft_openapi_example_omits_reference_type(client) -> None:
    schema = (await client.get("/openapi.json")).json()
    example = schema["components"]["schemas"]["DraftReviewCreate"]["example"]
    assert "reference_type" not in example["reference_files"][0]
    assert "severity" not in schema["components"]["schemas"]["DiffItem"]["properties"]
    diff_schema = schema["components"]["schemas"]["DiffItem"]
    assert {"certainty", "missing_detail"} <= diff_schema["properties"].keys()
    assert {"PAGE_MISSING", "CONTENT_BLOCK_MISSING"} <= set(
        diff_schema["properties"]["diff_type"]["enum"]
    )
    statistics = schema["components"]["schemas"]["ResultStatistics"]["properties"]
    assert {"risk_count", "review_count", "passed_check_count"} <= statistics.keys()
