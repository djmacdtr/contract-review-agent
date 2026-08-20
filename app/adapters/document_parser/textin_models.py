from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


class TextInModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class TextInContentNode(TextInModel):
    type: str | None = None
    sub_type: str | None = None
    text: str | None = None
    score: float | None = None
    pos: list[float] | None = None
    position: list[float] | None = None
    content: list[TextInContentNode] = Field(default_factory=list)


class TextInPage(TextInModel):
    page_id: int | float | None = None
    status: str | int | None = None
    angle: int | None = None
    width: int | None = None
    height: int | None = None
    content: list[TextInContentNode] = Field(default_factory=list)
    raw_ocr: list[TextInContentNode] = Field(default_factory=list)


class TextInCell(TextInModel):
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1
    text: str = ""
    position: list[float] | None = None
    pos: list[float] | None = None


class TextInDetail(TextInModel):
    page_id: int
    paragraph_id: int
    outline_level: int = -1
    text: str = ""
    type: str
    content: int | str = 0
    position: list[float] = Field(default_factory=list)
    origin_position: list[float] | None = None
    sub_type: str | None = None
    cells: list[TextInCell] = Field(default_factory=list)


class TextInResult(TextInModel):
    total_page_number: int | None = None
    valid_page_number: int | None = None
    success_count: int | None = None
    total_count: int | None = None
    paragraph_number: int | None = None
    character_number: int | None = None
    pages: list[TextInPage] = Field(default_factory=list)
    detail: list[TextInDetail] = Field(default_factory=list)


class TextInParseData(TextInModel):
    duration: int | float | None = None
    version: str | None = None
    result: TextInResult | None = None


class TextInParseResponse(TextInModel):
    _response_size_bytes: int | None = PrivateAttr(default=None)

    code: int
    msg: str = ""
    data: TextInParseData | None = None

    def safe_summary(self) -> dict[str, Any]:
        result = self.data.result if self.data else None
        return {
            "code": self.code,
            "page_count": result.total_page_number if result else None,
            "valid_page_count": result.valid_page_number if result else None,
        }
