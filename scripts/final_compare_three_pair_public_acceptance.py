"""Run the three fixed FINAL_COMPARE pairs through the public API.

This is an operator acceptance harness.  It does not call retry or any
private task-creation method, and it keeps the task/result output limited to
safe counts and identifiers.  OCR cache warming is done before task creation;
the actual tasks use the normal ``WorkerRunner`` and
``FinalCompareWorkflowExecutor`` path.
"""

# The repository path is bootstrapped below so this file can be invoked as
# ``python scripts/<name>.py`` from any working directory.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
FILE_ROOT = REPO_ROOT.parent
OUTPUT_ROOT = REPO_ROOT / "docs" / "progress"
DEFAULT_OUTPUT = OUTPUT_ROOT / "20260830-000000_final-compare-three-pair.json"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.adapters.document_parser.cached_parser import (
    CachedExternalDocumentParser,
    SqlAlchemyDocumentParseCache,
)
from app.adapters.document_parser.textin_client import TextInDocumentParserClient
from app.adapters.document_parser.textin_parser import TextInDocumentParser
from app.adapters.llm.openai_client import (
    _ADVICE_MAX_OUTPUT_TOKENS,
    ADVICE_SYSTEM_PROMPT,
    OpenAIContractLlmClient,
    _validate_advice,
)
from app.adapters.llm.schemas import AdviceResponse
from app.core.config import Settings
from app.core.enums import TaskStatus
from app.core.errors import WorkflowError
from app.db.models import CheckTask, TaskFile, TaskResult
from app.documents.models import ParsedDocument
from app.documents.page_location_cache import SqlAlchemyPageLocationSidecarCache
from app.documents.page_locations import validate_public_page_coverage
from app.documents.parsers import ParserRegistry
from app.documents.router import DocumentParsingRouter
from app.services.downloader import DOCX_MIME, PDF_MIME, LocalFile
from app.worker.runner import WorkerRunner
from app.workflows.final_compare import FinalCompareWorkflowExecutor
from app.workflows.router import WorkflowRouter

PAIR_SPECS = (
    {
        "name": "融资租赁合同（回租）",
        "baseline": FILE_ROOT / "04 合同素材文件/02 合同起草版本/融资租赁合同（回租）.docx",
        "target": FILE_ROOT
        / "04 合同素材文件/03 合同盖章版本/金坛东旭农业-融资租赁合同（回租）.pdf",
    },
    {
        "name": "租赁物转让合同（回租）",
        "baseline": FILE_ROOT / "04 合同素材文件/02 合同起草版本/租赁物转让合同（回租）.docx",
        "target": FILE_ROOT
        / "04 合同素材文件/03 合同盖章版本/金坛东旭农业-租赁物转让合同（回租）.pdf",
    },
    {
        "name": "保证合同",
        "baseline": FILE_ROOT / "04 合同素材文件/02 合同起草版本/保证合同.docx",
        "target": FILE_ROOT / "04 合同素材文件/03 合同盖章版本/金坛东旭农业-保证合同.pdf",
    },
)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return None


class CountingTransport(httpx.AsyncBaseTransport):
    """Count calls and safe provider metadata without retaining response text."""

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
                decoded = json.loads(body)
                choices = decoded.get("choices") if isinstance(decoded, dict) else None
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    finish_reason = choices[0].get("finish_reason")
                    if isinstance(finish_reason, str):
                        self.finish_reasons[finish_reason] += 1
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

    async def _progress(self, task_id: str, stage: Any, progress: int, message: str) -> None:
        if not self.stage_events or self.stage_events[-1][0] != stage.value:
            self.stage_events.append((stage.value, time.monotonic()))
        await super()._progress(task_id, stage, progress, message)


class FinalAcceptanceLlm(OpenAIContractLlmClient):
    """Keep FINAL_COMPARE advice json_object and explicitly disable thinking."""

    async def generate_advice(self, payload: dict[str, Any]):
        return await self._structured_completion(
            model=self.settings.LLM_ADVICE_MODEL,
            system=ADVICE_SYSTEM_PROMPT,
            payload=payload,
            validator=_validate_advice,
            schema=AdviceResponse,
            response_format_override=self.advice_response_format_override,
            max_output_tokens=_ADVICE_MAX_OUTPUT_TOKENS,
            disable_thinking=True,
        )


def host_database_url(value: str) -> str:
    url = make_url(value)
    if url.host == "postgres" and url.port == 5432:
        return url.set(host="127.0.0.1", port=15432).render_as_string(hide_password=False)
    return value


