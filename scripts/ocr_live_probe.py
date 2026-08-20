"""Call the configured OCR parser for one local synthetic PDF and print safe metrics only."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from pathlib import Path

import orjson

from app.adapters.document_parser.textin_parser import TextInDocumentParser
from app.core.config import get_settings
from app.services.downloader import PDF_MIME, LocalFile


async def run(path: Path) -> None:
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
    document = await TextInDocumentParser(get_settings()).parse(file)
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
    print(orjson.dumps(safe).decode())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    if not args.path.is_file():
        raise SystemExit("probe file does not exist")
    asyncio.run(run(args.path.resolve()))


if __name__ == "__main__":
    main()
