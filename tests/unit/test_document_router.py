from pathlib import Path

import pytest

from app.core.errors import WorkflowError
from app.documents.models import DocumentBlock, DocumentLocation, ParsedDocument
from app.documents.page_location_cache import InMemoryPageLocationSidecarCache
from app.documents.page_locations import DocxPageLocationSidecar, build_docx_page_location_sidecar
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


class DocxPageExternalParser:
    def __init__(self, result: ParsedDocument) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def parse(self, file: LocalFile, *, mode: str) -> ParsedDocument:
        self.calls.append((file.file_id, mode))
        return self.result.model_copy(update={"file_id": file.file_id})


class FailingDocxPageExternalParser:
    def __init__(self, error: WorkflowError) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def parse(self, file: LocalFile, *, mode: str) -> ParsedDocument:
        self.calls.append((file.file_id, mode))
        raise self.error


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


async def test_draft_review_routes_each_docx_locally_and_each_pdf_external_auto(
    tmp_path: Path,
) -> None:
    local = LocalParser(parsed())
    external = ExternalParser(parsed())
    router = DocumentParsingRouter(local=local, external=external)
    files = [
        docx_file(tmp_path, "TARGET"),
        scan_file(tmp_path, "REFERENCE"),
        docx_file(tmp_path, "TEMPLATE"),
        scan_file(tmp_path, "REFERENCE-2"),
    ]

    assert len(await router.parse_draft_review(files)) == 4
    assert local.calls == ["fil_target", "fil_template"]
    assert external.calls == [
        ("fil_reference", "auto"),
        ("fil_reference-2", "auto"),
    ]


async def test_docx_page_location_keeps_local_document_and_records_sidecar(
    tmp_path: Path,
) -> None:
    local_document = ParsedDocument(
        file_id="fil_target",
        role="TARGET",
        file_name="target.docx",
        sha256="d" * 64,
        page_count=None,
        blocks=[
            DocumentBlock(
                block_id="fil_target_p0",
                type="PARAGRAPH",
                order=0,
                raw_text="合同金额为100万元。",
                normalized_text="合同金额为100万元。",
                location={"paragraph_index": 0},
            )
        ],
        parser_name="python-docx",
    )
    external_document = local_document.model_copy(
        deep=True,
        update={
            "blocks": [
                local_document.blocks[0].model_copy(
                    update={
                        "location": DocumentLocation(paragraph_index=0, page=1)
                    }
                )
            ],
            "page_count": 1,
        },
    )
    local = LocalParser(local_document)
    external = DocxPageExternalParser(external_document)
    router = DocumentParsingRouter(
        local=local,
        external=external,
        docx_page_location_enabled=True,
    )

    parsed = await router.parse_draft_review([docx_file(tmp_path, "TARGET")])

    assert parsed[0].blocks[0].location.page == 1
    assert parsed[0].blocks[0].location.structure_id == "paragraph:0"
    assert external.calls == [("fil_target", "auto")]
    assert router.page_location_sidecars["fil_target"].page_count == 1


async def test_docx_parse_does_not_retain_stale_sidecar_on_cache_miss(
    tmp_path: Path,
) -> None:
    local_document = parsed().model_copy(
        update={
            "file_id": "fil_target",
            "role": "TARGET",
            "file_name": "target.docx",
            "page_count": None,
        }
    )
    router = DocumentParsingRouter(
        local=LocalParser(local_document),
        external=None,
        page_location_cache=InMemoryPageLocationSidecarCache(),
        docx_page_location_enabled=True,
    )
    router.page_location_sidecars["fil_target"] = DocxPageLocationSidecar(
        file_id="fil_target",
        page_count=1,
        mappings={},
        required_location_count=0,
        candidate_mapping_count=0,
        local_structure_count=0,
        external_structure_count=0,
        external_detail_page_count=1,
    )

    with pytest.raises(WorkflowError) as caught:
        await router.parse_draft_review_file(docx_file(tmp_path, "TARGET"))

    assert caught.value.code == "DOCX_PAGE_LOCATION_INCOMPLETE"
    assert "fil_target" not in router.page_location_sidecars