def runtime_settings(base: Settings) -> Settings:
    return base.model_copy(
        update={
            "DATABASE_URL": host_database_url(base.DATABASE_URL),
            "TEMP_ROOT": str(REPO_ROOT / ".real-diagnostic-temp" / "final-compare-three-pair"),
            "ALLOW_HTTP_DOWNLOADS": True,
            "DOWNLOAD_HOST_ALLOWLIST": "127.0.0.1",
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
            "WORKER_MAX_CONCURRENT_TASKS": 1,
            "FINAL_COMPARE_LOGICAL_V2_ENABLED": True,
            "FINAL_COMPARE_EQUIVALENT_FILTER_ENABLED": True,
            "FINAL_COMPARE_LLM_ADJUDICATION_ENABLED": False,
        }
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_inventory() -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    inventory: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pair_index, pair in enumerate(PAIR_SPECS, start=1):
        for role, path in (("BASELINE", pair["baseline"]), ("TARGET", pair["target"])):
            if not path.is_file() or not path.stat().st_size:
                return inventory, {
                    "failure_code": "LOCAL_FILE_MISSING",
                    "pair_index": pair_index,
                    "role": role,
                }
            suffix = path.suffix.casefold()
            expected = ".docx" if role == "BASELINE" else ".pdf"
            if suffix != expected:
                return inventory, {
                    "failure_code": "FILE_EXTENSION_MISMATCH",
                    "pair_index": pair_index,
                    "role": role,
                }
            digest = sha256(path)
            if digest in seen:
                return inventory, {
                    "failure_code": "DUPLICATE_FILE_SHA256",
                    "pair_index": pair_index,
                    "role": role,
                }
            seen.add(digest)
            inventory.append(
                {
                    "pair_index": pair_index,
                    "pair_name": pair["name"],
                    "role": role,
                    "file_name": path.name,
                    "mime_type": DOCX_MIME if suffix == ".docx" else PDF_MIME,
                    "bytes": path.stat().st_size,
                    "sha256": digest,
                }
            )
    return inventory, None


def _docker_inspect(container: str) -> dict[str, Any]:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{json .State}}", container],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"available": False, "return_code": result.returncode}
    try:
        state = json.loads(result.stdout)
    except (TypeError, ValueError, json.JSONDecodeError):
        state = {}
    health = (state.get("Health") or {}).get("Status") if isinstance(state, dict) else None
    return {
        "available": True,
        "running": bool(state.get("Running")) if isinstance(state, dict) else False,
        "health": health,
    }


