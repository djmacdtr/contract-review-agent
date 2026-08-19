from pathlib import Path

import httpx
from docx import Document

from app.core.config import Settings
from app.core.enums import TaskStage, TaskType
from app.services.downloader import SafeFileDownloadService
from app.workflows.final_compare import FinalCompareWorkflowExecutor


def build_docx(path: Path, amount: str) -> bytes:
    document = Document()
    document.add_heading("第一条 合同金额", level=1)
    document.add_paragraph(f"本合同金额为{amount}万元，期限为24个月。")
    document.save(path)
    return path.read_bytes()


async def resolver(host: str, port: int) -> list[str]:
    return ["127.0.0.1"]


async def test_final_compare_graph_returns_rule_based_traceable_result_and_cleans(tmp_path: Path) -> None:
    baseline_bytes = build_docx(tmp_path / "baseline.docx", "100")
    target_bytes = build_docx(tmp_path / "target.docx", "120")

    async def handler(request: httpx.Request) -> httpx.Response:
        body = baseline_bytes if request.url.path.endswith("baseline.docx") else target_bytes
        return httpx.Response(200, content=body, request=request)

    settings = Settings(
        _env_file=None,
        TEMP_ROOT=str(tmp_path / "workspaces"),
        ALLOW_HTTP_DOWNLOADS=True,
        DOWNLOAD_HOST_ALLOWLIST="fixture-server",
        MOCK_STAGE_DELAY_SECONDS=0,
    )
    downloader = SafeFileDownloadService(
        settings,
        transport=httpx.MockTransport(handler),
        resolver=resolver,
    )
    executor = FinalCompareWorkflowExecutor(settings, downloader=downloader)
    updates: list[tuple[TaskStage, int]] = []

    async def progress(stage: TaskStage, value: int, message: str) -> None:
        updates.append((stage, value))

    output = await executor.run(
        task_id="tsk_real_compare",
        task_type=TaskType.FINAL_COMPARE,
        files=[
            {
                "file_id": "fil_base",
                "role": "BASELINE",
                "file_name": "baseline.docx",
                "url": "http://fixture-server/baseline.docx?token=secret",
                "safe_url": "http://fixture-server/baseline.docx",
            },
            {
                "file_id": "fil_target",
                "role": "TARGET",
                "file_name": "target.docx",
                "url": "http://fixture-server/target.docx?token=secret",
                "safe_url": "http://fixture-server/target.docx",
            },
        ],
        options={"numeric_sensitive": True, "ignore_headers_footers": True},
        progress_callback=progress,
    )
    result = output.result
    assert result["mock"] is False
    assert result["metadata"]["execution_mode"] == "RULE_BASED"
    assert result["metadata"]["primary_model"] is None
    assert result["metadata"]["model_runs"] == []
    assert result["conclusion"] == "RISK_FOUND"
    assert any(item["diff_type"] == "NUMERIC_CHANGED" for item in result["diff_items"])
    assert result["files"][0]["sha256"] == output.file_metadata[0]["sha256"]
    assert "token=secret" not in str(result)
    assert updates[-1][0] == TaskStage.PERSISTING_RESULT
    assert not any((tmp_path / "workspaces").iterdir())
