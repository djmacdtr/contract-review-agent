from typing import Literal, Protocol

from app.adapters.document_parser.base import ExternalDocumentParser
from app.core.errors import WorkflowError
from app.documents.models import ParsedDocument
from app.documents.page_locations import (
    DocxPageLocationSidecar,
    bind_docx_page_locations,
    build_docx_page_location_sidecar,
    page_location_structure_count,
)
from app.services.downloader import DOCX_MIME, PDF_MIME, LocalFile


class LocalDocumentParser(Protocol):
    async def parse(self, file: LocalFile) -> ParsedDocument: ...


class DocumentParsingRouter:
    def __init__(
        self,
        *,
        local: LocalDocumentParser,
        external: ExternalDocumentParser | None,
        docx_page_location_enabled: bool = False,
    ) -> None:
        self.local = local
        self.external = external
        self.docx_page_location_enabled = docx_page_location_enabled
        self.page_location_sidecars: dict[str, DocxPageLocationSidecar] = {}

    def _require_external(self) -> ExternalDocumentParser:
        if self.external is None:
            raise WorkflowError("OCR_NOT_CONFIGURED", "正式 PDF 解析需要外部文档解析服务")
        return self.external

    async def _parse_external(
        self,
        file: LocalFile,
        *,
        mode: Literal["auto", "scan"],
        include_stamp_images: bool = False,
    ) -> ParsedDocument:
        external = self._require_external()
        stamp_parser = getattr(external, "parse_with_stamp_images", None)
        if include_stamp_images and stamp_parser is not None:
            return await stamp_parser(file, mode=mode)
        return await external.parse(file, mode=mode)

    @staticmethod
    def _failure_details(
        exc: WorkflowError,
        *,
        local_document: ParsedDocument,
        failure_stage: str,
    ) -> dict[str, object]:
        details = dict(exc.details or {})
        details.setdefault("failure_stage", failure_stage)
        details.setdefault("failure_code", exc.code)
        details.setdefault("page_count", None)
        details.setdefault("external_detail_page_count", 0)
        details.setdefault("external_detail_count", 0)
        details.setdefault("external_structure_count", 0)
        details.setdefault(
            "local_structure_count", page_location_structure_count(local_document)
        )
        details.setdefault("candidate_mapping_count", 0)
        details.setdefault("unmapped_location_count", page_location_structure_count(local_document))
        return details

    async def _parse_docx(self, file: LocalFile) -> ParsedDocument:
        local_document = await self.local.parse(file)
        if not self.docx_page_location_enabled:
            return local_document
        try:
            external_document = await self._parse_external(file, mode="auto")
            sidecar = build_docx_page_location_sidecar(local_document, external_document)
            bind_docx_page_locations(local_document, sidecar)
            self.page_location_sidecars[file.file_id] = sidecar
        except WorkflowError as exc:
            if exc.code == "DOCX_PAGE_LOCATION_INCOMPLETE":
                raise
            raise WorkflowError(
                "DOCX_PAGE_LOCATION_INCOMPLETE",
                "DOCX 真实页码解析或映射未能可靠完成",
                details=self._failure_details(
                    exc,
                    local_document=local_document,
                    failure_stage="EXTERNAL_PARSE",
                ),
            ) from exc
        except Exception as exc:
            raise WorkflowError(
                "DOCX_PAGE_LOCATION_INCOMPLETE",
                "DOCX 真实页码解析或映射未能可靠完成",
                details={
                    "failure_stage": "EXTERNAL_PARSE",
                    "failure_code": type(exc).__name__,
                    "page_count": None,
                    "external_detail_page_count": 0,
                    "external_detail_count": 0,
                    "local_structure_count": page_location_structure_count(local_document),
                    "external_structure_count": 0,
                    "candidate_mapping_count": 0,
                    "unmapped_location_count": page_location_structure_count(local_document),
                },
            ) from exc
        return local_document

    async def parse_draft_review(self, files: list[LocalFile]) -> list[ParsedDocument]:
        self.page_location_sidecars = {}
        parsed: list[ParsedDocument] = []
        for file in files:
            if file.detected_mime_type == DOCX_MIME:
                parsed.append(await self._parse_docx(file))
            elif file.detected_mime_type == PDF_MIME:
                parsed.append(await self._parse_external(file, mode="auto"))
            else:
                raise WorkflowError("UNSUPPORTED_FILE_TYPE", "起草检查仅支持 DOCX 或 PDF")
        return parsed

    async def parse_final_compare(self, files: list[LocalFile]) -> list[ParsedDocument]:
        self.page_location_sidecars = {}
        if len(files) != 2:
            raise WorkflowError("COMPARISON_FAILED", "放款比对必须包含两个文件")
        mimes = [file.detected_mime_type for file in files]
        if all(mime == DOCX_MIME for mime in mimes):
            return [await self._parse_docx(file) for file in files]
        if all(mime == PDF_MIME for mime in mimes):
            return [
                await self._parse_external(
                    file,
                    mode="auto",
                    include_stamp_images=file.role == "TARGET",
                )
                for file in files
            ]
        if set(mimes) == {DOCX_MIME, PDF_MIME}:
            self._require_external()
            parsed: list[ParsedDocument] = []
            for file in files:
                if file.detected_mime_type == DOCX_MIME:
                    parsed.append(await self._parse_docx(file))
                else:
                    parsed.append(
                        await self._parse_external(
                            file,
                            mode="scan",
                            include_stamp_images=file.role == "TARGET",
                        )
                    )
            return parsed
        raise WorkflowError("UNSUPPORTED_FILE_TYPE", "放款比对仅支持 DOCX 或 PDF")
