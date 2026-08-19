from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ParsedDocument:
    file_id: str
    parser_name: str
    blocks: tuple[str, ...]
    warnings: tuple[str, ...] = ()


class DocumentParser(Protocol):
    async def parse(self, file_id: str, file_name: str) -> ParsedDocument: ...


class MockDocumentParser:
    async def parse(self, file_id: str, file_name: str) -> ParsedDocument:
        return ParsedDocument(
            file_id=file_id,
            parser_name="mock-parser",
            blocks=(f"模拟解析块：{file_name}",),
            warnings=("未执行真实 DOC/DOCX/PDF 解析",),
        )

