from app.adapters.document_parser.textin_client import TextInDocumentParserClient
from app.adapters.document_parser.textin_mapper import map_textin_document
from app.core.config import Settings
from app.documents.models import ParsedDocument
from app.services.downloader import LocalFile


class TextInDocumentParser:
    def __init__(
        self, settings: Settings, *, client: TextInDocumentParserClient | None = None
    ) -> None:
        self.settings = settings
        self.client = client or TextInDocumentParserClient(settings)

    async def parse(self, file: LocalFile) -> ParsedDocument:
        response = await self.client.parse(file)
        return map_textin_document(
            response,
            file,
            low_confidence=self.settings.OCR_LOW_CONFIDENCE_THRESHOLD,
        )
