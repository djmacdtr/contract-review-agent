from typing import Protocol

from app.documents.models import ParsedDocument
from app.services.downloader import LocalFile


class ExternalDocumentParser(Protocol):
    async def parse(self, file: LocalFile) -> ParsedDocument: ...
