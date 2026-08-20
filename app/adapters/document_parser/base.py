from typing import Literal, Protocol

from app.documents.models import ParsedDocument
from app.services.downloader import LocalFile

ParseMode = Literal["auto", "scan"]


class ExternalDocumentParser(Protocol):
    async def parse(self, file: LocalFile, *, mode: ParseMode) -> ParsedDocument: ...
