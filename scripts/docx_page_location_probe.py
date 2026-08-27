"""Validate one redacted DOCX page-location mapping with safe metrics only."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from app.adapters.document_parser.textin_parser import TextInDocumentParser
from app.core.config import Settings
from app.core.errors import WorkflowError
from app.documents.page_locations import build_docx_page_location_sidecar
from app.documents.parsers import DocxParser
from app.services.downloader import DOCX_MIME, LocalFile


async def probe(path: Path) -> dict[str, object]:
    content = path.read_bytes()
    file = LocalFile(
        file_id="fil_docx_page_probe",
        role="TARGET",
        file_name=path.name,
        safe_url="local-probe://redacted",
        path=path,
        file_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        detected_mime_type=DOCX_MIME,
    )
    settings = Settings(DOCX_PAGE_LOCATION_ENABLED=True, OCR_HTTP_RETRY_ATTEMPTS=0)
    local = await DocxParser().parse(file)
    external = await TextInDocumentParser(settings).parse(file, mode="auto")
    sidecar = build_docx_page_location_sidecar(local, external)
    return {
        "file_name": path.name,
        "local_parser": local.parser_name,
        "external_parser": external.parser_name,
        "external_page_count": external.page_count,
        "local_block_count": len(local.blocks),
        "external_block_count": len(external.blocks),
        "diagnostics": sidecar.summary(),
        "unmapped_structures": [dict(item) for item in sidecar.unmapped_structures],
        "warning_codes": [warning.code for warning in external.warnings],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    path = args.path.resolve()
    if not path.is_file() or path.suffix.casefold() != ".docx":
        parser.error("probe file must be an existing DOCX")
    try:
        print(json.dumps({"ok": True, **asyncio.run(probe(path))}, ensure_ascii=False))
    except WorkflowError as exc:
        safe_keys = {
            "failure_stage",
            "failure_code",
            "page_count",
            "external_detail_page_count",
            "external_detail_count",
            "local_structure_count",
            "external_structure_count",
            "candidate_mapping_count",
            "unmapped_location_count",
            "returned_page_count",
            "unmapped_structures",
        }
        safe_details = {
            key: value
            for key, value in (exc.details or {}).items()
            if key in safe_keys
        }
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": exc.code,
                    **safe_details,
                    "message": exc.safe_message,
                },
                ensure_ascii=False,
            )
        )
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": "DOCX_PAGE_LOCATION_INCOMPLETE",
                    "failure_stage": "MAPPING",
                    "failure_code": type(exc).__name__,
                    "message": "DOCX 真实页码解析或映射未能可靠完成",
                },
                ensure_ascii=False,
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
