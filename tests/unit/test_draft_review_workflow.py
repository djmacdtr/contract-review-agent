from pathlib import Path

import httpx
from docx import Document

from app.core.config import Settings
from app.core.enums import TaskStage, TaskType
from app.services.downloader import SafeFileDownloadService
from app.workflows.draft_review import DraftReviewWorkflowExecutor


def build_docx(path: Path, title: str, body: str) -> bytes:
    document = Document()
    document.add_heading(title, level=1)
    document.add_paragraph(body)
    document.save(path)
    return path.read_bytes()


async def resolver(host: str, port: int) -> list[str]:
    return ["127.0.0.1"]


async def test_draft_review_downloads_and_parses_every_file_without_mocking(
    tmp_path: Path,
) -> None:
    bodies = {
        "/target.docx": build_docx(
            tmp_path / "target.docx", "融资租赁合同", "第一条 融资金额为1000万元。"
        ),
        "/template.docx": build_docx(
            tmp_path / "template.docx", "融资租赁合同", "第一条 融资金额为##{融资金额}万元。"
        ),
        "/reference.docx": build_docx(
            tmp_path / "reference.docx", "任意辅助资料", "仅参与本阶段解析。"
        ),
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bodies[request.url.path], request=request)

    settings = Settings(
        _env_file=None,
        TEMP_ROOT=str(tmp_path / "workspaces"),
        ALLOW_HTTP_DOWNLOADS=True,
        DOWNLOAD_HOST_ALLOWLIST="fixture-server",
    )
    executor = DraftReviewWorkflowExecutor(
        settings,
        downloader=SafeFileDownloadService(
            settings,
            transport=httpx.MockTransport(handler),
            resolver=resolver,
        ),
    )
    updates: list[tuple[TaskStage, int]] = []

    async def progress(stage: TaskStage, value: int, message: str) -> None:
        updates.append((stage, value))

    output = await executor.run(
        task_id="tsk_draft_parse",
        task_type=TaskType.DRAFT_REVIEW,
        files=[
            {
                "file_id": "fil_target",
                "role": "TARGET",
                "file_name": "target.docx",
                "url": "http://fixture-server/target.docx?token=secret",
                "safe_url": "http://fixture-server/target.docx",
            },
            {
                "file_id": "fil_template",
                "role": "TEMPLATE",
                "file_name": "template.docx",
                "url": "http://fixture-server/template.docx?token=secret",
                "safe_url": "http://fixture-server/template.docx",
            },
            {
                "file_id": "fil_reference",
                "role": "REFERENCE",
                "file_name": "reference.docx",
                "url": "http://fixture-server/reference.docx?token=secret",
                "safe_url": "http://fixture-server/reference.docx",
            },
        ],
        options={},
        progress_callback=progress,
    )

    result = output.result
    assert result["mock"] is False
    assert result["metadata"]["execution_mode"] == "RULE_BASED"
    assert result["schema_version"] == "2.0"
    assert result["metadata"]["workflow_version"] == "0.3.1"
    assert result["metadata"]["primary_model"] is None
    assert result["conclusion"] == "PASS"
    assert result["diff_items"] == []
    assert result["rule_checks"] == []
    assert result["metadata"]["template_diagnostics"]["filtered_diff_count"] == 1
    assert len({item["code"] for item in result["warnings"]}) == len(result["warnings"])
    assert len(result["files"]) == 3
    assert all(item["parser_name"] == "python-docx" for item in result["files"])
    assert all(item["document_profile"]["document_kind"] == "UNKNOWN" for item in result["files"])
    assert all(item["content_structure"]["block_count"] == 2 for item in result["files"])
    assert result["files"][0]["content_structure"]["sample_locations"]
    assert result["warnings"][-1]["code"] == "DRAFT_REVIEW_RULE_BASED_LIMITATION"
    assert "token=secret" not in str(result)
    assert updates[-1][0] == TaskStage.PERSISTING_RESULT
    assert any(stage == TaskStage.TEMPLATE_COMPARE for stage, _value in updates)
    assert not any((tmp_path / "workspaces").iterdir())
    assert len(output.file_metadata) == 3
