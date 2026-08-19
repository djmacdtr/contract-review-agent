from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OcrResult:
    pages: tuple[str, ...]
    mock: bool


class OcrAdapter(Protocol):
    async def recognize(self, file_id: str) -> OcrResult: ...


class DisabledOcrAdapter:
    async def recognize(self, file_id: str) -> OcrResult:
        return OcrResult(pages=(), mock=True)

