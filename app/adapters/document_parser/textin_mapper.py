from __future__ import annotations

from statistics import mean

from app.adapters.document_parser.textin_models import (
    TextInContentNode,
    TextInParseResponse,
)
from app.core.errors import WorkflowError
from app.documents.models import (
    DocumentBlock,
    DocumentLocation,
    ParsedDocument,
    ParsedTable,
    ProcessingWarning,
    TableCell,
    TableRow,
)
from app.documents.normalization import normalize_text
from app.services.downloader import LocalFile


def _scores(nodes: list[TextInContentNode]) -> list[float]:
    values: list[float] = []
    for node in nodes:
        if node.score is not None:
            score = node.score / 100 if node.score > 1 else node.score
            if 0 <= score <= 1:
                values.append(score)
        values.extend(_scores(node.content))
    return values


def _page_number(value: int | float | None) -> int | None:
    return int(value) if value is not None else None


def _bbox(values: list[float] | None) -> list[float] | None:
    if not values:
        return None
    if len(values) != 8:
        raise WorkflowError("OCR_RESPONSE_INVALID", "OCR 坐标结构无效")
    return [float(value) for value in values]


def _failed_status(value: str | int | None) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"-1", "failed", "failure", "error"}


def map_textin_document(
    response: TextInParseResponse,
    file: LocalFile,
    *,
    low_confidence: float,
) -> ParsedDocument:
    if response.data is None or response.data.result is None:
        raise WorkflowError("OCR_RESPONSE_INVALID", "OCR 服务响应缺少解析结果")
    result = response.data.result
    total = result.total_page_number or result.total_count or len(result.pages)
    valid = result.valid_page_number
    if valid is None:
        valid = result.success_count
    if total <= 0 or (valid is not None and valid < total):
        raise WorkflowError("OCR_PARTIAL_FAILURE", "OCR 服务仅完成了部分页面")
    if any(_failed_status(page.status) for page in result.pages):
        raise WorkflowError("OCR_PARTIAL_FAILURE", "OCR 服务存在失败页面")
    if not result.detail:
        raise WorkflowError("OCR_RESPONSE_INVALID", "OCR 服务未返回段落结构")

    page_confidence: dict[int, float] = {}
    warnings: list[ProcessingWarning] = [
        ProcessingWarning(
            code="OCR_USED",
            message="文档由外部 OCR 服务解析",
            requires_manual_review=False,
            file_id=file.file_id,
        )
    ]
    angles: dict[int, int] = {}
    for page in result.pages:
        page_number = _page_number(page.page_id)
        if page_number is None:
            continue
        values = _scores(page.content) + _scores(page.raw_ocr)
        if values:
            page_confidence[page_number] = round(mean(values), 4)
        else:
            warnings.append(
                ProcessingWarning(
                    code="OCR_CONFIDENCE_UNAVAILABLE",
                    message=f"第 {page_number} 页未返回可用的 OCR 置信度",
                    file_id=file.file_id,
                    page=page_number,
                )
            )
        angle = page.angle or 0
        angles[page_number] = angle
        if angle:
            warnings.append(
                ProcessingWarning(
                    code="OCR_PAGE_ROTATED",
                    message=f"第 {page_number} 页识别到 {angle} 度旋转",
                    file_id=file.file_id,
                    page=page_number,
                    details={"angle": angle},
                )
            )
    for page_number, confidence in page_confidence.items():
        if confidence < low_confidence:
            warnings.append(
                ProcessingWarning(
                    code="OCR_LOW_CONFIDENCE",
                    message=f"第 {page_number} 页 OCR 置信度低，需要人工复核",
                    file_id=file.file_id,
                    page=page_number,
                    confidence=confidence,
                )
            )

    sole_confidence = next(iter(page_confidence.values())) if len(page_confidence) == 1 else None
    blocks: list[DocumentBlock] = []
    table_index = 0
    for order, detail in enumerate(
        sorted(result.detail, key=lambda item: (item.page_id, item.paragraph_id))
    ):
        confidence = page_confidence.get(detail.page_id, sole_confidence)
        location = DocumentLocation(
            page=detail.page_id,
            paragraph_index=detail.paragraph_id,
            section=detail.text if detail.outline_level >= 0 else None,
            bbox=_bbox(detail.position),
            source="OCR",
            confidence=confidence,
        )
        if detail.type == "image":
            warnings.append(
                ProcessingWarning(
                    code="OCR_NON_TEXT_BLOCK_SKIPPED",
                    message=f"第 {detail.page_id} 页存在未进入文字比对的图像块",
                    file_id=file.file_id,
                    page=detail.page_id,
                )
            )
            continue
        if detail.type == "table":
            if not detail.cells:
                raise WorkflowError("OCR_PARTIAL_FAILURE", "OCR 表格缺少单元格结构")
            grouped: dict[int, list[TableCell]] = {}
            merged = False
            for cell in sorted(detail.cells, key=lambda item: (item.row, item.col)):
                merged = merged or cell.row_span > 1 or cell.col_span > 1
                grouped.setdefault(cell.row, []).append(
                    TableCell(
                        raw_text=cell.text,
                        normalized_text=normalize_text(cell.text),
                        location=DocumentLocation(
                            page=detail.page_id,
                            paragraph_index=detail.paragraph_id,
                            table_index=table_index,
                            row=cell.row,
                            column=cell.col,
                            bbox=_bbox(cell.position or cell.pos),
                            source="OCR",
                            confidence=confidence,
                        ),
                    )
                )
            rows = [TableRow(row=row, cells=cells) for row, cells in sorted(grouped.items())]
            if merged:
                warnings.append(
                    ProcessingWarning(
                        code="OCR_MERGED_CELLS_SIMPLIFIED",
                        message=f"第 {detail.page_id} 页表格包含跨行或跨列单元格",
                        file_id=file.file_id,
                        page=detail.page_id,
                        details={"table_index": table_index},
                    )
                )
            parsed_table = ParsedTable(table_index=table_index, rows=rows)
            raw = "\n".join("\t".join(cell.raw_text for cell in row.cells) for row in rows)
            location.table_index = table_index
            blocks.append(
                DocumentBlock(
                    block_id=f"{file.file_id}_ocr_t{table_index:06d}",
                    type="TABLE",
                    order=order,
                    raw_text=raw,
                    normalized_text=normalize_text(raw),
                    location=location,
                    table=parsed_table,
                )
            )
            table_index += 1
            continue
        block_type = {
            "header": "HEADER",
            "footer": "FOOTER",
            "sidebar": "SIDEBAR",
        }.get(detail.sub_type or "", "PARAGRAPH")
        blocks.append(
            DocumentBlock(
                block_id=f"{file.file_id}_ocr_p{detail.paragraph_id:06d}",
                type=block_type,
                order=order,
                raw_text=detail.text,
                normalized_text=normalize_text(detail.text),
                location=location,
            )
        )
    if not blocks:
        raise WorkflowError("OCR_PARSE_FAILED", "OCR 未提取到可比较内容")
    parsed_pages = {block.location.page for block in blocks if block.location.page is not None}
    if len(parsed_pages) < total:
        raise WorkflowError("OCR_PARTIAL_FAILURE", "OCR 未形成完整的逐页可比较内容")
    confidence_values = list(page_confidence.values())
    table_count = sum(block.table is not None for block in blocks)
    cell_count = sum(
        len(row.cells) for block in blocks if block.table is not None for row in block.table.rows
    )
    detail_page_count = len(parsed_pages)
    bbox_block_count = sum(block.location.bbox is not None for block in blocks)
    bbox_cell_count = sum(
        cell.location.bbox is not None
        for block in blocks
        if block.table is not None
        for row in block.table.rows
        for cell in row.cells
    )
    return ParsedDocument(
        file_id=file.file_id,
        role=file.role,
        file_name=file.file_name,
        sha256=file.sha256,
        page_count=total,
        blocks=blocks,
        parser_name="textin-document-parser",
        parser_metadata={
            "ocr": True,
            "engine_version": response.data.version,
            "duration_ms": response.data.duration,
            "response_size_bytes": response._response_size_bytes,
            "block_count": len(blocks),
            "table_count": table_count,
            "cell_count": cell_count,
            "detail_page_count": detail_page_count,
            "bbox_block_count": bbox_block_count,
            "bbox_cell_count": bbox_cell_count,
            "confidence_mean": round(mean(confidence_values), 4) if confidence_values else None,
            "confidence_min": round(min(confidence_values), 4) if confidence_values else None,
            # JSON object keys are strings. Keeping this JSON-native avoids
            # serializer-dependent integer-key handling in API responses and
            # persisted task metadata.
            "page_angles": {str(page): angle for page, angle in angles.items()},
        },
        warnings=warnings,
    )
