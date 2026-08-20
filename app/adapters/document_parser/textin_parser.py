from app.adapters.document_parser.base import ParseMode
from app.adapters.document_parser.textin_client import TextInDocumentParserClient
from app.adapters.document_parser.textin_mapper import map_textin_document
from app.core.config import Settings
from app.documents.models import ParsedDocument, ProcessingWarning
from app.services.downloader import LocalFile


class TextInDocumentParser:
    def __init__(
        self, settings: Settings, *, client: TextInDocumentParserClient | None = None
    ) -> None:
        self.settings = settings
        self.client = client or TextInDocumentParserClient(settings)

    async def parse(self, file: LocalFile, *, mode: ParseMode) -> ParsedDocument:
        response = await self.client.parse(file, mode=mode)
        document = map_textin_document(
            response,
            file,
            low_confidence=self.settings.OCR_LOW_CONFIDENCE_THRESHOLD,
        )
        document.parser_metadata["parse_mode"] = mode
        document.warnings.append(
            ProcessingWarning(
                code="PDF_EXTERNAL_PARSE_USED",
                message="PDF 已使用外部文档解析服务处理",
                requires_manual_review=False,
                file_id=file.file_id,
                details={"parse_mode": mode},
            )
        )
        return document