async def service_preflight(
    session_factory: async_sessionmaker[AsyncSession], api_base_url: str
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            health = await client.get(f"{api_base_url}/health")
            ready = await client.get(f"{api_base_url}/ready")
        result.update({"health_status": health.status_code, "ready_status": ready.status_code})
    except httpx.HTTPError as exc:
        result.update(
            {
                "health_status": None,
                "ready_status": None,
                "api_error_type": type(exc).__name__,
            }
        )
    try:
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
        result["active_task_count"] = active_count
    except Exception as exc:
        result.update({"active_task_count": None, "database_error_type": type(exc).__name__})
    result["docker"] = {
        name: _docker_inspect(container)
        for name, container in {
            "api": "contract-review-api-1",
            "postgres": "contract-review-postgres-1",
            "worker": "contract-review-worker-1",
        }.items()
    }
    docker = result["docker"]
    result["passed"] = bool(
        result.get("health_status") == 200
        and result.get("ready_status") == 200
        and result.get("active_task_count") == 0
        and all(item.get("available") and item.get("running") for item in docker.values())
    )
    return result


def _local_file(item: dict[str, Any], path: Path, *, file_id: str) -> LocalFile:
    return LocalFile(
        file_id=file_id,
        role=item["role"],
        file_name=path.name,
        safe_url=f"http://127.0.0.1/preflight/{file_id}",
        path=path,
        file_size=path.stat().st_size,
        sha256=item["sha256"],
        detected_mime_type=item["mime_type"],
    )


def _valid_cached_document(
    value: Any, expected_sha: str, *, require_stamp: bool
) -> ParsedDocument | None:
    try:
        document = ParsedDocument.model_validate((value or {}).get("document"))
    except (AttributeError, TypeError, ValueError):
        return None
    if (
        document.sha256 != expected_sha
        or not document.blocks
        or not isinstance(document.page_count, int)
    ):
        return None
    if require_stamp and not isinstance(document.stamp_images, list):
        return None
    return document


async def cache_audit(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    inventory: list[dict[str, Any]],
) -> dict[str, Any]:
    parse_cache = SqlAlchemyDocumentParseCache(session_factory)
    cached_parser = CachedExternalDocumentParser(None, parse_cache, settings)
    page_cache = SqlAlchemyPageLocationSidecarCache(session_factory)
    items: list[dict[str, Any]] = []
    for index, item in enumerate(inventory):
        is_docx = item["mime_type"] == DOCX_MIME
        mode = "auto" if is_docx else "scan"
        include_stamp_images = not is_docx
        cache_key = cached_parser._cache_key(
            mode=mode, include_stamp_images=include_stamp_images
        )
        cached = await parse_cache.load(file_sha256=item["sha256"], cache_key=cache_key)
        document = _valid_cached_document(
            cached, item["sha256"], require_stamp=include_stamp_images
        )
        sidecar = None
        if is_docx:
            sidecar = await page_cache.load(
                file_sha256=item["sha256"], file_id=f"preflight_{index:02d}"
            )
        items.append(
            {
                **{
                    key: item[key]
                    for key in (
                        "pair_index",
                        "pair_name",
                        "role",
                        "file_name",
                        "mime_type",
                        "sha256",
                    )
                },
                "ocr_cache_hit": document is not None,
                "page_sidecar_hit": sidecar is not None if is_docx else None,
                "page_count": (
                    sidecar.page_count
                    if sidecar is not None
                    else document.page_count if document is not None else None
                ),
                "stamp_image_count": len(document.stamp_images) if document is not None else 0,
                "cache_mode": mode,
                "include_stamp_images": include_stamp_images,
            }
        )
    return {
        "items": items,
        "ocr_cache_hits": sum(item["ocr_cache_hit"] for item in items),
        "ocr_cache_total": len(items),
        "docx_sidecar_hits": sum(item["page_sidecar_hit"] is True for item in items),
        "docx_sidecar_total": sum(item["mime_type"] == DOCX_MIME for item in items),
        "stamp_cache_hits": sum(
            item["ocr_cache_hit"] and item["include_stamp_images"] for item in items
        ),
        "all_ready": all(
            item["ocr_cache_hit"]
            and (item["mime_type"] != DOCX_MIME or item["page_sidecar_hit"])
            for item in items
        ),
    }


async def preheat_missing_caches(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    inventory: list[dict[str, Any]],
) -> dict[str, Any]:
    initial = await cache_audit(session_factory, settings, inventory)
    missing = [
        (index, item)
        for index, item in enumerate(initial["items"])
        if not item["ocr_cache_hit"]
        or (item["mime_type"] == DOCX_MIME and not item["page_sidecar_hit"])
    ]
    if not missing:
        initial.update({"preheat_http_calls": 0, "preheated": []})
        return initial

    local_parsers = ParserRegistry(pdf_min_text_chars_per_page=settings.PDF_MIN_TEXT_CHARS_PER_PAGE)
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
        local=local_parsers,
        external=external,
        page_location_cache=SqlAlchemyPageLocationSidecarCache(session_factory),
        docx_page_location_enabled=True,
    )
    path_by_key = {
        (pair_index, role): pair["baseline"] if role == "BASELINE" else pair["target"]
        for pair_index, pair in enumerate(PAIR_SPECS, start=1)
        for role in ("BASELINE", "TARGET")
    }

    preheated: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    try:
        for index, item in missing:
            path = path_by_key[(item["pair_index"], item["role"])]
            local_file = _local_file(item, path, file_id=f"preheat_{index:02d}")
            try:
                if item["mime_type"] == DOCX_MIME:
                    if not item["ocr_cache_hit"]:
                        await external.parse(local_file, mode="auto")
                    if not item["page_sidecar_hit"]:
                        await router.parse_draft_review_file(local_file)
                elif not item["ocr_cache_hit"]:
                    await external.parse_with_stamp_images(local_file, mode="scan")
                preheated.append(
                    {"pair_index": item["pair_index"], "role": item["role"]}
                )
            except Exception as exc:
                failures.append(
                    {
                        "pair_index": item["pair_index"],
                        "role": item["role"],
                        "file_name": item["file_name"],
                        "failure": {
                            "type": type(exc).__name__,
                            "code": exc.code if isinstance(exc, WorkflowError) else None,
                            "details": (
                                safe_error(exc.details)
                                if isinstance(exc, WorkflowError)
                                else {}
                            ),
                        },
                    }
                )
                # A failed file blocks only this pair.  Do not spend another
                # external call trying to complete the same pair in this run.
                break
        refreshed = await cache_audit(session_factory, settings, inventory)
        refreshed.update(
            {
                "preheat_http_calls": ocr_transport.http_calls,
                "preheated": preheated,
                "preheat_failures": failures,
            }
        )
        return refreshed
    finally:
        await ocr_transport.close_all()


