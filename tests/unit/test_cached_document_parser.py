from pathlib import Path

from app.adapters.document_parser.cached_parser import (
    CachedExternalDocumentParser,
    InMemoryDocumentParseCache,
)
from app.core.config import Settings
from app.documents.models import ParsedDocument, ParsedStampImage, ProcessingWarning
from app.services.downloader import PDF_MIME, LocalFile


def settings() -> Settings:
    return Settings(
        _env_file=None,
        OCR_ENABLED=True,
        OCR_BASE_URL="https://ocr.invalid",
        OCR_API_KEY="unit-test-secret",
    )


def local_file(
    tmp_path: Path,
    *,
    file_id: str,
    role: str,
    file_name: str,
) -> LocalFile:
    path = tmp_path / file_name
    path.write_bytes(b"same-pdf")
    return LocalFile(
        file_id=file_id,
        role=role,
        file_name=file_name,
        safe_url="https://files.example.com/safe.pdf",
        path=path,
        file_size=path.stat().st_size,
        sha256="a" * 64,
        detected_mime_type=PDF_MIME,
    )


class CountingParser:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def _result(self, file: LocalFile, *, with_stamp: bool) -> ParsedDocument:
        return ParsedDocument(
            file_id=file.file_id,
            role=file.role,
            file_name=file.file_name,
            sha256=file.sha256,
            page_count=1,
            blocks=[],
            parser_name="TEXTIN",
            warnings=[
                ProcessingWarning(
                    code="OCR_USED",
                    message="used",
                    file_id=file.file_id,
                )
            ],
            stamp_images=(
                [ParsedStampImage(page=1, bbox=[0.0] * 8, data_uri="data:image/png;base64,AA==")]
                if with_stamp
                else []
            ),
        )

    async def parse(self, file: LocalFile, *, mode: str) -> ParsedDocument:
        self.calls.append((mode, False))
        return self._result(file, with_stamp=False)

    async def parse_with_stamp_images(
        self, file: LocalFile, *, mode: str
    ) -> ParsedDocument:
        self.calls.append((mode, True))
        return self._result(file, with_stamp=True)


async def test_cache_reuses_sha_and_rebinds_current_file_identity(tmp_path: Path) -> None:
    inner = CountingParser()
    cached = CachedExternalDocumentParser(
        inner,  # type: ignore[arg-type]
        InMemoryDocumentParseCache(),
        settings(),
    )
    first = local_file(
        tmp_path, file_id="fil_first", role="BASELINE", file_name="first.pdf"
    )
    second = local_file(
        tmp_path, file_id="fil_second", role="TARGET", file_name="second.pdf"
    )

    await cached.parse(first, mode="scan")
    result = await cached.parse(second, mode="scan")

    assert inner.calls == [("scan", False)]
    assert result.file_id == "fil_second"
    assert result.role == "TARGET"
    assert result.file_name == "second.pdf"
    assert result.warnings[0].file_id == "fil_second"


async def test_stamp_and_plain_results_use_distinct_cache_entries(tmp_path: Path) -> None:
    inner = CountingParser()
    cached = CachedExternalDocumentParser(
        inner,  # type: ignore[arg-type]
        InMemoryDocumentParseCache(),
        settings(),
    )
    file = local_file(tmp_path, file_id="fil_one", role="TARGET", file_name="one.pdf")

    plain = await cached.parse(file, mode="scan")
    stamped = await cached.parse_with_stamp_images(file, mode="scan")
    stamped_again = await cached.parse_with_stamp_images(file, mode="scan")

    assert inner.calls == [("scan", False), ("scan", True)]
    assert plain.stamp_images == []
    assert len(stamped.stamp_images) == 1
    assert stamped_again == stamped
