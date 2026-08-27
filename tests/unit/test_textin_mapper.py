import base64
import json
from pathlib import Path

import pytest

from app.adapters.document_parser.textin_mapper import map_textin_document
from app.adapters.document_parser.textin_models import TextInParseResponse
from app.adapters.document_parser.textin_parser import TextInDocumentParser
from app.core.config import Settings
from app.core.errors import WorkflowError
from app.services.downloader import PDF_MIME, LocalFile


def load_response() -> TextInParseResponse:
    path = Path(__file__).parents[1] / "fixtures" / "ocr" / "textin_success.json"
    return TextInParseResponse.model_validate_json(path.read_text(encoding="utf-8"))


def local_file(tmp_path: Path) -> LocalFile:
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.7")
    return LocalFile(
        file_id="fil_ocr",
        role="BASELINE",
        file_name="scan.pdf",
        safe_url="http://fixture/scan.pdf",
        path=path,
        file_size=8,
        sha256="b" * 64,
        detected_mime_type=PDF_MIME,
    )


class StubClient:
    async def parse(self, file: LocalFile, *, mode: str) -> TextInParseResponse:
        return load_response()


async def test_parser_records_external_mode_and_stable_warning(tmp_path: Path) -> None:
    parser = TextInDocumentParser(Settings(_env_file=None), client=StubClient())
    document = await parser.parse(local_file(tmp_path), mode="auto")
    assert document.parser_metadata["parse_mode"] == "auto"
    warning = next(item for item in document.warnings if item.code == "PDF_EXTERNAL_PARSE_USED")
    assert warning.details == {"parse_mode": "auto"}
    assert warning.requires_manual_review is False


def test_mapper_preserves_paragraph_table_location_and_confidence(tmp_path: Path) -> None:
    response = load_response()
    response._response_size_bytes = 1234
    document = map_textin_document(response, local_file(tmp_path), low_confidence=0.8)

    assert document.parser_name == "textin-document-parser"
    assert document.page_count == 1
    assert document.parser_metadata["engine_version"] == "test-engine-1.0"
    assert document.parser_metadata["confidence_min"] == 0.99
    assert document.parser_metadata["response_size_bytes"] == 1234
    assert document.parser_metadata["block_count"] == 2
    assert document.parser_metadata["table_count"] == 1
    assert document.parser_metadata["cell_count"] == 4
    assert document.parser_metadata["detail_page_count"] == 1
    assert document.parser_metadata["physical_page_numbers"] is True
    assert document.parser_metadata["bbox_block_count"] == 2
    assert document.parser_metadata["bbox_cell_count"] == 4
    assert [block.type for block in document.blocks] == ["PARAGRAPH", "TABLE"]
    paragraph = document.blocks[0]
    assert paragraph.location.page == 1
    assert paragraph.location.source == "OCR"
    assert paragraph.location.confidence == 0.99
    assert paragraph.location.bbox == [10.0, 20.0, 300.0, 20.0, 300.0, 60.0, 10.0, 60.0]
    table = document.blocks[1].table
    assert table is not None and table.rows[1].cells[1].raw_text == "1"
    assert table.rows[1].cells[1].location.column == 1
    assert any(warning.code == "OCR_USED" for warning in document.warnings)
    assert not any(warning.code == "OCR_LOW_CONFIDENCE" for warning in document.warnings)


def test_mapper_marks_low_confidence_and_rotation(tmp_path: Path) -> None:
    payload = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "ocr" / "textin_success.json").read_text(
            encoding="utf-8"
        )
    )
    payload["data"]["result"]["pages"][0]["angle"] = 90
    payload["data"]["result"]["pages"][0]["content"][0]["content"][0]["score"] = 0.55
    document = map_textin_document(
        TextInParseResponse.model_validate(payload), local_file(tmp_path), low_confidence=0.8
    )
    codes = {warning.code for warning in document.warnings}
    assert {"OCR_LOW_CONFIDENCE", "OCR_PAGE_ROTATED"} <= codes