async def run_ocr_canary(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    inventory: list[dict[str, Any]],
) -> dict[str, Any]:
    """Warm exactly the first missing cache entry, with no OCR retry."""

    initial = await cache_audit(session_factory, settings, inventory)
    missing = [
        item
        for item in initial["items"]
        if not item["ocr_cache_hit"]
        or (item["mime_type"] == DOCX_MIME and not item["page_sidecar_hit"])
    ]
    if not missing:
        return {
            "status": "SKIPPED_CACHED",
            "http_calls": 0,
            "cache_preflight": initial,
        }

    item = missing[0]
    path = next(
        path
        for pair in PAIR_SPECS
        if pair["name"] == item["pair_name"]
        for role, path in (("BASELINE", pair["baseline"]), ("TARGET", pair["target"]))
        if role == item["role"]
    )
    local_parsers = ParserRegistry(pdf_min_text_chars_per_page=settings.PDF_MIN_TEXT_CHARS_PER_PAGE)
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
        local=local_parsers,
        external=external,
        page_location_cache=SqlAlchemyPageLocationSidecarCache(session_factory),
        docx_page_location_enabled=True,
    )
    try:
        local_file = _local_file(item, path, file_id="ocr_canary_00")
        if item["mime_type"] == DOCX_MIME:
            if not item["ocr_cache_hit"]:
                await external.parse(local_file, mode="auto")
            if not item["page_sidecar_hit"]:
                await router.parse_draft_review_file(local_file)
        else:
            await external.parse_with_stamp_images(local_file, mode="scan")
        refreshed = await cache_audit(session_factory, settings, inventory)
        selected = next(
            candidate
            for candidate in refreshed["items"]
            if candidate["pair_index"] == item["pair_index"]
            and candidate["role"] == item["role"]
        )
        ready = selected["ocr_cache_hit"] and (
            selected["mime_type"] != DOCX_MIME or selected["page_sidecar_hit"]
        )
        return {
            "status": "SUCCEEDED" if ready else "FAILED",
            "selected": {
                "pair_index": item["pair_index"],
                "role": item["role"],
                "file_name": item["file_name"],
                "cache_mode": item["cache_mode"],
                "include_stamp_images": item["include_stamp_images"],
            },
            "http_calls": ocr_transport.http_calls,
            "status_counts": dict(sorted(ocr_transport.statuses.items())),
            "cache_preflight": refreshed,
        }
    except Exception as exc:
        return {
            "status": "FAILED",
            "selected": {
                "pair_index": item["pair_index"],
                "role": item["role"],
                "file_name": item["file_name"],
                "cache_mode": item["cache_mode"],
                "include_stamp_images": item["include_stamp_images"],
            },
            "http_calls": ocr_transport.http_calls,
            "status_counts": dict(sorted(ocr_transport.statuses.items())),
            "failure": {
                "type": type(exc).__name__,
                "code": exc.code if isinstance(exc, WorkflowError) else None,
                "details": safe_error(exc.details) if isinstance(exc, WorkflowError) else {},
            },
            "cache_preflight": initial,
        }
    finally:
        await ocr_transport.close_all()


def safe_error(details: Any) -> dict[str, Any]:
    if not isinstance(details, dict):
        return {}
    allowed = {
        "failure_stage",
        "failure_code",
        "underlying_failure_code",
        "component",
        "failure_kind",
        "attempts",
        "file_id",
        "batch_id",
        "batch_depth",
        "unit_count",
        "finish_reason",
        "http_status",
        "required_evidence_count",
        "covered_evidence_count",
        "missing_evidence_count",
        "public_evidence_file_id",
        "public_evidence_location",
    }
    return {key: details[key] for key in allowed if key in details}


def _transport_delta(transport: CountingTransport, before: int) -> dict[str, Any]:
    return {
        "http_calls": transport.http_calls - before,
        "status_counts": dict(sorted(transport.statuses.items())),
        "finish_reasons": dict(sorted(transport.finish_reasons.items())),
    }


