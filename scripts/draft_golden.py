from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

from app.documents.parsers import DocxParser
from app.draft_review.golden_annotations import (
    build_annotation_candidates,
    load_annotation_set,
    merge_existing_annotations,
    validate_annotations,
)
from app.draft_review.template_checks import analyze_template
from app.services.downloader import DOCX_MIME, LocalFile

DEFAULT_SAMPLE_DIR = Path(r"D:\work\contract_review\脱敏真实合同")
DEFAULT_TARGET = DEFAULT_SAMPLE_DIR / "融资租赁合同（回租）.docx"
DEFAULT_TEMPLATE = DEFAULT_SAMPLE_DIR / "融资租赁合同（回租）模版.docx"
DEFAULT_ANNOTATIONS = DEFAULT_SAMPLE_DIR / "draft-review-golden-annotations.json"


def _local_file(path: Path, file_id: str, role: str) -> LocalFile:
    content = path.read_bytes()
    return LocalFile(
        file_id=file_id,
        role=role,
        file_name=path.name,
        safe_url="local-golden://redacted",
        path=path,
        file_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        detected_mime_type=DOCX_MIME,
    )


async def _generate(target_path: Path, template_path: Path):
    parser = DocxParser()
    target = await parser.parse(_local_file(target_path, "fil_golden_target", "TARGET"))
    template = await parser.parse(
        _local_file(template_path, "fil_golden_template", "TEMPLATE")
    )
    review = analyze_template(template, target)
    return build_annotation_candidates(template, target, review)


def _summary(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or validate safe DRAFT golden labels")
    parser.add_argument("command", choices=("export", "validate"))
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    args = parser.parse_args()

    generated = asyncio.run(_generate(args.target.resolve(), args.template.resolve()))
    if args.command == "export":
        existing = load_annotation_set(args.annotations) if args.annotations.exists() else None
        merged = merge_existing_annotations(generated, existing)
        args.annotations.write_text(
            merged.model_dump_json(indent=2),
            encoding="utf-8",
        )
        _summary(
            {
                "status": "EXPORTED",
                "candidate_count": len(merged.candidates),
                "classified_count": sum(
                    item.classification is not None for item in merged.candidates
                ),
                "target_sha256": merged.target_sha256,
                "template_sha256": merged.template_sha256,
            }
        )
        return 0

    if not args.annotations.exists():
        _summary({"status": "ANNOTATIONS_NOT_FOUND", "candidate_count": len(generated.candidates)})
        return 2
    annotated = load_annotation_set(args.annotations)
    result = validate_annotations(generated, annotated)
    _summary(
        {
            "status": "PASSED" if result.complete else "INCOMPLETE",
            **result.model_dump(mode="json"),
        }
    )
    return 0 if result.complete else 2


if __name__ == "__main__":
    sys.exit(main())