def test_mapper_rejects_partial_pages_or_incomplete_tables(tmp_path: Path) -> None:
    payload = load_response().model_dump(mode="json")
    payload["data"]["result"]["valid_page_number"] = 0
    with pytest.raises(WorkflowError) as caught:
        map_textin_document(
            TextInParseResponse.model_validate(payload), local_file(tmp_path), low_confidence=0.8
        )
    assert caught.value.code == "OCR_PARTIAL_FAILURE"

    payload = load_response().model_dump(mode="json")
    payload["data"]["result"]["detail"] = []
    with pytest.raises(WorkflowError) as caught:
        map_textin_document(
            TextInParseResponse.model_validate(payload), local_file(tmp_path), low_confidence=0.8
        )
    assert caught.value.code == "OCR_RESPONSE_INVALID"

    payload = load_response().model_dump(mode="json")
    payload["data"]["result"]["total_page_number"] = 2
    payload["data"]["result"]["valid_page_number"] = 2
    payload["data"]["result"]["pages"].append(
        {**payload["data"]["result"]["pages"][0], "page_id": 2, "content": [], "raw_ocr": []}
    )
    document = map_textin_document(
        TextInParseResponse.model_validate(payload), local_file(tmp_path), low_confidence=0.8
    )
    assert document.page_count == 2
    assert document.parser_metadata["page_ids"] == [1, 2]
    assert document.parser_metadata["detail_page_count"] == 1


def test_mapper_warns_when_merged_cells_are_simplified(tmp_path: Path) -> None:
    payload = load_response().model_dump(mode="json")
    payload["data"]["result"]["detail"][1]["cells"][0]["row_span"] = 2
    document = map_textin_document(
        TextInParseResponse.model_validate(payload), local_file(tmp_path), low_confidence=0.8
    )
    assert any(warning.code == "OCR_MERGED_CELLS_SIMPLIFIED" for warning in document.warnings)

    payload = load_response().model_dump(mode="json")
    payload["data"]["result"]["detail"][1]["cells"] = []
    with pytest.raises(WorkflowError) as caught:
        map_textin_document(
            TextInParseResponse.model_validate(payload), local_file(tmp_path), low_confidence=0.8
        )
    assert caught.value.code == "OCR_PARTIAL_FAILURE"


def stamp_payload(*, with_base64: bool = True, stamp_count: int = 1) -> dict:
    payload = load_response().model_dump(mode="json")
    page = payload["data"]["result"]["pages"][0]
    page.update({"width": 1000, "height": 1000})
    image_data = {
        "base64": base64.b64encode(b"\x89PNG\r\n\x1a\nunit-test-image").decode()
    } if with_base64 else {"path": "private/stamp.png"}
    details = payload["data"]["result"]["detail"]
    details.append(
        {
            "page_id": 1,
            "paragraph_id": 2,
            "outline_level": -1,
            "text": "",
            "type": "image",
            "sub_type": "stamp",
            "position": [100, 100, 300, 100, 300, 300, 100, 300],
            "data": image_data,
        }
    )
    page["content"].append(
        {
            "type": "image",
            "sub_type": "stamp",
            "pos": [100, 100, 300, 100, 300, 300, 100, 300],
            "data": image_data,
        }
    )
    for index in range(1, stamp_count):
        position = [
            400 + index * 10,
            100,
            500 + index * 10,
            100,
            500 + index * 10,
            200,
            400 + index * 10,
            200,
        ]
        details.append(
            {
                "page_id": 1,
                "paragraph_id": 2 + index,
                "type": "image",
                "sub_type": "stamp",
                "position": position,
                "data": image_data,
            }
        )
    return payload


def test_mapper_materializes_and_deduplicates_stamp_images(tmp_path: Path) -> None:
    document = map_textin_document(
        TextInParseResponse.model_validate(stamp_payload()),
        local_file(tmp_path),
        low_confidence=0.8,
        include_stamp_images=True,
    )

    assert len(document.stamp_images) == 1
    assert document.stamp_images[0].page == 1
    assert document.stamp_images[0].data_uri.startswith("data:image/png;base64,")
    assert document.parser_metadata["stamp_image_count"] == 1


def test_mapper_rejects_stamp_url_or_excessive_stamp_count(tmp_path: Path) -> None:
    with pytest.raises(WorkflowError) as caught:
        map_textin_document(
            TextInParseResponse.model_validate(stamp_payload(with_base64=False)),
            local_file(tmp_path),
            low_confidence=0.8,
            include_stamp_images=True,
        )
    assert caught.value.code == "OCR_STAMP_IMAGE_UNAVAILABLE"
    assert "private/stamp.png" not in str(caught.value)

    with pytest.raises(WorkflowError) as caught:
        map_textin_document(
            TextInParseResponse.model_validate(stamp_payload(stamp_count=2)),
            local_file(tmp_path),
            low_confidence=0.8,
            include_stamp_images=True,
            stamp_max_count=1,
        )
    assert caught.value.code == "OCR_STAMP_IMAGE_LIMIT"
