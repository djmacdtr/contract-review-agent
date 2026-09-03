import hashlib
import json
from pathlib import Path

import httpx
import pytest

from app.api.routes import console_uploads
from app.core.config import Settings
from app.main import app
from app.services.console_uploads import ConsoleUploadError, ConsoleUploadStore


class FakeUpload:
    def __init__(self, filename: str, content: bytes, chunk_size: int = 17) -> None:
        self.filename = filename
        self.content = content
        self.position = 0
        self.chunk_size = chunk_size

    async def read(self, size: int) -> bytes:
        requested = min(size, self.chunk_size)
        chunk = self.content[self.position : self.position + requested]
        self.position += len(chunk)
        return chunk


def make_store(tmp_path: Path, max_size_bytes: int = 1024 * 1024) -> ConsoleUploadStore:
    return ConsoleUploadStore(tmp_path, "http://api:8000", max_size_bytes)


@pytest.mark.asyncio
async def test_upload_is_chunked_hashed_and_resolvable(tmp_path: Path) -> None:
    content = b"PK\x03\x04" + b"contract-data" * 100
    store = make_store(tmp_path)

    result = await store.save(FakeUpload("合同.docx", content, chunk_size=3))

    assert result.size_bytes == len(content)
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert result.url.endswith(f"/api/v1/console/uploads/{result.upload_id}")
    path, metadata = store.resolve(result.upload_id)
    assert path.read_bytes() == content
    assert metadata["file_name"] == "合同.docx"
    assert (
        metadata["mime_type"]
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


@pytest.mark.asyncio
async def test_pdf_upload_uses_pdf_signature_and_mime(tmp_path: Path) -> None:
    result = await make_store(tmp_path).save(FakeUpload("scan.PDF", b"%PDF-1.7\nbody"))

    assert result.mime_type == "application/pdf"
    assert make_store(tmp_path).resolve(result.upload_id)[0].suffix == ".pdf"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content", "code"),
    [
        ("empty.pdf", b"", "UPLOAD_EMPTY"),
        ("fake.pdf", b"PK\x03\x04not-pdf", "UPLOAD_SIGNATURE_MISMATCH"),
        ("fake.docx", b"%PDF-1.7", "UPLOAD_SIGNATURE_MISMATCH"),
        ("notes.txt", b"plain text", "UPLOAD_UNSUPPORTED_TYPE"),
        ("..\\escape.pdf", b"%PDF-1.7", "UPLOAD_INVALID_FILENAME"),
    ],
)
async def test_upload_rejects_invalid_input_and_leaves_no_partial_file(
    tmp_path: Path, filename: str, content: bytes, code: str
) -> None:
    store = make_store(tmp_path)

    with pytest.raises(ConsoleUploadError) as error:
        await store.save(FakeUpload(filename, content))

    assert error.value.code == code
    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_upload_rejects_size_limit_and_cleans_partial_file(tmp_path: Path) -> None:
    store = make_store(tmp_path, max_size_bytes=8)

    with pytest.raises(ConsoleUploadError, match="文件大小") as error:
        await store.save(FakeUpload("large.pdf", b"%PDF-1.7-too-large"))

    assert error.value.code == "UPLOAD_TOO_LARGE"
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_upload_route_returns_envelope_and_get_serves_original_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    settings = Settings(
        UPLOAD_ROOT=str(tmp_path),
        CONSOLE_UPLOAD_BASE_URL="http://api:8000",
        MAX_FILE_SIZE_MB=1,
    )
    monkeypatch.setattr(console_uploads, "get_settings", lambda: settings)
    content = b"%PDF-1.7\nroute-test"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/console/uploads",
            files={"file": ("route.pdf", content, "application/octet-stream")},
        )
        assert response.status_code == 201
        payload = response.json()["data"]
        download = await client.get(f"/api/v1/console/uploads/{payload['upload_id']}")

    assert download.status_code == 200
    assert download.content == content
    assert download.headers["x-content-sha256"] == hashlib.sha256(content).hexdigest()


@pytest.mark.asyncio
async def test_expired_upload_cleanup_removes_data_and_metadata(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    result = await store.save(FakeUpload("old.pdf", b"%PDF-1.7"))
    metadata_path = tmp_path / f"{result.upload_id}.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["created_at"] = 0
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    assert store.cleanup_expired() == 1
    with pytest.raises(ConsoleUploadError):
        store.resolve(result.upload_id)