def result_summary(
    result: dict[str, Any],
    *,
    sidecars: dict[str, Any],
) -> dict[str, Any]:
    from app.schemas.results import TaskResultData

    TaskResultData.model_validate(result)
    page_coverage = validate_public_page_coverage(result, sidecars)
    risks = [item for item in result.get("risk_items", []) if isinstance(item, dict)]
    model_runs = [
        item
        for item in result.get("metadata", {}).get("model_runs", [])
        if isinstance(item, dict)
    ]
    advice_runs = [item for item in model_runs if item.get("purpose") == "RISK_ADVICE"]
    advice_items = result.get("advice", {}).get("risk_advices", [])
    model_advice_count = len(advice_items) if advice_runs else 0
    return {
        "conclusion": result.get("conclusion"),
        "file_count": len(result.get("files", [])),
        "files": [
            {
                "role": item.get("role"),
                "file_name": item.get("file_name"),
                "sha256": item.get("sha256"),
                "page_count": item.get("page_count"),
                "parser_name": item.get("parser_name"),
            }
            for item in result.get("files", [])
            if isinstance(item, dict)
        ],
        "diff_count": len(result.get("diff_items", [])),
        "risk_count": len(risks),
        "passed_count": len(result.get("passed_checks", [])),
        "alignment_reliable": result.get("metadata", {})
        .get("comparison_diagnostics", {})
        .get("reliable"),
        "advice": {
            "risk_count": len(risks),
            "non_empty_count": sum(
                bool(str(item.get("analysis_advice") or "").strip()) for item in risks
            ),
            "model_count": model_advice_count,
            "fallback_count": max(0, len(risks) - model_advice_count),
            "model_runs": len(advice_runs),
        },
        "page_coverage": page_coverage,
        "stamp_image_count": sum(
            isinstance(item, dict) and bool(item.get("data_uri"))
            for item in result.get("stamp_images", [])
        ),
        "warning_codes": [
            item.get("code")
            for item in result.get("warnings", [])
            if isinstance(item, dict) and item.get("code")
        ],
    }


def docker_service_action(action: str, service: str | None = None) -> dict[str, Any]:
    command = ["docker", "compose", action]
    if service:
        command.append(service)
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return {"action": action, "service": service, "return_code": result.returncode}


def git_snapshot() -> dict[str, Any]:
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return {"head": head.stdout.strip(), "dirty_files": status.stdout.splitlines()}


