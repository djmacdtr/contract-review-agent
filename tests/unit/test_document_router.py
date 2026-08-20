from pathlib import Path

import pytest

from app.core.errors import WorkflowError
from app.documents.models import ParsedDocument
from app.documents.router import DocumentParsingRouter
from app.services.downloader import DOCX_MIME, PDF_MIME, LocalFile


def scan_file(tmp_path: Path, role: str = "TARGET") -> LocalFile:
    path = tmp_path / f"{role.lower()}.pdf"
    path.write_bytes(b"%PDF-1.7")
    return LocalFile(
        file_id=f"fil_{role.lower()}",
        role=role,
        file_name=path.name,
        safe_url=f"http://fixture/{path.name}",
        path=path,
        file_size=8,
        sha256="c" * 64,
        detected_mime_type=PDF_MIME,
    )


def docx_file(tmp_path: Path, role: str = "BASELINE") -> LocalFile:
    path = tmp_path / f"{role.lower()}.docx"
    path.write_bytes(b"PK\x03\x04synthetic")
    return LocalFile(
        file_id=f"fil_{role.lower()}",
        role=role,
        file_name=path.name,
        safe_url=f"http://fixture/{path.name}",
        path=path,
        file_size=path.stat().st_size,
        sha256="d" * 64,
        detected_mime_type=DOCX_MIME,
    )


class LocalParser:
    def __init__(self, result: ParsedDocument | None = None, code: str | None = None) -> None:
        self.result = result
        self.code = code
        self.calls: list[str] = []

    async def parse(self, file: LocalFile) -> ParsedDocument:
        self.calls.append(file.file_id)
        if self.code:
            raise WorkflowError(self.code, "local parser failure")
        assert self.result is not None
        return self.result


class ExternalParser:
    def __init__(self, result: ParsedDocument) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def parse(self, file: LocalFile, *, mode: str) -> ParsedDocument:
        self.calls.append((file.file_id, mode))
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


async def test_final_compare_docx_pair_uses_only_local_parser(tmp_path: Path) -> None:
    local = LocalParser(parsed())
    external = ExternalParser(parsed())
    router = DocumentParsingRouter(local=local, external=external)
    files = [docx_file(tmp_path), docx_file(tmp_path, "TARGET")]
    assert len(await router.parse_final_compare(files)) == 2
    assert local.calls == ["fil_baseline", "fil_target"]
    assert external.calls == []


async def test_final_compare_pdf_pair_uses_external_auto_for_both(tmp_path: Path) -> None:
    external = ExternalParser(parsed())
    local = LocalParser(parsed())
    router = DocumentParsingRouter(local=local, external=external)
    baseline = scan_file(tmp_path, "BASELINE")
    target = scan_file(tmp_path)
    assert len(await router.parse_final_compare([baseline, target])) == 2
    assert local.calls == []
    assert external.calls == [("fil_baseline", "auto"), ("fil_target", "auto")]


async def test_final_compare_docx_pdf_uses_local_and_external_scan(tmp_path: Path) -> None:
    local = LocalParser(parsed())
    external = ExternalParser(parsed())
    router = DocumentParsingRouter(local=local, external=external)
    pdf = scan_file(tmp_path)
    assert len(await router.parse_final_compare([docx_file(tmp_path), pdf])) == 2
    assert local.calls == ["fil_baseline"]
    assert external.calls == [("fil_target", "scan")]


async def test_formal_pdf_does_not_fall_back_when_external_disabled(tmp_path: Path) -> None:
    local = LocalParser(parsed())
    router = DocumentParsingRouter(local=local, external=None)
    with pytest.raises(WorkflowError) as caught:
        await router.parse_final_compare([docx_file(tmp_path), scan_file(tmp_path)])
    assert caught.value.code == "OCR_NOT_CONFIGURED"
    assert local.calls == []
