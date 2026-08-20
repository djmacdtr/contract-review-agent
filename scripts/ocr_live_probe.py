"""Call the configured OCR parser for one local synthetic PDF and print safe metrics only."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path

import orjson

from app.adapters.document_parser.base import ParseMode
from app.adapters.document_parser.textin_parser import TextInDocumentParser
from app.core.config import get_settings
from app.core.errors import WorkflowError
from app.services.downloader import PDF_MIME, LocalFile


async def probe(
    path: Path,
    *,
    mode: ParseMode,
    document_parser: TextInDocumentParser | None = None,
) -> dict:
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    file = LocalFile(
        file_id="fil_ocr_probe",
        role="TARGET",
        file_name=path.name,
        safe_url="local-probe://redacted",
        path=path,
        file_size=path.stat().st_size,
        sha256=content_hash,
        detected_mime_type=PDF_MIME,
    )
    parser = document_parser or TextInDocumentParser(get_settings())
    document = await parser.parse(file, mode=mode)
    table_count = sum(1 for block in document.blocks if block.table is not None)
    cell_count = sum(
        len(row.cells)
        for block in document.blocks
        if block.table is not None
        for row in block.table.rows
    )
    safe = {
        "parser_name": document.parser_name,
        "page_count": document.page_count,
        "block_count": len(document.blocks),
        "table_count": table_count,
        "cell_count": cell_count,
        "warning_codes": [warning.code for warning in document.warnings],
        "parser_metadata": document.parser_metadata,
    }
    return safe


def safe_failure(error: WorkflowError) -> dict:
    return {
        "ok": False,
        "code": error.code,
        "message": error.safe_message,
        "details": error.details,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--mode", choices=("auto", "scan"), default="auto")
    args = parser.parse_args()
    if not args.path.is_file():
        parser.error("probe file does not exist")
    try:
        result = asyncio.run(probe(args.path.resolve(), mode=args.mode))
    except WorkflowError as exc:
        print(orjson.dumps(safe_failure(exc)).decode(), file=sys.stderr)
        return 1
    print(orjson.dumps(result).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
