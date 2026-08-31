"""Run one fresh five-file public DRAFT_REVIEW acceptance task.

The harness deliberately uses the public create endpoint and the normal
WorkerRunner/DraftReviewWorkflowExecutor path.  It does not read historical
task results or fact checkpoints.  OCR/page caches are content-addressed and
are only warmed before task creation when a required cache entry is absent.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import threading
import time
from collections import Counter, defaultdict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.document_parser.cached_parser import (
    CachedExternalDocumentParser,
    SqlAlchemyDocumentParseCache,
)
from app.adapters.document_parser.textin_client import TextInDocumentParserClient
from app.adapters.document_parser.textin_parser import TextInDocumentParser
from app.adapters.llm.openai_client import OpenAIContractLlmClient
from app.core.config import Settings
from app.core.enums import TaskStatus
from app.db.models import CheckTask, ExtractionCheckpoint, TaskFile, TaskResult
from app.documents.models import ParsedDocument
from app.documents.page_location_cache import SqlAlchemyPageLocationSidecarCache
from app.documents.page_locations import validate_public_page_coverage
from app.documents.parsers import ParserRegistry
from app.documents.router import DocumentParsingRouter
from app.draft_review.checkpoints import SqlAlchemyExtractionCheckpointStore
from app.draft_review.facts import stable_fact_id
from app.services.downloader import DOCX_MIME, PDF_MIME, LocalFile
from app.worker.runner import WorkerRunner
from app.workflows.draft_review import DraftReviewWorkflowExecutor
from app.workflows.router import WorkflowRouter

REPO_ROOT = Path(__file__).resolve().parents[1]
FILE_ROOT = Path(r"D:\work\contract_review")
FILE_SPECS = (
    {
        "path": Path(
            r"D:\work\contract_review\04 合同素材文件\02 合同起草版本\融资租赁合同（回租）.docx"
        ),
        "role": "TARGET",
        "mime_type": DOCX_MIME,
    },
    {
        "path": Path(
            r"D:\work\contract_review\04 合同素材文件\01 合同制式模版\融资租赁合同（回租）.docx"
        ),
        "role": "TEMPLATE",
        "mime_type": DOCX_MIME,
    },
    {
        "path": Path(
            r"D:\work\contract_review\04 合同素材文件\04 基准材料文件"
        )
        / "法律合规风险报告-XX公司合规报告.docx",
        "role": "REFERENCE",
        "mime_type": DOCX_MIME,
    },
    {
        "path": Path(
            r"D:\work\contract_review\04 合同素材文件\04 基准材料文件\评审会评审意见表（对内版).pdf"
        ),
        "role": "REFERENCE",
        "mime_type": PDF_MIME,
    },
    {
        "path": Path(
            r"D:\work\contract_review\04 合同素材文件\04 基准材料文件\项目方案确认函.docx"
        ),
        "role": "REFERENCE",
        "mime_type": DOCX_MIME,
    },
)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return None


class CountingTransport(httpx.AsyncBaseTransport):
    """Count only request totals, statuses, and provider finish reasons."""

    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport(retries=0)
        self.http_calls = 0
        self.statuses: Counter[int] = Counter()
        self.finish_reasons: Counter[str] = Counter()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.http_calls += 1
        response = await self.inner.handle_async_request(request)
        try:
            body = await response.aread()
            self.statuses[response.status_code] += 1
            try:
                payload = json.loads(body)
                choices = payload.get("choices") if isinstance(payload, dict) else None
                finish = (
                    choices[0].get("finish_reason")
                    if isinstance(choices, list)
                    and choices
                    and isinstance(choices[0], dict)
                    else None
                )
                if isinstance(finish, str):
                    self.finish_reasons[finish] += 1
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=body,
                request=request,
                extensions=response.extensions,
            )
        finally:
            await response.aclose()

    async def aclose(self) -> None:
        return None

    async def close_all(self) -> None:
        await self.inner.aclose()


class TimedWorkerRunner(WorkerRunner):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.stage_events: list[tuple[str, float]] = []

    async def _progress(
        self, task_id: str, stage: Any, progress: int, message: str
    ) -> None:
        if not self.stage_events or self.stage_events[-1][0] != stage.value:
            self.stage_events.append((stage.value, time.monotonic()))
        await super()._progress(task_id, stage, progress, message)


def host_database_url(value: str) -> str:
    url = make_url(value)
    if url.host == "postgres" and url.port == 5432:
        return url.set(host="127.0.0.1", port=15432).render_as_string(hide_password=False)
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_error(details: Any) -> dict[str, Any]:
    if not isinstance(details, dict):
        return {}
    allowed = {
        "failure_stage",
        "failure_code",
        "underlying_failure_code",
        "component",
        "chain",
        "file_id",
        "batch_id",
        "batch_depth",
        "unit_count",
        "numeric_candidate_count",
        "request_attempts",
        "structure_retries",
        "finish_reason",
        "http_status",
        "required_evidence_count",
        "covered_evidence_count",
        "missing_evidence_count",
        "public_evidence_file_id",
        "public_evidence_location",
    }
    return {key: details[key] for key in allowed if key in details}


def acquire_lock(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump({"started_at": time.time(), "purpose": "fresh_five_file_public_task"}, stream)


def git_preflight() -> dict[str, Any]:
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    allowed_prefixes = ("backups/", "tmp/", ".real-diagnostic-temp/")
    allowed_paths = {"scripts/draft_review_five_file_public_acceptance.py"}
    unallowed = []
    for line in status.stdout.splitlines():
        path = line[3:] if len(line) >= 4 else line
        if not path.startswith(allowed_prefixes) and path not in allowed_paths:
            unallowed.append(line)
    commits = {}
    for commit in ("d0cca24", "6d8166d"):
        check = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=REPO_ROOT,
            check=False,
        )
        commits[commit] = check.returncode == 0
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, check=False, capture_output=True, text=True
    )
    return {
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "required_commits": commits,
        "unallowed_tracked_changes": unallowed,
        "allowed_workspace_entries": [
            line for line in status.stdout.splitlines() if line not in unallowed
        ],
        "passed": not unallowed and all(commits.values()),
    }


def runtime_settings(base: Settings) -> Settings:
    return base.model_copy(
        update={
            "DATABASE_URL": host_database_url(base.DATABASE_URL),
            "TEMP_ROOT": str(REPO_ROOT / ".real-diagnostic-temp" / "five-file-public-worker"),
            "ALLOW_HTTP_DOWNLOADS": True,
            "DOWNLOAD_HOST_ALLOWLIST": "127.0.0.1",
            "MAX_REFERENCE_FILES": max(20, base.MAX_REFERENCE_FILES),
            "OCR_ENABLED": True,
            "DOCX_PAGE_LOCATION_ENABLED": True,
            "OCR_HTTP_RETRY_ATTEMPTS": 0,
            "LLM_ENABLED": True,
            "LLM_EXTRACTION_MODEL": "GLM-5.3-Flash",
            "LLM_REVIEW_MODEL": "GLM-5.3-Flash",
            "LLM_ADVICE_MODEL": "GLM-5.3-Flash",
            "LLM_RESPONSE_FORMAT": "json_schema",
            "LLM_NATIVE_STRUCTURED_OUTPUT": True,
            "LLM_MAX_CONCURRENCY": 2,
            "LLM_EXTRACTION_TASK_CONCURRENCY": 2,
            "LLM_HTTP_RETRY_ATTEMPTS": 1,
            "LLM_FACT_REVIEW_ENABLED": False,
            "LLM_MAPPING_REVIEW_ENABLED": False,
            "LLM_SEMANTIC_PLAN_ENABLED": False,
        }
    )


def file_inventory() -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    inventory: list[dict[str, Any]] = []
    hashes: set[str] = set()
    for index, spec in enumerate(FILE_SPECS):
        path = spec["path"]
        if not path.is_file() or not path.stat().st_size:
            return inventory, {"failure_code": "LOCAL_FILE_MISSING", "index": str(index)}
        digest = sha256(path)
        if digest in hashes:
            return inventory, {"failure_code": "DUPLICATE_FILE_SHA256", "index": str(index)}
        hashes.add(digest)
        inventory.append(
            {
                "index": index,
                "role": spec["role"],
                "file_name": path.name,
                "mime_type": spec["mime_type"],
                "bytes": path.stat().st_size,
                "sha256": digest,
                "path_exists": True,
            }
        )
    return inventory, None


async def service_preflight(
    session_factory: async_sessionmaker[AsyncSession], api_base_url: str
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        health = await client.get(f"{api_base_url}/health")
        ready = await client.get(f"{api_base_url}/ready")
    async with session_factory() as session:
        active_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(CheckTask)
                    .where(CheckTask.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
                )
            ).scalar_one()
        )
    return {
        "health_status": health.status_code,
        "ready_status": ready.status_code,
        "active_task_count": active_count,
        "passed": health.status_code == 200 and ready.status_code == 200 and active_count == 0,
    }


def _local_file(spec: dict[str, Any], index: int) -> LocalFile:
    path = spec["path"]
    digest = sha256(path)
    return LocalFile(
        file_id=f"preflight_{index:02d}",
        role=spec["role"],
        file_name=path.name,
        safe_url=f"http://127.0.0.1/preflight/{index}",
        path=path,
        file_size=path.stat().st_size,
        sha256=digest,
        detected_mime_type=spec["mime_type"],
    )


async def cache_audit(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    inventory: list[dict[str, Any]],
) -> dict[str, Any]:
    cache = SqlAlchemyDocumentParseCache(session_factory)
    cached_parser = CachedExternalDocumentParser(None, cache, settings)
    page_cache = SqlAlchemyPageLocationSidecarCache(session_factory)
    items: list[dict[str, Any]] = []
    for item, spec in zip(inventory, FILE_SPECS, strict=True):
        cache_key = cached_parser._cache_key(mode="auto", include_stamp_images=False)
        cached = await cache.load(file_sha256=item["sha256"], cache_key=cache_key)
        ocr_hit = False
        parsed_page_count: int | None = None
        if isinstance(cached, dict) and isinstance(cached.get("document"), dict):
            try:
                document = ParsedDocument.model_validate(cached["document"])
                ocr_hit = document.sha256 == item["sha256"] and document.page_count is not None
                parsed_page_count = document.page_count
            except Exception:
                ocr_hit = False
        sidecar_hit: bool | None = None
        sidecar_page_count: int | None = None
        if spec["mime_type"] == DOCX_MIME:
            sidecar = await page_cache.load(
                file_sha256=item["sha256"], file_id=f"preflight_{item['index']:02d}"
            )
            sidecar_hit = sidecar is not None
            sidecar_page_count = sidecar.page_count if sidecar is not None else None
        items.append(
            {
                **{key: item[key] for key in ("index", "role", "file_name", "mime_type", "sha256")},
                "ocr_cache_hit": ocr_hit,
                "page_sidecar_hit": sidecar_hit,
                "page_count": sidecar_page_count or parsed_page_count,
            }
        )
    return {
        "items": items,
        "ocr_cache_hits": sum(item["ocr_cache_hit"] for item in items),
        "ocr_cache_total": len(items),
        "page_sidecar_hits": sum(item["page_sidecar_hit"] is True for item in items),
        "page_sidecar_required": sum(item["mime_type"] == DOCX_MIME for item in items),
        "all_ocr_cached": all(item["ocr_cache_hit"] for item in items),
        "all_docx_sidecars_cached": all(
            item["page_sidecar_hit"] for item in items if item["mime_type"] == DOCX_MIME
        ),
    }


async def preheat_missing_caches(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    inventory: list[dict[str, Any]],
) -> dict[str, Any]:
    audit = await cache_audit(session_factory, settings, inventory)
    missing = [
        item
        for item in audit["items"]
        if not item["ocr_cache_hit"] or (
            item["mime_type"] == DOCX_MIME and not item["page_sidecar_hit"]
        )
    ]
    if not missing:
        audit["preheat_http_calls"] = 0
        audit["preheated_indexes"] = []
        return audit

    local = ParserRegistry(pdf_min_text_chars_per_page=settings.PDF_MIN_TEXT_CHARS_PER_PAGE)
    ocr_transport = CountingTransport()
    external = CachedExternalDocumentParser(
        TextInDocumentParser(
            settings,
            client=TextInDocumentParserClient(settings, transport=ocr_transport),
        ),
        SqlAlchemyDocumentParseCache(session_factory),
        settings,
    )
    router = DocumentParsingRouter(
        local=local,
        external=external,
        page_location_cache=SqlAlchemyPageLocationSidecarCache(session_factory),
        docx_page_location_enabled=True,
    )
    semaphore = asyncio.Semaphore(2)

    async def parse_one(item: dict[str, Any]) -> None:
        async with semaphore:
            await router.parse_draft_review_file(
                _local_file(FILE_SPECS[item["index"]], item["index"])
            )

    try:
        await asyncio.gather(*(parse_one(item) for item in missing))
        refreshed = await cache_audit(session_factory, settings, inventory)
        refreshed["preheat_http_calls"] = ocr_transport.http_calls
        refreshed["preheated_indexes"] = [item["index"] for item in missing]
        return refreshed
    finally:
        await ocr_transport.close_all()


async def document_checkpoint_counts(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
    files: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    by_sha = {item["sha256"]: item for item in files}
    facts_by_sha: dict[str, set[str]] = defaultdict(set)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ExtractionCheckpoint).where(
                    ExtractionCheckpoint.task_id == task_id,
                    ExtractionCheckpoint.status == "SUCCEEDED",
                )
            )
        ).scalars().all()
    for row in rows:
        if row.file_sha256 not in by_sha or not isinstance(row.value, dict):
            continue
        raw_facts = row.value.get("facts")
        if not isinstance(raw_facts, list):
            raw_facts = (row.value.get("extraction") or {}).get("facts")
        if not isinstance(raw_facts, list):
            continue
        for fact in raw_facts:
            if isinstance(fact, dict):
                try:
                    facts_by_sha[row.file_sha256].add(stable_fact_id(fact))
                except Exception:
                    continue
    return {
        item["file_id"]: {
            "extracted_fact_count": len(facts_by_sha[item["sha256"]]),
            "qualified_fact_count": len(facts_by_sha[item["sha256"]]),
        }
        for item in files
    }


def evidence_file_id(evidence: dict[str, Any]) -> str | None:
    if not isinstance(evidence, dict):
        return None
    if isinstance(evidence.get("file_id"), str):
        return evidence["file_id"]
    if isinstance(evidence.get("source_file_id"), str):
        return evidence["source_file_id"]
    location = evidence.get("location")
    if isinstance(location, dict) and isinstance(location.get("file_id"), str):
        return location["file_id"]
    return None


def result_summary(
    result: dict[str, Any],
    *,
    task_files: list[dict[str, Any]],
    page_sidecars: dict[str, Any],
    checkpoint_counts: dict[str, dict[str, int]],
) -> dict[str, Any]:
    files = result.get("files", [])
    risks = result.get("risk_items", [])
    diffs = result.get("diff_items", [])
    passed = result.get("passed_checks", [])
    metadata = result.get("metadata", {})
    model_runs = metadata.get("model_runs", [])
    advice_coverage = metadata.get("advice_coverage", {})
    participation: list[dict[str, Any]] = []
    fact_matrix = result.get("fact_matrix", [])
    for file in task_files:
        file_id = file["file_id"]
        mapping_count = 0
        formal_evidence_count = 0
        for matrix in fact_matrix:
            if not isinstance(matrix, dict):
                continue
            target_candidate = matrix.get("target_candidate")
            if (
                isinstance(target_candidate, dict)
                and target_candidate.get("source_file_id") == file_id
            ):
                mapping_count += 1
            for relation in matrix.get("reference_results", []):
                if isinstance(relation, dict):
                    candidate = relation.get("candidate")
                    if isinstance(candidate, dict) and candidate.get("source_file_id") == file_id:
                        mapping_count += 1
        for diff in diffs:
            if isinstance(diff, dict):
                formal_evidence_count += sum(
                    isinstance(diff.get(side), dict)
                    and diff[side].get("file_id") == file_id
                    for side in ("baseline", "target")
                )
        for risk in risks:
            if isinstance(risk, dict):
                formal_evidence_count += sum(
                    evidence_file_id(evidence) == file_id
                    for evidence in risk.get("source_evidence", [])
                    if isinstance(evidence, dict)
                )
        counts = checkpoint_counts.get(
            file_id, {"extracted_fact_count": 0, "qualified_fact_count": 0}
        )
        participation.append(
            {
                "file_id": file_id,
                "role": file["role"],
                "file_name": file["file_name"],
                **counts,
                "mapping_relation_count": mapping_count,
                "formal_evidence_count": formal_evidence_count,
                "no_matching_target_fact": (
                    "NO_MATCHING_TARGET_FACT"
                    if file["role"] == "REFERENCE" and mapping_count == 0
                    else None
                ),
            }
        )
    page_coverage = validate_public_page_coverage(result, page_sidecars)
    purpose_counts = Counter(
        run.get("purpose") for run in model_runs if isinstance(run, dict) and run.get("purpose")
    )
    return {
        "file_count": len(files),
        "risk_count": len(risks),
        "diff_count": len(diffs),
        "passed_count": len(passed),
        "fact_matrix_count": len(fact_matrix),
        "advice": {
            "risk_count": len(risks),
            "non_empty_count": sum(
                isinstance(risk, dict) and bool(str(risk.get("analysis_advice") or "").strip())
                for risk in risks
            ),
            "coverage": advice_coverage,
        },
        "model_run_purposes": dict(sorted(purpose_counts.items())),
        "page_location_coverage": page_coverage,
        "file_page_counts": [
            {
                "file_name": file.get("file_name"),
                "role": file.get("role"),
                "page_count": file.get("page_count"),
            }
            for file in files
            if isinstance(file, dict)
        ],
        "reference_participation": participation,
    }


async def create_and_execute(
    api_base_url: str,
    output: Path,
    *,
    preflight: dict[str, Any],
    base_settings: Settings,
    inventory: list[dict[str, Any]],
) -> dict[str, Any]:
    settings = runtime_settings(base_settings)
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        lambda *args, **kwargs: QuietHandler(*args, directory=str(FILE_ROOT), **kwargs),
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    llm_transport = CountingTransport()
    ocr_transport = CountingTransport()
    started = time.monotonic()
    task_id: str | None = None
    runner: TimedWorkerRunner | None = None
    try:
        port = int(server.server_address[1])
        urls = {
            index: (
                f"http://127.0.0.1:{port}/"
                f"{quote(item['path'].relative_to(FILE_ROOT).as_posix(), safe='/')}"
            )
            for index, item in enumerate(FILE_SPECS)
        }
        payload = {
            "client_reference_id": f"draft-review-five-file-{int(time.time())}",
            "target_file": {
                "url": urls[0],
                "file_name": FILE_SPECS[0]["path"].name,
                "mime_type": FILE_SPECS[0]["mime_type"],
            },
            "template_file": {
                "url": urls[1],
                "file_name": FILE_SPECS[1]["path"].name,
                "mime_type": FILE_SPECS[1]["mime_type"],
            },
            "reference_files": [
                {
                    "url": urls[index],
                    "file_name": item["path"].name,
                    "mime_type": item["mime_type"],
                }
                for index, item in enumerate(FILE_SPECS[2:], start=2)
            ],
            "options": {
                "ignore_formatting": True,
                "ignore_headers_footers": True,
                "check_blank_fields": True,
                "check_numeric_consistency": True,
            },
        }
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.post(f"{api_base_url}/api/v1/draft-reviews", json=payload)
        if response.status_code != 202:
            return {
                "status": "BLOCKED",
                "reason_code": "PUBLIC_CREATE_FAILED",
                "http_status": response.status_code,
                "preflight": preflight,
            }
        task_id = (response.json().get("data") or {}).get("task_id")
        if not isinstance(task_id, str):
            return {
                "status": "BLOCKED",
                "reason_code": "PUBLIC_CREATE_NO_TASK_ID",
                "preflight": preflight,
            }

        async with session_factory() as session:
            task = (
                await session.execute(select(CheckTask).where(CheckTask.id == task_id))
            ).scalar_one()
            task_file_rows = (
                await session.execute(
                    select(TaskFile)
                    .where(TaskFile.task_id == task_id)
                    .order_by(TaskFile.sort_order)
                )
            ).scalars().all()
            task_identity = {
                "source_task_id": task.source_task_id,
                "legacy_option_keys": sorted(
                    key
                    for key in (task.options or {})
                    if key.startswith("_") or "legacy" in key.casefold()
                ),
                "task_file_count": len(task_file_rows),
                "reference_file_count": sum(
                    row.role.value == "REFERENCE" for row in task_file_rows
                ),
                "file_ids_unique": len({row.id for row in task_file_rows})
                == len(task_file_rows),
            }
        if (
            task_identity["source_task_id"] is not None
            or task_identity["legacy_option_keys"]
            or task_identity["task_file_count"] != 5
            or task_identity["reference_file_count"] != 3
            or not task_identity["file_ids_unique"]
        ):
            return {
                "status": "BLOCKED",
                "reason_code": "PUBLIC_TASK_IDENTITY_INVALID",
                "task_id": task_id,
                "task_identity": task_identity,
                "preflight": preflight,
            }

        local = ParserRegistry(pdf_min_text_chars_per_page=settings.PDF_MIN_TEXT_CHARS_PER_PAGE)
        external = CachedExternalDocumentParser(
            TextInDocumentParser(
                settings,
                client=TextInDocumentParserClient(settings, transport=ocr_transport),
            ),
            SqlAlchemyDocumentParseCache(session_factory),
            settings,
        )
        document_router = DocumentParsingRouter(
            local=local,
            external=external,
            page_location_cache=SqlAlchemyPageLocationSidecarCache(session_factory),
            docx_page_location_enabled=True,
        )
        draft = DraftReviewWorkflowExecutor(
            settings,
            document_router=document_router,
            llm=OpenAIContractLlmClient(
                settings,
                transport=llm_transport,
                text_response_format_override="json_object",
                advice_response_format_override="json_object",
            ),
            checkpoint_store=SqlAlchemyExtractionCheckpointStore(session_factory),
        )
        runner = TimedWorkerRunner(
            settings,
            workflow=WorkflowRouter(settings, draft_review=draft),
            session_factory=session_factory,
        )
        runner_task = asyncio.create_task(runner.run_once())
        last_status: str | None = None
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            while not runner_task.done():
                try:
                    detail = await client.get(f"{api_base_url}/api/v1/tasks/{task_id}")
                    if detail.status_code == 200:
                        current = (detail.json().get("data") or {}).get("status")
                        if current != last_status:
                            last_status = current
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
        worker_claimed = await runner_task
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            detail_response = await client.get(f"{api_base_url}/api/v1/tasks/{task_id}")
            result_response = await client.get(f"{api_base_url}/api/v1/tasks/{task_id}/result")
        async with session_factory() as session:
            task = (
                await session.execute(select(CheckTask).where(CheckTask.id == task_id))
            ).scalar_one()
            task_file_rows = (
                await session.execute(
                    select(TaskFile)
                    .where(TaskFile.task_id == task_id)
                    .order_by(TaskFile.sort_order)
                )
            ).scalars().all()
            stored = (
                await session.execute(select(TaskResult).where(TaskResult.task_id == task_id))
            ).scalar_one_or_none()
        report: dict[str, Any] = {
            "status": task.status.value,
            "task_id": task_id,
            "stage": task.stage.value,
            "progress": task.progress,
            "worker_claimed": worker_claimed,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "preflight": preflight,
            "task_identity": {
                "source_task_id": task.source_task_id,
                "options_private_keys": sorted(
                    key for key in (task.options or {}) if key.startswith("_")
                ),
                "file_count": len(task_file_rows),
                "reference_file_count": sum(
                    row.role.value == "REFERENCE" for row in task_file_rows
                ),
            },
            "ocr_http_calls": ocr_transport.http_calls,
            "ocr_status_counts": dict(sorted(ocr_transport.statuses.items())),
            "llm_http_calls": llm_transport.http_calls,
            "llm_status_counts": dict(sorted(llm_transport.statuses.items())),
            "llm_finish_reasons": dict(sorted(llm_transport.finish_reasons.items())),
            "api_get_status": detail_response.status_code,
            "api_get_result_status": result_response.status_code,
            "stage_events": [stage for stage, _started in (runner.stage_events if runner else [])],
        }
        if stored is None:
            report["error_code"] = task.error_code
            report["first_failure"] = safe_error(task.error_details)
            return report
        result = stored.result
        from app.schemas.results import TaskResultData

        TaskResultData.model_validate(result)
        task_files = [
            {
                "file_id": row.id,
                "role": row.role.value,
                "file_name": row.file_name,
                "sha256": row.sha256,
            }
            for row in task_file_rows
        ]
        checkpoint_counts = await document_checkpoint_counts(session_factory, task_id, task_files)
        report["result"] = result_summary(
            result,
            task_files=task_files,
            page_sidecars=document_router.page_location_sidecars,
            checkpoint_counts=checkpoint_counts,
        )
        report["checkpoint_counts"] = checkpoint_counts
        report["acceptance"] = {
            "five_files": len(result.get("files", [])) == 5,
            "three_references": sum(
                file.get("role") == "REFERENCE" for file in result.get("files", [])
            )
            == 3,
            "public_page_coverage_complete": report["result"]["page_location_coverage"][
                "missing_evidence_count"
            ]
            == 0,
            "advice_non_empty_complete": report["result"]["advice"]["non_empty_count"]
            == len(result.get("risk_items", [])),
            "not_template_only": any(
                item.get("module_code") != "TEMPLATE_INTEGRITY"
                for item in result.get("risk_items", [])
                if isinstance(item, dict)
            ),
        }
        return report
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        await llm_transport.close_all()
        await ocr_transport.close_all()
        await engine.dispose()


async def run(args: argparse.Namespace) -> dict[str, Any]:
    base = Settings()
    settings = runtime_settings(base)
    git = git_preflight()
    inventory, inventory_error = file_inventory()
    report: dict[str, Any] = {
        "script": "draft_review_five_file_public_acceptance",
        "started_at": time.time(),
        "git_preflight": git,
        "file_inventory": inventory,
    }
    if not git["passed"]:
        report.update({"status": "BLOCKED", "reason_code": "GIT_PREFLIGHT_FAILED"})
        return report
    if inventory_error is not None:
        report.update({"status": "BLOCKED", "reason_code": inventory_error["failure_code"]})
        return report
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        services = await service_preflight(session_factory, args.api_base_url)
        caches = await preheat_missing_caches(session_factory, settings, inventory)
        report["service_preflight"] = services
        report["cache_preflight"] = caches
        report["runtime_configuration"] = {
            "llm_extraction_model": settings.LLM_EXTRACTION_MODEL,
            "llm_mapping_model": settings.LLM_EXTRACTION_MODEL,
            "llm_advice_model": settings.LLM_ADVICE_MODEL,
            "llm_response_format": settings.LLM_RESPONSE_FORMAT,
            "llm_native_structured_output": settings.LLM_NATIVE_STRUCTURED_OUTPUT,
            "text_response_format_override": "json_object",
            "advice_response_format_override": "json_object",
            "llm_max_concurrency": settings.LLM_MAX_CONCURRENCY,
            "llm_extraction_task_concurrency": settings.LLM_EXTRACTION_TASK_CONCURRENCY,
            "llm_http_retry_attempts": settings.LLM_HTTP_RETRY_ATTEMPTS,
            "ocr_http_retry_attempts": settings.OCR_HTTP_RETRY_ATTEMPTS,
            "docx_page_location_enabled": settings.DOCX_PAGE_LOCATION_ENABLED,
            "source_task_id": None,
        }
        if not services["passed"]:
            report.update({"status": "BLOCKED", "reason_code": "SERVICE_PREFLIGHT_FAILED"})
            return report
        if not caches["all_ocr_cached"] or not caches["all_docx_sidecars_cached"]:
            report.update({"status": "BLOCKED", "reason_code": "CACHE_PREFLIGHT_INCOMPLETE"})
            return report
        if args.require_worker_stopped and docker_worker_running():
            report.update({"status": "BLOCKED", "reason_code": "DOCKER_WORKER_STILL_RUNNING"})
            return report
        if args.preflight_only:
            report["status"] = "PREFLIGHT_PASSED"
            return report
        return {
            **report,
            **await create_and_execute(
                args.api_base_url,
                args.output,
                preflight=report,
                base_settings=base,
                inventory=inventory,
            ),
        }
    finally:
        await engine.dispose()


def docker_worker_running() -> bool:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", "contract-review-worker-1"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip().casefold() == "true"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--require-worker-stopped", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    if not arguments.preflight_only:
        acquire_lock(arguments.lock)
    report = asyncio.run(run(arguments))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if report.get("status") in {"SUCCEEDED", "PREFLIGHT_PASSED"} else 2)
