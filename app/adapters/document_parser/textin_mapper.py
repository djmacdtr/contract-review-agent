from __future__ import annotations

import base64
import binascii
from statistics import mean
from typing import Any

from app.adapters.document_parser.textin_models import (
    TextInContentNode,
    TextInParseResponse,
)
from app.core.errors import WorkflowError
from app.documents.models import (
    DocumentBlock,
    DocumentLocation,
    ParsedDocument,
    ParsedStampImage,
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


def _safe_structure_diagnostics(result: Any, page_count: int) -> dict[str, int]:
    detail_pages = {detail.page_id for detail in result.detail}
    structure_count = len(result.detail) + sum(len(detail.cells) for detail in result.detail)
    return {
        "page_count": page_count,
        "external_detail_page_count": len(detail_pages),
        "external_detail_count": len(result.detail),
        "external_structure_count": structure_count,
    }


def _stamp_position(
    values: list[float] | None,
    *,
    page_width: int | None,
    page_height: int | None,
) -> tuple[tuple[float, ...], list[float]]:
    bbox = _bbox(values)
    if bbox is None:
        raise WorkflowError(
            "OCR_STAMP_IMAGE_INVALID",
            "OCR 印章影像缺少安全位置数据",
            details={"failure_stage": "STAMP_IMAGE_MAPPING"},
        )
    xs = bbox[0::2]
    ys = bbox[1::2]
    width = float(page_width or 0)
    height = float(page_height or 0)
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    if width > 0 and height > 0:
        normalized = (left / width, top / height, right / width, bottom / height)
    else:
        normalized = (left, top, right, bottom)
    return tuple(round(value, 4) for value in normalized), bbox


def _stamp_base64(data: Any) -> str | None:
    if data is None:
        return None
    if isinstance(data, dict):
        value = data.get("base64")
    else:
        value = getattr(data, "base64", None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _stamp_candidates(result: Any) -> list[dict[str, Any]]:
    pages = {
        page_number: page
        for page in result.pages
        if (page_number := _page_number(page.page_id))
    }
    candidates: list[dict[str, Any]] = []

    def add_candidate(
        *, page_number: int | None, node_type: str | None, sub_type: str | None,
        position: list[float] | None, data: Any,
    ) -> None:
        if page_number is None or (node_type or "").casefold() != "image":
            return
        if (sub_type or "").casefold() != "stamp":
            return
        page = pages.get(page_number)
        position_key, bbox = _stamp_position(
            position,
            page_width=getattr(page, "width", None),
            page_height=getattr(page, "height", None),
        )
        candidates.append(
            {
                "page": page_number,
                "position_key": position_key,
                "bbox": bbox,
                "base64": _stamp_base64(data),
            }
        )

    for detail in result.detail:
        add_candidate(
            page_number=detail.page_id,
            node_type=detail.type,
            sub_type=detail.sub_type,
            position=detail.position or detail.pos,
            data=detail.data,
        )

    def visit(page_number: int, nodes: list[TextInContentNode]) -> None:
        for node in nodes:
            add_candidate(
                page_number=page_number,
                node_type=node.type,
                sub_type=node.sub_type,
                position=node.position or node.pos or (node.data.region if node.data else None),
                data=node.data,
            )
            visit(page_number, node.content)

    for page_number, page in pages.items():
        visit(page_number, page.content)

    # The service repeats the same object in detail and page content. A normalized
    # page-relative rectangle is stable across those representations.
    deduplicated: dict[tuple[int, tuple[float, ...]], dict[str, Any]] = {}
    for candidate in candidates:
        key = (candidate["page"], candidate["position_key"])
        previous = deduplicated.get(key)
        if previous is None:
            deduplicated[key] = candidate
        elif previous["base64"] is None and candidate["base64"] is not None:
            previous["base64"] = candidate["base64"]
    return list(deduplicated.values())


def _materialize_stamp_images(
    result: Any,
    *,
    max_count: int,
    max_image_bytes: int,
    max_total_bytes: int,
) -> list[ParsedStampImage]:
    candidates = _stamp_candidates(result)
    if len(candidates) > max_count:
        raise WorkflowError(
            "OCR_STAMP_IMAGE_LIMIT",
            "OCR 印章影像数量超过允许上限",
            details={"failure_stage": "STAMP_IMAGE_MAPPING", "stamp_count": len(candidates)},
        )
    images: list[ParsedStampImage] = []
    total_bytes = 0
    for candidate in candidates:
        encoded = candidate["base64"]
        if not encoded:
            raise WorkflowError(
                "OCR_STAMP_IMAGE_UNAVAILABLE",
                "OCR 印章影像未返回可安全承载的图片数据",
                details={"failure_stage": "STAMP_IMAGE_MAPPING"},
            )
        if encoded.startswith("data:"):
            prefix, separator, encoded = encoded.partition(",")
            if separator != "," or prefix.casefold() not in {
                "data:image/png;base64",
                "data:image/jpeg;base64",
            }:
                raise WorkflowError(
                    "OCR_STAMP_IMAGE_INVALID",
                    "OCR 印章影像格式不受支持",
                    details={"failure_stage": "STAMP_IMAGE_MAPPING"},
                )
        compact = "".join(encoded.split())
        try:
            payload = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WorkflowError(
                "OCR_STAMP_IMAGE_INVALID",
                "OCR 印章影像数据无效",
                details={"failure_stage": "STAMP_IMAGE_MAPPING"},
            ) from exc
        if payload.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif payload.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        else:
            raise WorkflowError(
                "OCR_STAMP_IMAGE_INVALID",
                "OCR 印章影像格式不受支持",
                details={"failure_stage": "STAMP_IMAGE_MAPPING"},
            )
        if len(payload) > max_image_bytes or total_bytes + len(payload) > max_total_bytes:
            raise WorkflowError(
                "OCR_STAMP_IMAGE_LIMIT",
                "OCR 印章影像数据超过允许上限",
                details={"failure_stage": "STAMP_IMAGE_MAPPING"},
            )
        total_bytes += len(payload)
        images.append(
            ParsedStampImage(
                page=candidate["page"],
                bbox=candidate["bbox"],
                data_uri=f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}",
            )
        )
    return images


def map_textin_document(
    response: TextInParseResponse,
    file: LocalFile,
    *,
    low_confidence: float,
    include_stamp_images: bool = False,
    stamp_max_count: int = 64,
    stamp_max_image_bytes: int = 1_048_576,
    stamp_max_total_bytes: int = 8_388_608,
) -> ParsedDocument:
    if response.data is None or response.data.result is None:
        raise WorkflowError("OCR_RESPONSE_INVALID", "OCR 服务响应缺少解析结果")
    result = response.data.result
    total = result.total_page_number or result.total_count or len(result.pages)
    valid = result.valid_page_number
    if valid is None:
        valid = result.success_count
    if total <= 0 or (valid is not None and valid < total):
        raise WorkflowError(
            "OCR_PARTIAL_FAILURE",
            "OCR 服务仅完成了部分页面",
            details={
                "failure_stage": "EXTERNAL_PARSE",
                "failure_code": "EXTERNAL_PARTIAL_PAGE_RESULT",
                **_safe_structure_diagnostics(result, total),
            },
        )
    page_ids = {
        page_number
        for page in result.pages
        if (page_number := _page_number(page.page_id)) is not None
    }
    expected_page_ids = set(range(1, total + 1))
    if page_ids != expected_page_ids:
        raise WorkflowError(
            "OCR_RESPONSE_INVALID",
            "OCR 服务未返回完整的物理页码",
            details={
                "failure_stage": "PAGE_ID_VALIDATION",
                "failure_code": "EXTERNAL_PAGE_ID_INCOMPLETE",
                **_safe_structure_diagnostics(result, total),
                "external_page_count": len(page_ids),
            },
        )
    if any(_failed_status(page.status) for page in result.pages):
        raise WorkflowError(
            "OCR_PARTIAL_FAILURE",
            "OCR 服务存在失败页面",
            details={
                "failure_stage": "EXTERNAL_PARSE",
                "failure_code": "EXTERNAL_PAGE_FAILED",
                **_safe_structure_diagnostics(result, total),
            },
        )
    if not result.detail:
        raise WorkflowError(
            "OCR_RESPONSE_INVALID",
            "OCR 服务未返回段落结构",
            details={
                "failure_stage": "EXTERNAL_PARSE",
                "failure_code": "EXTERNAL_DETAIL_EMPTY",
                **_safe_structure_diagnostics(result, total),
            },
        )

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
            structure_id=(
                f"table:{table_index}"
                if detail.type == "table"
                else f"paragraph:{detail.paragraph_id}"
            ),
        )
        if detail.type == "image":
            if include_stamp_images and (detail.sub_type or "").casefold() == "stamp":
                continue
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
                            structure_id=(
                                f"table_cell:{table_index}:{cell.row}:{cell.col}"
                            ),
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
    parsed_pages = {block.location.page for block in blocks if block.location.page is not None}
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
    stamp_images = (
        _materialize_stamp_images(
            result,
            max_count=stamp_max_count,
            max_image_bytes=stamp_max_image_bytes,
            max_total_bytes=stamp_max_total_bytes,
        )
        if include_stamp_images
        else []
    )
    stamp_bytes = sum(
        (len(item.data_uri.split(",", 1)[1]) * 3) // 4
        - item.data_uri.split(",", 1)[1].count("=")
        for item in stamp_images
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
            "physical_page_numbers": True,
            "engine_version": response.data.version,
            "duration_ms": response.data.duration,
            "response_size_bytes": response._response_size_bytes,
            "block_count": len(blocks),
            "table_count": table_count,
            "cell_count": cell_count,
            "detail_page_count": detail_page_count,
            "detail_count": len(result.detail),
            "page_ids": sorted(page_ids),
            "bbox_block_count": bbox_block_count,
            "bbox_cell_count": bbox_cell_count,
            "confidence_mean": round(mean(confidence_values), 4) if confidence_values else None,
            "confidence_min": round(min(confidence_values), 4) if confidence_values else None,
            # JSON object keys are strings. Keeping this JSON-native avoids
            # serializer-dependent integer-key handling in API responses and
            # persisted task metadata.
            "page_angles": {str(page): angle for page, angle in angles.items()},
            "stamp_image_count": len(stamp_images),
            "stamp_image_bytes": stamp_bytes,
        },
        warnings=warnings,
        stamp_images=stamp_images,
    )
