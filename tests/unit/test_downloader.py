from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import WorkflowError
from app.services.downloader import SafeFileDownloadService
from app.services.temp_files import TaskWorkspace

DOCX_BYTES = b"PK\x03\x04" + b"docx-content"


def settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "TEMP_ROOT": str(tmp_path),
        "MAX_FILE_SIZE_MB": 1,
        "ALLOW_HTTP_DOWNLOADS": True,
        "DOWNLOAD_HOST_ALLOWLIST": "fixture-server",
        "DOWNLOAD_TIMEOUT_SECONDS": 2,
    }
    values.update(overrides)
    return Settings(**values)


async def public_resolver(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


async def private_resolver(host: str, port: int) -> list[str]:
    return ["127.0.0.1"]


async def test_downloader_streams_hashes_valid_docx_and_workspace_cleans(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=DOCX_BYTES,
            request=request,
        )

    service = SafeFileDownloadService(
        settings(tmp_path),
        transport=httpx.MockTransport(handler),
        resolver=private_resolver,
    )
    async with TaskWorkspace(tmp_path, "tsk_download") as workspace:
        files = await service.prepare(
            [
                {
                    "file_id": "fil_1",
                    "file_name": "合同.docx",
                    "url": "http://fixture-server/contract.docx?secret=hidden",
                    "safe_url": "http://fixture-server/contract.docx",
                    "role": "BASELINE",
                }
            ],
            workspace,
        )
        assert files[0].path.read_bytes() == DOCX_BYTES
        assert files[0].detected_mime_type.endswith("wordprocessingml.document")
        assert len(files[0].sha256) == 64
        workspace_path = workspace.path
    assert not workspace_path.exists()


async def test_downloader_rejects_private_target_without_allowlist(tmp_path: Path) -> None:
    service = SafeFileDownloadService(
        settings(tmp_path, DOWNLOAD_HOST_ALLOWLIST=""),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=DOCX_BYTES)),
        resolver=private_resolver,
    )
    async with TaskWorkspace(tmp_path, "tsk_private") as workspace:
        with pytest.raises(WorkflowError) as caught:
            await service.prepare(
                [
                    {
                        "file_id": "fil_1",
                        "file_name": "a.docx",
                        "url": "http://127.0.0.1/a.docx",
                        "safe_url": "http://127.0.0.1/a.docx",
                        "role": "TARGET",
                    }
                ],
                workspace,
            )
    assert caught.value.code == "DOWNLOAD_FORBIDDEN_TARGET"


async def test_downloader_allows_only_the_configured_console_upload_route(
    tmp_path: Path,
) -> None:
    service = SafeFileDownloadService(
        settings(
            tmp_path,
            DOWNLOAD_HOST_ALLOWLIST="example.com",
            CONSOLE_UPLOAD_BASE_URL="http://127.0.0.1:8000",
        ),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=DOCX_BYTES, request=request)
        ),
        resolver=private_resolver,
    )
    async with TaskWorkspace(tmp_path, "tsk_console_upload") as workspace:
        files = await service.prepare(
            [
                {
                    "file_id": "fil_console",
                    "file_name": "合同.docx",
                    "url": "http://127.0.0.1:8000/api/v1/console/uploads/upl_ABC123",
                    "safe_url": "http://127.0.0.1:8000/api/v1/console/uploads/upl_ABC123",
                    "role": "TARGET",
                }
            ],
            workspace,
        )
        assert files[0].path.read_bytes() == DOCX_BYTES

        for forbidden_url in (
            "http://127.0.0.1:8000/health",
            "http://127.0.0.1:8001/api/v1/console/uploads/upl_ABC123",
        ):
            with pytest.raises(WorkflowError) as caught:
                await service.prepare(
                    [
                        {
                            "file_id": "fil_forbidden",
                            "file_name": "合同.docx",
                            "url": forbidden_url,
                            "safe_url": forbidden_url,
                            "role": "TARGET",
                        }
                    ],
                    workspace,
                )
            assert caught.value.code == "DOWNLOAD_FORBIDDEN_TARGET"


async def test_downloader_revalidates_redirect_and_enforces_streamed_size(tmp_path: Path) -> None:
    async def redirect_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private.docx"})
        return httpx.Response(200, content=DOCX_BYTES)

    redirect_service = SafeFileDownloadService(
        settings(tmp_path, DOWNLOAD_HOST_ALLOWLIST="example.com"),
        transport=httpx.MockTransport(redirect_handler),
        resolver=public_resolver,
    )
    async with TaskWorkspace(tmp_path, "tsk_redirect") as workspace:
        with pytest.raises(WorkflowError) as caught:
            await redirect_service.prepare(
                [
                    {
                        "file_id": "fil_1",
                        "file_name": "a.docx",
                        "url": "http://example.com/a.docx",
                        "safe_url": "http://example.com/a.docx",
                        "role": "TARGET",
                    }
                ],
                workspace,
            )
    assert caught.value.code == "DOWNLOAD_FORBIDDEN_TARGET"

    oversized = b"PK\x03\x04" + b"x" * 2048
    size_service = SafeFileDownloadService(
        settings(tmp_path, MAX_FILE_SIZE_MB=0.001),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=oversized)),
        resolver=private_resolver,
    )
    async with TaskWorkspace(tmp_path, "tsk_large") as workspace:
        with pytest.raises(WorkflowError) as caught:
            await size_service.prepare(
                [
                    {
                        "file_id": "fil_1",
                        "file_name": "a.docx",
                        "url": "http://fixture-server/a.docx",
                        "safe_url": "http://fixture-server/a.docx",
                        "role": "TARGET",
                    }
                ],
                workspace,
            )
    assert caught.value.code == "FILE_TOO_LARGE"


async def test_downloader_rejects_content_signature_mismatch(tmp_path: Path) -> None:
    service = SafeFileDownloadService(
        settings(tmp_path),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"not-a-docx")),
        resolver=private_resolver,
    )
    async with TaskWorkspace(tmp_path, "tsk_bad") as workspace:
        with pytest.raises(WorkflowError) as caught:
            await service.prepare(
                [
                    {
                        "file_id": "fil_1",
                        "file_name": "a.docx",
                        "url": "http://fixture-server/a.docx",
                        "safe_url": "http://fixture-server/a.docx",
                        "role": "TARGET",
                    }
                ],
                workspace,
            )
    assert caught.value.code == "FILE_CONTENT_INVALID"
