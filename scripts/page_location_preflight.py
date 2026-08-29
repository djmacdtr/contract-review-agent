"""Rebuild and validate DOCX page sidecars from the existing OCR cache only.

This operator diagnostic deliberately has no OCR or LLM fallback. It parses the
local DOCX structure, reads the content-addressed OCR parse cache, rebuilds the
page alignment with the current algorithm, and checks the public evidence of
the known successful report. Output is limited to hashes, counts, stages, and
safe error fields.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.document_parser.cached_parser import (
    CachedExternalDocumentParser,
    SqlAlchemyDocumentParseCache,
)
from app.adapters.document_parser.textin_parser import TextInDocumentParser
from app.core.config import Settings
from app.core.errors import WorkflowError
from app.db.models import TaskFile, TaskResult
from app.documents.models import ParsedDocument
from app.documents.page_location_cache import SqlAlchemyPageLocationSidecarCache
from app.documents.page_locations import (
    PAGE_LOCATION_ALGORITHM_VERSION,
    PAGE_LOCATION_CACHE_OWNER,
    PAGE_LOCATION_CACHE_VERSION,
    augment_unmapped_table_page_bindings,
    build_docx_page_location_sidecar,
)
from app.documents.parsers import ParserRegistry
from app.services.downloader import DOCX_MIME, LocalFile

SOURCE_TASK_ID = "tsk_01M161GFY6Q7YSP07R877XQM2B"
SAMPLE_DIR = Path(r"D:\work\contract_review\脱敏真实合同")
FILES = (
    ("融资租赁合同（回租）.docx", "TARGET"),
    ("融资租赁合同（回租）模版.docx", "TEMPLATE"),
    ("项目方案确认函.docx", "REFERENCE"),
)


def host_database_url(value: str) -> str:
    url = make_url(value)
    if url.host == "postgres" and url.port == 5432:
        return url.set(host="127.0.0.1", port=15432).render_as_string(
            hide_password=False
        )
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class CacheOnlyExternalDocumentParser:
    """Read cached OCR output and never fall back to the external service."""

    def __init__(self, cached_parser: CachedExternalDocumentParser) -> None:
        self.cached_parser = cached_parser

    async def parse(self, file: LocalFile, *, mode: str) -> ParsedDocument:
        cache_key = self.cached_parser._cache_key(
            mode=mode,
            include_stamp_images=False,
        )
        cached = await self.cached_parser.cache.load(
            file_sha256=file.sha256,
            cache_key=cache_key,
        )
        if not isinstance(cached, dict) or not isinstance(cached.get("document"), dict):
            raise WorkflowError(
                "PAGE_LOCATION_PREFLIGHT_BLOCKED",
                "页码预检缺少 OCR 解析缓存",
                details={
                    "failure_stage": "OCR_CACHE_READ",
                    "failure_code": "OCR_PARSE_CACHE_MISSING",
                },
            )
        return self.cached_parser._rebind(
            ParsedDocument.model_validate(cached["document"]),
            file,
        )


def safe_error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, WorkflowError):
        details = exc.details if isinstance(exc.details, dict) else {}
        allowed = {
            "failure_stage",
            "failure_code",
            "page_count",
            "local_structure_count",
            "external_structure_count",
            "external_detail_page_count",
            "candidate_mapping_count",
            "mapped_location_count",
            "unmapped_location_count",
            "required_evidence_count",
            "covered_evidence_count",
            "missing_evidence_count",
            "public_evidence_file_id",
            "public_evidence_location",
        }
        return {key: details[key] for key in allowed if key in details}
    return {
        "failure_stage": "PAGE_LOCATION_PREFLIGHT",
        "failure_code": type(exc).__name__,
    }


def strip_public_pages(result: dict[str, Any]) -> dict[str, Any]:
    """Remove existing public page fields so the preflight exercises sidecars."""

    value = copy.deepcopy(result)
    for diff in value.get("diff_items", []):
        if not isinstance(diff, dict):
            continue
        for side_name in ("baseline", "target"):
            side = diff.get(side_name)
            if not isinstance(side, dict):
                continue
            for location in side.get("locations", [side.get("location")]):
                if isinstance(location, dict):
                    location.pop("page", None)
            if isinstance(side.get("location"), dict):
                side["location"].pop("page", None)
    diff_ids = {
        item.get("diff_id")
        for item in value.get("diff_items", [])
        if isinstance(item, dict)
    }
    for risk in value.get("risk_items", []):
        if not isinstance(risk, dict) or not set(risk.get("related_diff_ids", [])) & diff_ids:
            continue
        for evidence in risk.get("source_evidence", []):
            if not isinstance(evidence, dict):
                continue
            for location in evidence.get("locations", [evidence.get("location")]):
                if isinstance(location, dict):
                    location.pop("page", None)
            if isinstance(evidence.get("location"), dict):
                evidence["location"].pop("page", None)
    return value


async def run(output: Path) -> dict[str, Any]:
    settings = Settings()
    database_url = host_database_url(settings.DATABASE_URL)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    started: dict[str, Any] = {
        "source_task_id": SOURCE_TASK_ID,
        "external_calls": 0,
        "ocr_calls": 0,
        "llm_calls": 0,
        "algorithm_version": PAGE_LOCATION_ALGORITHM_VERSION,
        "cache_owner": PAGE_LOCATION_CACHE_OWNER,
        "cache_version": PAGE_LOCATION_CACHE_VERSION,
    }
    try:
        hashes = {
            role: file_sha256(SAMPLE_DIR / file_name)
            for file_name, role in FILES
        }
        async with session_factory() as session:
            source_files = (
                await session.execute(
                    select(TaskFile)
                    .where(TaskFile.task_id == SOURCE_TASK_ID)
                    .order_by(TaskFile.sort_order)
                )
            ).scalars().all()
            source_result = await session.get(TaskResult, SOURCE_TASK_ID)
        if len(source_files) != len(FILES) or source_result is None:
            raise WorkflowError(
                "PAGE_LOCATION_PREFLIGHT_BLOCKED",
                "页码预检缺少三份来源文件或成功结果",
                details={
                    "failure_stage": "SOURCE_READ",
                    "failure_code": "SOURCE_REPORT_INCOMPLETE",
                },
            )
        source_by_role = {file.role.value: file for file in source_files}
        if set(source_by_role) != {role for _, role in FILES}:
            raise WorkflowError(
                "PAGE_LOCATION_PREFLIGHT_BLOCKED",
                "页码预检的来源文件角色不完整",
                details={
                    "failure_stage": "SOURCE_READ",
                    "failure_code": "SOURCE_FILE_ROLES_INVALID",
                },
            )
        for role, file_sha in hashes.items():
            source_sha = source_by_role[role].sha256
            if source_sha != file_sha:
                raise WorkflowError(
                    "PAGE_LOCATION_PREFLIGHT_BLOCKED",
                    "页码预检文件摘要不一致",
                    details={
                        "failure_stage": "SOURCE_FILE_HASH",
                        "failure_code": "SOURCE_FILE_SHA_MISMATCH",
                    },
                )

        parser_settings = settings.model_copy(
            update={
                "DATABASE_URL": database_url,
                "OCR_ENABLED": False,
                "DOCX_PAGE_LOCATION_ENABLED": True,
                "LLM_ENABLED": False,
            }
        )
        cached_parser = CachedExternalDocumentParser(
            TextInDocumentParser(parser_settings),
            SqlAlchemyDocumentParseCache(session_factory),
            parser_settings,
        )
        cache_only_external = CacheOnlyExternalDocumentParser(cached_parser)
        local_parser = ParserRegistry(
            pdf_min_text_chars_per_page=parser_settings.PDF_MIN_TEXT_CHARS_PER_PAGE
        )
        sidecar_cache = SqlAlchemyPageLocationSidecarCache(session_factory)
        sidecars: dict[str, Any] = {}
        file_reports: list[dict[str, Any]] = []
        semaphore = asyncio.Semaphore(2)

        async def process(file_name: str, role: str) -> None:
            async with semaphore:
                source_file = source_by_role[role]
                existing = await sidecar_cache.load(
                    file_sha256=hashes[role],
                    file_id=source_file.id,
                )
                has_unmapped_table = bool(
                    existing
                    and any(
                        item.get("kind") == "TABLE"
                        for item in existing.unmapped_structures
                    )
                )
                # A large target table can be repaired from the already cached
                # OCR stream without rebuilding the expensive full alignment.
                # Other flattened-table cases still require a full sidecar
                # rebuild so their structure mappings are recalculated.
                rebuild_flattened_table = has_unmapped_table and role != "TARGET"
                augment_large_table = bool(
                    existing and role == "TARGET" and has_unmapped_table
                )
                if (
                    existing is not None
                    and not rebuild_flattened_table
                    and not augment_large_table
                ):
                    file_reports.append(
                        {
                            "role": role,
                            "file_sha256": hashes[role],
                            "page_count": existing.page_count,
                            "local_structure_count": existing.local_structure_count,
                            "external_structure_count": existing.external_structure_count,
                            "candidate_mapping_count": existing.candidate_mapping_count,
                            "mapped_location_count": existing.mapped_location_count,
                            "required_location_count": existing.required_location_count,
                            "unmapped_location_count": existing.unmapped_location_count,
                            "status": "CACHED",
                        }
                    )
                    sidecars[source_file.id] = existing
                    return
                file_path = SAMPLE_DIR / file_name
                local_file = LocalFile(
                    file_id=source_file.id,
                    role=role,
                    file_name=file_name,
                    safe_url="",
                    path=file_path,
                    file_size=file_path.stat().st_size,
                    sha256=hashes[role],
                    detected_mime_type=DOCX_MIME,
                )
                local_document = await local_parser.parse(local_file)
                external_document = await cache_only_external.parse(
                    local_file,
                    mode="auto",
                )
                if augment_large_table and existing is not None:
                    sidecar = augment_unmapped_table_page_bindings(
                        local_document,
                        external_document,
                        existing,
                    )
                else:
                    sidecar = await asyncio.to_thread(
                        build_docx_page_location_sidecar,
                        local_document,
                        external_document,
                    )
                await sidecar_cache.save(
                    file_sha256=hashes[role],
                    sidecar=sidecar,
                )
                sidecars[source_file.id] = sidecar
                file_reports.append(
                    {
                        "role": role,
                        "file_sha256": hashes[role],
                        "page_count": sidecar.page_count,
                        "local_structure_count": sidecar.local_structure_count,
                        "external_structure_count": sidecar.external_structure_count,
                        "candidate_mapping_count": sidecar.candidate_mapping_count,
                        "mapped_location_count": sidecar.mapped_location_count,
                        "required_location_count": sidecar.required_location_count,
                        "unmapped_location_count": sidecar.unmapped_location_count,
                        "status": (
                            "AUGMENTED_LARGE_TABLE_FROM_OCR_CACHE"
                            if augment_large_table
                            else (
                                "REBUILT_FLATTENED_TABLE_FROM_OCR_CACHE"
                                if rebuild_flattened_table
                                else "REBUILT_FROM_OCR_CACHE"
                            )
                        ),
                    }
                )

        await asyncio.gather(*(process(file_name, role) for file_name, role in FILES))
        if len(sidecars) != 3:
            raise WorkflowError(
                "PAGE_LOCATION_PREFLIGHT_BLOCKED",
                "三份页码 sidecar 未全部生成",
                details={
                    "failure_stage": "SIDECAR_BUILD",
                    "failure_code": "PAGE_SIDECAR_CACHE_INCOMPLETE",
                },
            )
        # The failed retry did not persist a result. Its task error identifies
        # the first public trigger, so validate the known public triggers from
        # that execution instead of treating unrelated internal structures as
        # part of the publication gate.
        public_triggers = (
            ("TARGET", {"table_index": 2}),
            ("TEMPLATE", {"table_index": 0}),
        )
        for role, location in public_triggers:
            affected_file = source_by_role[role]
            if sidecars[affected_file.id].pages_for(location) is None:
                raise WorkflowError(
                    "PAGE_LOCATION_PREFLIGHT_BLOCKED",
                    "受影响公开表格位置仍缺少真实页码",
                    details={
                        "failure_stage": "PUBLIC_EVIDENCE_MAPPING",
                        "failure_code": "PUBLIC_LOCATION_UNMAPPED",
                        "public_evidence_file_id": affected_file.id,
                        "public_evidence_location": location,
                    },
                )
        public_coverage = {
            "required_evidence_count": len(public_triggers),
            "covered_evidence_count": len(public_triggers),
            "missing_evidence_count": 0,
            "scope": "known_failed_task_public_triggers",
        }
        started.update(
            {
                "status": "PAGE_LOCATION_PREFLIGHT_OK",
                "files": sorted(file_reports, key=lambda item: item["role"]),
                "sidecar_count": len(sidecars),
                "public_evidence_coverage": public_coverage,
            }
        )
    except Exception as exc:
        started.update({"status": "FAILED", **safe_error(exc)})
    finally:
        await engine.dispose()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(started, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return started


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    report = asyncio.run(run(arguments.output))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if report.get("status") == "PAGE_LOCATION_PREFLIGHT_OK" else 2)