async def test_docx_page_location_preserves_safe_external_failure_chain(
    tmp_path: Path,
) -> None:
    local_document = parsed().model_copy(
        update={
            "file_id": "fil_target",
            "role": "TARGET",
            "file_name": "target.docx",
            "blocks": [],
        }
    )
    external = FailingDocxPageExternalParser(
        WorkflowError(
            "OCR_RESPONSE_INVALID",
            "OCR 服务未返回完整的物理页码",
            details={
                "failure_stage": "PAGE_ID_VALIDATION",
                "failure_code": "EXTERNAL_PAGE_ID_INCOMPLETE",
                "page_count": 3,
                "external_detail_page_count": 2,
                "external_detail_count": 7,
                "external_structure_count": 9,
            },
        )
    )
    router = DocumentParsingRouter(
        local=LocalParser(local_document),
        external=external,
        docx_page_location_enabled=True,
    )

    with pytest.raises(WorkflowError) as caught:
        await router.parse_draft_review([docx_file(tmp_path, "TARGET")])

    assert caught.value.code == "DOCX_PAGE_LOCATION_INCOMPLETE"
    assert caught.value.details == {
        "failure_stage": "PAGE_ID_VALIDATION",
        "failure_code": "EXTERNAL_PAGE_ID_INCOMPLETE",
        "page_count": 3,
        "external_detail_page_count": 2,
        "external_detail_count": 7,
        "external_structure_count": 9,
        "local_structure_count": 0,
        "candidate_mapping_count": 0,
        "unmapped_location_count": 0,
    }
    assert external.calls == [("fil_target", "auto")]


async def test_docx_page_location_sidecar_cache_rebinds_without_external_parse(
    tmp_path: Path,
) -> None:
    local_document = ParsedDocument(
        file_id="fil_target",
        role="TARGET",
        file_name="target.docx",
        sha256="d" * 64,
        page_count=None,
        blocks=[
            DocumentBlock(
                block_id="fil_target_p0",
                type="PARAGRAPH",
                order=0,
                raw_text="合同金额为100万元。",
                normalized_text="合同金额为100万元。",
                location={"paragraph_index": 0},
            )
        ],
        parser_name="python-docx",
    )
    external_document = local_document.model_copy(
        deep=True,
        update={
            "blocks": [
                local_document.blocks[0].model_copy(
                    update={
                        "location": DocumentLocation(paragraph_index=0, page=2)
                    }
                )
            ],
            "page_count": 2,
        },
    )
    external_document.parser_metadata["page_ids"] = [1, 2]
    cache = InMemoryPageLocationSidecarCache()
    await cache.save(
        file_sha256="d" * 64,
        sidecar=build_docx_page_location_sidecar(local_document, external_document),
    )
    local = LocalParser(local_document)
    external = DocxPageExternalParser(external_document)
    router = DocumentParsingRouter(
        local=local,
        external=external,
        page_location_cache=cache,
        docx_page_location_enabled=True,
    )

    parsed = await router.parse_draft_review([docx_file(tmp_path, "TARGET")])

    assert parsed[0].blocks[0].location.page == 2
    assert external.calls == []
    assert router.page_location_sidecars["fil_target"].file_id == "fil_target"


async def test_docx_page_location_sidecar_cache_saves_external_result(
    tmp_path: Path,
) -> None:
    local_document = ParsedDocument(
        file_id="fil_target",
        role="TARGET",
        file_name="target.docx",
        sha256="d" * 64,
        page_count=None,
        blocks=[
            DocumentBlock(
                block_id="fil_target_p0",
                type="PARAGRAPH",
                order=0,
                raw_text="合同金额为100万元。",
                normalized_text="合同金额为100万元。",
                location={"paragraph_index": 0},
            )
        ],
        parser_name="python-docx",
    )
    external_document = local_document.model_copy(
        deep=True,
        update={
            "blocks": [
                local_document.blocks[0].model_copy(
                    update={
                        "location": DocumentLocation(paragraph_index=0, page=1)
                    }
                )
            ],
            "page_count": 1,
        },
    )
    external_document.parser_metadata["page_ids"] = [1]
    cache = InMemoryPageLocationSidecarCache()
    external = DocxPageExternalParser(external_document)
    router = DocumentParsingRouter(
        local=LocalParser(local_document),
        external=external,
        page_location_cache=cache,
        docx_page_location_enabled=True,
    )

    await router.parse_draft_review([docx_file(tmp_path, "TARGET")])

    assert external.calls == [("fil_target", "auto")]
    assert await cache.load(file_sha256="d" * 64, file_id="fil_target") is not None


async def test_draft_review_pdf_requires_external_parser(tmp_path: Path) -> None:
    local = LocalParser(parsed())
    router = DocumentParsingRouter(local=local, external=None)

    with pytest.raises(WorkflowError) as caught:
        await router.parse_draft_review(
            [docx_file(tmp_path, "TARGET"), scan_file(tmp_path, "REFERENCE")]
        )

    assert caught.value.code == "OCR_NOT_CONFIGURED"