async def run_one_pair(
    *,
    client: httpx.AsyncClient,
    api_base_url: str,
    pair_index: int,
    pair: dict[str, Any],
    urls: dict[tuple[int, str], str],
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    llm_transport: CountingTransport,
    ocr_transport: CountingTransport,
    deadline_seconds: float,
) -> dict[str, Any]:
    payload = {
        "client_reference_id": f"final-compare-three-pair-{int(time.time())}-{pair_index}",
        "baseline_file": {
            "url": urls[(pair_index, "BASELINE")],
            "file_name": pair["baseline"].name,
            "mime_type": DOCX_MIME,
        },
        "target_file": {
            "url": urls[(pair_index, "TARGET")],
            "file_name": pair["target"].name,
            "mime_type": PDF_MIME,
        },
        "options": {
            "ignore_formatting": True,
            "ignore_headers_footers": True,
            "numeric_sensitive": True,
        },
    }
    started = time.monotonic()
    response = await client.post(f"{api_base_url}/api/v1/final-comparisons", json=payload)
    summary: dict[str, Any] = {
        "pair_index": pair_index,
        "pair_name": pair["name"],
        "create_status": response.status_code,
    }
    if response.status_code != 202:
        summary["failure_code"] = "PUBLIC_CREATE_FAILED"
        return summary
    try:
        task_id = (response.json().get("data") or {}).get("task_id")
    except (TypeError, ValueError, json.JSONDecodeError):
        task_id = None
    if not isinstance(task_id, str):
        summary["failure_code"] = "PUBLIC_CREATE_NO_TASK_ID"
        return summary
    summary["task_id"] = task_id

    async with session_factory() as session:
        task = (
            await session.execute(select(CheckTask).where(CheckTask.id == task_id))
        ).scalar_one()
        rows = (
            await session.execute(
                select(TaskFile).where(TaskFile.task_id == task_id).order_by(TaskFile.sort_order)
            )
        ).scalars().all()
    summary["task_identity"] = {
        "source_task_id": task.source_task_id,
            "private_option_keys": sorted(
                key for key in (task.options or {}) if key.startswith("_")
            ),
        "file_count": len(rows),
        "file_ids_unique": len({row.id for row in rows}) == len(rows),
    }
    if (
        task.source_task_id is not None
        or summary["task_identity"]["private_option_keys"]
        or len(rows) != 2
        or not summary["task_identity"]["file_ids_unique"]
    ):
        summary["failure_code"] = "PUBLIC_TASK_IDENTITY_INVALID"
        return summary

    local_parsers = ParserRegistry(pdf_min_text_chars_per_page=settings.PDF_MIN_TEXT_CHARS_PER_PAGE)
    external = CachedExternalDocumentParser(
        TextInDocumentParser(
            settings,
            client=TextInDocumentParserClient(settings, transport=ocr_transport),
        ),
        SqlAlchemyDocumentParseCache(session_factory),
        settings,
    )
    document_router = DocumentParsingRouter(
        local=local_parsers,
        external=external,
        page_location_cache=SqlAlchemyPageLocationSidecarCache(session_factory),
        docx_page_location_enabled=True,
    )
    final_executor = FinalCompareWorkflowExecutor(
        settings,
        document_router=document_router,
        llm=FinalAcceptanceLlm(
            settings,
            transport=llm_transport,
            advice_response_format_override="json_object",
        ),
    )
    runner = TimedWorkerRunner(
        settings,
        workflow=WorkflowRouter(settings, final_compare=final_executor),
        session_factory=session_factory,
    )
    llm_before = llm_transport.http_calls
    ocr_before = ocr_transport.http_calls
    runner_task = asyncio.create_task(runner.run_once())
    last_state: tuple[str, str, int] | None = None
    deadline = time.monotonic() + deadline_seconds
    while not runner_task.done() and time.monotonic() < deadline:
        try:
            detail_response = await client.get(f"{api_base_url}/api/v1/tasks/{task_id}")
            if detail_response.status_code == 200:
                data = detail_response.json().get("data") or {}
                state = (data.get("status"), data.get("stage"), data.get("progress"))
                if state != last_state:
                    last_state = state
        except (httpx.HTTPError, TypeError, ValueError, json.JSONDecodeError):
            pass
        await asyncio.sleep(1)
    if not runner_task.done():
        runner_task.cancel()
        try:
            await runner_task
        except asyncio.CancelledError:
            pass
        summary.update({"status": "TIMEOUT", "failure_code": "TASK_TIMEOUT"})
        return summary
    worker_claimed = await runner_task
    detail_response = await client.get(f"{api_base_url}/api/v1/tasks/{task_id}")
    detail_data = detail_response.json().get("data") if detail_response.status_code == 200 else {}
    summary.update(
        {
            "status": detail_data.get("status"),
            "stage": detail_data.get("stage"),
            "progress": detail_data.get("progress"),
            "worker_claimed": worker_claimed,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "api_get_status": detail_response.status_code,
            "llm": _transport_delta(llm_transport, llm_before),
            "ocr": _transport_delta(ocr_transport, ocr_before),
            "stage_events": [stage for stage, _at in runner.stage_events],
        }
    )
    if summary["status"] != "SUCCEEDED":
        summary["failure_code"] = "TASK_FAILED"
        if isinstance(detail_data.get("error"), dict):
            summary["error"] = {
                "code": detail_data["error"].get("code"),
                "details": safe_error(detail_data["error"].get("details")),
            }
        return summary

    result_response = await client.get(f"{api_base_url}/api/v1/tasks/{task_id}/result")
    summary["api_get_result_status"] = result_response.status_code
    if result_response.status_code != 200:
        summary["failure_code"] = "PUBLIC_RESULT_GET_FAILED"
        return summary
    result = result_response.json().get("data")
    if not isinstance(result, dict):
        summary["failure_code"] = "PUBLIC_RESULT_INVALID"
        return summary
    try:
        summary["result"] = result_summary(
            result, sidecars=document_router.page_location_sidecars
        )
    except Exception as exc:
        summary["failure_code"] = "RESULT_ACCEPTANCE_FAILED"
        summary["result_validation_error_type"] = type(exc).__name__
    async with session_factory() as session:
        stored = (
            await session.execute(select(TaskResult).where(TaskResult.task_id == task_id))
        ).scalar_one_or_none()
    summary["result_persisted"] = stored is not None
    summary["console_task_path"] = f"/console/#/tasks/{task_id}"
    summary["console_report_path"] = f"/console/#/tasks/{task_id}/report"
    return summary


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# 三组 FINAL_COMPARE 放款阶段验收记录",
        "",
        f"- 时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- 状态：{report.get('status')}",
        f"- 当前提交：{report.get('git', {}).get('head')}",
        "- 任务创建：仅使用公开 `POST /api/v1/final-comparisons`，每组最多一次",
        "- 视觉验收：由用户在控制台完成",
        "",
        "## 固定配对结果",
        "",
        "| 组 | 任务 ID | 状态 | 差异 | 风险 | 通过 | OCR 调用 | LLM 调用 | 页码覆盖 | 印章图片 |",
        "|---:|---|---|---:|---:|---:|---:|---:|---|---:|",
    ]
    for item in report.get("tasks", []):
        result = item.get("result") or {}
        page = result.get("page_coverage") or {}
        lines.append(
            (
                "| {pair_index} | {task_id} | {status} | {diff} | {risk} | {passed} | "
                "{ocr} | {llm} | {covered}/{required} | {stamps} |"
            ).format(
                pair_index=item.get("pair_index"),
                task_id=item.get("task_id", "-"),
                status=item.get("status", item.get("failure_code", "-")),
                diff=result.get("diff_count", "-"),
                risk=result.get("risk_count", "-"),
                passed=result.get("passed_count", "-"),
                ocr=(item.get("ocr") or {}).get("http_calls", "-"),
                llm=(item.get("llm") or {}).get("http_calls", "-"),
                covered=page.get("covered_evidence_count", "-"),
                required=page.get("required_evidence_count", "-"),
                stamps=result.get("stamp_image_count", "-"),
            )
        )
    lines.extend(
        [
            "",
            "## 缓存与预检",
            "",
            "- 服务预检：`"
            + json.dumps(report.get("service_preflight", {}), ensure_ascii=False, sort_keys=True)
            + "`",
            "- 缓存预检：`"
            + json.dumps(report.get("cache_preflight", {}), ensure_ascii=False, sort_keys=True)
            + "`",
            "",
            "## 已知未完成项",
            "",
            "- 控制台页面视觉、印章图片切换和建议语气由用户人工抽查。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run(args: argparse.Namespace) -> dict[str, Any]:
    base = Settings()
    settings = runtime_settings(base)
    inventory, inventory_error = file_inventory()
    report: dict[str, Any] = {
        "script": "final_compare_three_pair_public_acceptance",
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "git": git_snapshot(),
        "inventory": inventory,
        "task_creation_method": "POST /api/v1/final-comparisons",
        "task_creation_count": 0,
    }
    if inventory_error is not None:
        report.update(
            {
                "status": "BLOCKED",
                "reason_code": inventory_error["failure_code"],
                "inventory_error": inventory_error,
            }
        )
        return report

    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    docker_worker_stopped = False
    docker_worker_restored = False
    try:
        services = await service_preflight(session_factory, args.api_base_url)
        report["service_preflight"] = services
        if not services["passed"]:
            report.update({"status": "BLOCKED", "reason_code": "SERVICE_PREFLIGHT_FAILED"})
            return report
        if args.ocr_canary_only:
            worker_state = services["docker"]["worker"]
            if not worker_state.get("running"):
                report.update({"status": "BLOCKED", "reason_code": "DOCKER_WORKER_NOT_RUNNING"})
                return report
            report["docker_worker_stop"] = docker_service_action("stop", "worker")
            docker_worker_stopped = report["docker_worker_stop"]["return_code"] == 0
            stopped = _docker_inspect("contract-review-worker-1")
            report["docker_worker_after_stop"] = stopped
            if stopped.get("running"):
                report.update({"status": "BLOCKED", "reason_code": "DOCKER_WORKER_STILL_RUNNING"})
                return report
            canary = await run_ocr_canary(session_factory, settings, inventory)
            report["ocr_canary"] = canary
            report["status"] = (
                "SUCCEEDED"
                if canary["status"] in {"SUCCEEDED", "SKIPPED_CACHED"}
                else "BLOCKED"
            )
            report["docker_worker_restore"] = docker_service_action("start", "worker")
            docker_worker_restored = report["docker_worker_restore"]["return_code"] == 0
            report["docker_worker_final"] = _docker_inspect("contract-review-worker-1")
            return report
        if args.preflight_only:
            caches = await cache_audit(session_factory, settings, inventory)
            report["cache_preflight"] = caches
            report["status"] = (
                "PREFLIGHT_PASSED" if caches["all_ready"] else "CACHE_PREFLIGHT_REQUIRED"
            )
            return report

        worker_state = services["docker"]["worker"]
        if not worker_state.get("running"):
            report.update({"status": "BLOCKED", "reason_code": "DOCKER_WORKER_NOT_RUNNING"})
            return report
        report["docker_worker_stop"] = docker_service_action("stop", "worker")
        docker_worker_stopped = report["docker_worker_stop"]["return_code"] == 0
        stopped = _docker_inspect("contract-review-worker-1")
        report["docker_worker_after_stop"] = stopped
        if stopped.get("running"):
            report.update({"status": "BLOCKED", "reason_code": "DOCKER_WORKER_STILL_RUNNING"})
            return report

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            lambda *args_, **kwargs: QuietHandler(*args_, directory=str(FILE_ROOT), **kwargs),
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        llm_transport = CountingTransport()
        ocr_transport = CountingTransport()
        try:
            port = int(server.server_address[1])
            urls = {
                (pair_index, role): (
                    f"http://127.0.0.1:{port}/"
                    f"{quote(path.relative_to(FILE_ROOT).as_posix(), safe='/')}"
                )
                for pair_index, pair in enumerate(PAIR_SPECS[: args.pair_limit], start=1)
                for role, path in (("BASELINE", pair["baseline"]), ("TARGET", pair["target"]))
            }
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                for pair_index, pair in enumerate(PAIR_SPECS[: args.pair_limit], start=1):
                    if pair_index > 1:
                        health = await client.get(f"{args.api_base_url}/health")
                        if health.status_code != 200:
                            report["stop_reason"] = "SHARED_INFRASTRUCTURE_UNAVAILABLE"
                            break
                    pair_inventory = [
                        item for item in inventory if item["pair_index"] == pair_index
                    ]
                    pair_cache = await preheat_missing_caches(
                        session_factory, settings, pair_inventory
                    )
                    report.setdefault("cache_preflight_by_pair", {})[str(pair_index)] = (
                        pair_cache
                    )
                    if not pair_cache["all_ready"]:
                        report.setdefault("tasks", []).append(
                            {
                                "pair_index": pair_index,
                                "pair_name": pair["name"],
                                "status": "BLOCKED",
                                "failure_code": "CACHE_PREFLIGHT_INCOMPLETE",
                                "cache_preflight": pair_cache,
                            }
                        )
                        report["stop_reason"] = "FIRST_PAIR_CACHE_PREFLIGHT_FAILED"
                        break
                    item = await run_one_pair(
                        client=client,
                        api_base_url=args.api_base_url,
                        pair_index=pair_index,
                        pair=pair,
                        urls=urls,
                        settings=settings,
                        session_factory=session_factory,
                        llm_transport=llm_transport,
                        ocr_transport=ocr_transport,
                        deadline_seconds=args.task_timeout,
                    )
                    report.setdefault("tasks", []).append(item)
                    report["task_creation_count"] += int(item.get("create_status") == 202)
                    if item.get("status") != "SUCCEEDED":
                        report["stop_reason"] = "FIRST_FAILED_PAIR"
                        break
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
            await llm_transport.close_all()
            await ocr_transport.close_all()
            report["docker_worker_restore"] = docker_service_action("start", "worker")
            docker_worker_restored = report["docker_worker_restore"]["return_code"] == 0
            report["docker_worker_final"] = _docker_inspect("contract-review-worker-1")

        report["cache_preflight"] = report.get("cache_preflight_by_pair", {})
        tasks = report.get("tasks", [])
        report["status"] = (
            "SUCCEEDED"
            if len(tasks) == args.pair_limit
            and all(item.get("status") == "SUCCEEDED" for item in tasks)
            else "PARTIAL" if tasks else "BLOCKED"
        )
        return report
    finally:
        if docker_worker_stopped and not docker_worker_restored:
            report["docker_worker_restore"] = docker_service_action("start", "worker")
            report["docker_worker_final"] = _docker_inspect("contract-review-worker-1")
        await engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--ocr-canary-only", action="store_true")
    parser.add_argument(
        "--pair-limit",
        type=int,
        choices=range(1, len(PAIR_SPECS) + 1),
        default=len(PAIR_SPECS),
        help="本次最多串行执行的配对数量；正式收口首轮使用 1。",
    )
    parser.add_argument("--task-timeout", type=float, default=900.0)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    result = asyncio.run(run(arguments))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for task in result.get("tasks", []):
        pair_index = task.get("pair_index", "unknown")
        pair_output = arguments.output.with_name(
            f"{arguments.output.stem}-pair-{pair_index}{arguments.output.suffix}"
        )
        pair_output.write_text(
            json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    markdown_output = arguments.markdown_output or arguments.output.with_suffix(".md")
    write_markdown(markdown_output, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if result.get("status") in {"SUCCEEDED", "PREFLIGHT_PASSED"} else 2)
