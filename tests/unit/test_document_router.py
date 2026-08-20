from pathlib import Path

import pytest

from app.core.errors import WorkflowError
from app.documents.models import ParsedDocument
from app.documents.router import DocumentParsingRouter
from app.services.downloader import PDF_MIME, LocalFile


def scan_file(tmp_path: Path) -> LocalFile:
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.7")
    return LocalFile(
        file_id="fil_scan",
        role="TARGET",
        file_name="scan.pdf",
        safe_url="http://fixture/scan.pdf",
        path=path,
        file_size=8,
        sha256="c" * 64,
        detected_mime_type=PDF_MIME,
    )


class LocalParser:
    def __init__(self, result: ParsedDocument | None = None, code: str | None = None) -> None:
        self.result = result
        self.code = code

    async def parse(self, file: LocalFile) -> ParsedDocument:
        if self.code:
            raise WorkflowError(self.code, "local parser failure")
        assert self.result is not None
        return self.result


class ExternalParser:
    def __init__(self, result: ParsedDocument) -> None:
        self.result = result
        self.calls = 0

    async def parse(self, file: LocalFile) -> ParsedDocument:
        self.calls += 1
        return self.result


def parsed() -> ParsedDocument:
    return ParsedDocument(
        file_id="fil_scan",
        role="TARGET",
        file_name="scan.pdf",
        sha256="c" * 64,
        page_count=1,
        blocks=[],
        parser_name="test",
    )


async def test_router_uses_local_parser_without_external_call(tmp_path: Path) -> None:
    external = ExternalParser(parsed())
    router = DocumentParsingRouter(local=LocalParser(parsed()), external=external)
    assert (await router.parse(scan_file(tmp_path))).parser_name == "test"
    assert external.calls == 0


async def test_router_falls_back_only_for_ocr_required(tmp_path: Path) -> None:
    external = ExternalParser(parsed())
    router = DocumentParsingRouter(local=LocalParser(code="OCR_REQUIRED"), external=external)
    assert (await router.parse(scan_file(tmp_path))).parser_name == "test"
    assert external.calls == 1

    router = DocumentParsingRouter(local=LocalParser(code="PARSE_FAILED"), external=external)
    with pytest.raises(WorkflowError) as caught:
        await router.parse(scan_file(tmp_path))
    assert caught.value.code == "PARSE_FAILED"
    assert external.calls == 1


async def test_router_preserves_ocr_required_when_external_disabled(tmp_path: Path) -> None:
    router = DocumentParsingRouter(local=LocalParser(code="OCR_REQUIRED"), external=None)
    with pytest.raises(WorkflowError) as caught:
        await router.parse(scan_file(tmp_path))
    assert caught.value.code == "OCR_REQUIRED"
