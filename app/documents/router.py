from typing import Protocol

from app.adapters.document_parser.base import ExternalDocumentParser
from app.core.errors import WorkflowError
from app.documents.models import ParsedDocument
from app.services.downloader import LocalFile


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

    async def parse(self, file: LocalFile) -> ParsedDocument:
        try:
            return await self.local.parse(file)
        except WorkflowError as exc:
            if exc.code != "OCR_REQUIRED" or self.external is None:
                raise
        return await self.external.parse(file)
