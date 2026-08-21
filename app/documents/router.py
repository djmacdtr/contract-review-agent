from typing import Protocol

from app.adapters.document_parser.base import ExternalDocumentParser
from app.core.errors import WorkflowError
from app.documents.models import ParsedDocument
from app.services.downloader import DOCX_MIME, PDF_MIME, LocalFile


class LocalDocumentParser(Protocol):
    async def parse(self, file: LocalFile) -> ParsedDocument: ...


class DocumentParsingRouter:
    def __init__(
        self,
        *,
        local: LocalDocumentParser,
        external: ExternalDocumentParser | None,
    ) -> None:
        self.local = local
        self.external = external

    def _require_external(self) -> ExternalDocumentParser:
        if self.external is None:
            raise WorkflowError("OCR_NOT_CONFIGURED", "正式 PDF 解析需要外部文档解析服务")
        return self.external

    async def parse_draft_review(self, files: list[LocalFile]) -> list[ParsedDocument]:
        parsed: list[ParsedDocument] = []
        for file in files:
            if file.detected_mime_type == DOCX_MIME:
                parsed.append(await self.local.parse(file))
            elif file.detected_mime_type == PDF_MIME:
                parsed.append(await self._require_external().parse(file, mode="auto"))
            else:
                raise WorkflowError("UNSUPPORTED_FILE_TYPE", "起草检查仅支持 DOCX 或 PDF")
        return parsed

    async def parse_final_compare(self, files: list[LocalFile]) -> list[ParsedDocument]:
        if len(files) != 2:
            raise WorkflowError("COMPARISON_FAILED", "放款比对必须包含两个文件")
        mimes = [file.detected_mime_type for file in files]
        if all(mime == DOCX_MIME for mime in mimes):
            return [await self.local.parse(file) for file in files]
        if all(mime == PDF_MIME for mime in mimes):
            external = self._require_external()
            return [await external.parse(file, mode="auto") for file in files]
        if set(mimes) == {DOCX_MIME, PDF_MIME}:
            external = self._require_external()
            parsed: list[ParsedDocument] = []
            for file in files:
                if file.detected_mime_type == DOCX_MIME:
                    parsed.append(await self.local.parse(file))
                else:
                    parsed.append(await external.parse(file, mode="scan"))
            return parsed
        raise WorkflowError("UNSUPPORTED_FILE_TYPE", "放款比对仅支持 DOCX 或 PDF")
