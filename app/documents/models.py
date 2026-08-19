from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class DocumentLocation(BaseModel):
    page: int | None = None
    paragraph_index: int | None = None
    table_index: int | None = None
    row: int | None = None
    column: int | None = None
    section: str | None = None
    bbox: list[str] | None = None


class TableCell(BaseModel):
    raw_text: str
    normalized_text: str
    location: DocumentLocation


class TableRow(BaseModel):
    row: int
    cells: list[TableCell]


class ParsedTable(BaseModel):
    table_index: int
    rows: list[TableRow]


class DocumentBlock(BaseModel):
    block_id: str
    type: Literal["PARAGRAPH", "TABLE", "HEADER", "FOOTER"]
    order: int
    raw_text: str
    normalized_text: str
    location: DocumentLocation
    table: ParsedTable | None = None


class ProcessingWarning(BaseModel):
    code: str
    message: str
    requires_manual_review: bool = True


class ParsedDocument(BaseModel):
    file_id: str
    role: str
    file_name: str
    sha256: str
    page_count: int | None
    blocks: list[DocumentBlock]
    parser_name: str
    parser_metadata: dict[str, Any] = Field(default_factory=dict)
    warnings: list[ProcessingWarning] = Field(default_factory=list)
