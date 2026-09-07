"""Shared strict page-location recovery for DRAFT_REVIEW and FINAL_COMPARE."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from app.core.errors import WorkflowError
from app.core.reliability import RecoveryBudget, is_page_refreshable_error
from app.documents.page_locations import (
    apply_docx_page_location_sidecars,
    validate_public_page_coverage,
)
from app.services.downloader import DOCX_MIME, LocalFile

_LOCATION_CONTEXT_KEYS = frozenset(
    {
        "evidence",
        "location",
        "locations",
        "source_evidence",
        "evidence_locations",
        "sample_locations",
        "target_candidate",
        "candidates",
        "reference_results",
        "candidate",
        "baseline",
        "target",
    }
)
_PAGE_ANCHOR_KEYS = frozenset(
    {"target_anchor_before_page", "target_anchor_after_page"}
)
_FILE_ID_KEYS = frozenset({"file_id", "source_file_id"})
_LOCATION_SHAPE_KEYS = frozenset(
    {"paragraph_index", "table_index", "row", "column", "structure_id"}
)


def _strip_page_fields(
    value: Any,
    *,
    docx_file_ids: frozenset[str],
    location_context: bool = False,
    inherited_file_id: str | None = None,
) -> Any:
    """Copy logical evidence while removing physical pages only for DOCX."""

    if isinstance(value, list):
        return [
            _strip_page_fields(
                item,
                docx_file_ids=docx_file_ids,
                location_context=location_context,
                inherited_file_id=inherited_file_id,
            )
            for item in value
        ]
    if isinstance(value, dict):
        file_id = inherited_file_id
        for key in _FILE_ID_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                file_id = candidate
                break
        is_location = location_context or bool(_LOCATION_SHAPE_KEYS.intersection(value))
        is_docx_location = is_location and file_id in docx_file_ids
        return {
            key: _strip_page_fields(
                item,
                docx_file_ids=docx_file_ids,
                location_context=is_location or key in _LOCATION_CONTEXT_KEYS,
                inherited_file_id=file_id,
            )
            for key, item in value.items()
            if not (
                (key == "page" and is_docx_location)
                or (key in _PAGE_ANCHOR_KEYS and is_docx_location)
            )
        }
    return deepcopy(value)


def page_free_result_copy(
    result: dict[str, Any], *, docx_file_ids: set[str] | frozenset[str]
) -> dict[str, Any]:
    """Return the immutable logical-evidence source used for every retry."""

    return _strip_page_fields(result, docx_file_ids=frozenset(docx_file_ids))


def _failure_file_id(exc: BaseException) -> str | None:
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return None
    file_id = details.get("public_evidence_file_id") or details.get("file_id")
    if isinstance(file_id, str) and file_id:
        return file_id
    location = details.get("public_evidence_location")
    if isinstance(location, dict) and isinstance(location.get("file_id"), str):
        return location["file_id"]
    return None


def _page_failure_code(exc: BaseException) -> str | None:
    details = getattr(exc, "details", None)
    return details.get("failure_code") if isinstance(details, dict) else None


@dataclass(frozen=True)
class PageRecoveryOutput:
    result: dict[str, Any]
    documents: list[Any]
    sidecars: dict[str, Any]


def _sidecar_missing_error(missing: list[str]) -> WorkflowError:
    return WorkflowError(
        "DOCX_PAGE_LOCATION_INCOMPLETE",
        "DOCX 真实页码解析或映射未能可靠完成",
        details={
            "failure_stage": "PUBLIC_EVIDENCE_MAPPING",
            "failure_code": "SIDECAR_MISSING",
            "page_count": None,
            "external_detail_page_count": 0,
            "external_detail_count": 0,
            "local_structure_count": 0,
            "external_structure_count": 0,
            "candidate_mapping_count": 0,
            "unmapped_location_count": len(missing),
            "missing_file_count": len(missing),
        },
    )


async def recover_missing_page_sidecars(
    *,
    documents: list[Any],
    local_files: list[LocalFile],
    router: Any,
    sidecars: dict[str, Any],
    budget: RecoveryBudget,
    ocr_refresh_attempts: int,
    ocr_timeout_seconds: float = 600.0,
    progress_callback: Any | None = None,
    progress_stage: Any | None = None,
) -> PageRecoveryOutput:
    """Recover missing DOCX sidecars before a workflow enters business logic."""

    current_documents = list(documents)
    current_sidecars = dict(sidecars)

    docx_files = [
        file
        for file in local_files
        if file.detected_mime_type == DOCX_MIME and file.file_id not in current_sidecars
    ]
    if not docx_files:
        return PageRecoveryOutput({}, current_documents, current_sidecars)

    for local_file in docx_files:
        last_error: WorkflowError | None = None
        for refresh_ocr in (False, True):
            if refresh_ocr and int(ocr_refresh_attempts) < 1:
                break
            code = _page_failure_code(last_error) if last_error is not None else "SIDECAR_MISSING"
            if refresh_ocr:
                allowed = budget.allow_ocr_refresh(code or "SIDECAR_MISSING")
            else:
                allowed = budget.allow_page_sidecar_rebuild(code or "SIDECAR_MISSING")
            if not allowed:
                break
            if progress_callback is not None:
                await progress_callback(progress_stage, 35, "正在自动恢复（1/1）")
            try:
                try:
                    rebuilt = await router.rebuild_docx_page_location(
                        local_file,
                        refresh_ocr=refresh_ocr,
                        persist=False,
                        timeout_seconds=budget.safe_timeout(ocr_timeout_seconds),
                    )
                except TypeError:
                    rebuilt = await router.rebuild_docx_page_location(
                        local_file, refresh_ocr=refresh_ocr, persist=False
                    )
            except WorkflowError as exc:
                last_error = exc
                budget.record_failure(_page_failure_code(exc))
                if not is_page_refreshable_error(exc):
                    break
                continue

            current_documents = [
                rebuilt if getattr(document, "file_id", None) == local_file.file_id else document
                for document in current_documents
            ]
            current_sidecars = dict(getattr(router, "page_location_sidecars", current_sidecars))
            if local_file.file_id in current_sidecars:
                budget.stats.page_sidecar_rebuilt = True
                if refresh_ocr:
                    budget.stats.ocr_cache_refreshed = True
                break
            last_error = _sidecar_missing_error([local_file.file_id])
            budget.record_failure("SIDECAR_MISSING")

        if local_file.file_id not in current_sidecars:
            missing = [
                file.file_id
                for file in docx_files
                if file.file_id not in current_sidecars
            ]
            raise last_error or _sidecar_missing_error(missing)

    return PageRecoveryOutput({}, current_documents, current_sidecars)


async def enrich_result_with_page_recovery(
    result: dict[str, Any],
    *,
    documents: list[Any],
    local_files: list[LocalFile],
    router: Any,
    sidecars: dict[str, Any],
    budget: RecoveryBudget,
    ocr_refresh_attempts: int,
    ocr_timeout_seconds: float = 600.0,
    validate_coverage: bool = True,
    progress_callback: Any | None = None,
    progress_stage: Any | None = None,
) -> PageRecoveryOutput:
    """Enrich from a page-free copy and retry only an explicit page allowlist."""

    docx_file_ids = frozenset(
        file.file_id for file in local_files if file.detected_mime_type == DOCX_MIME
    )
    logical_result = page_free_result_copy(result, docx_file_ids=docx_file_ids)
    current_documents = list(documents)
    current_sidecars = dict(sidecars)

    async def commit_pending() -> None:
        commit = getattr(router, "commit_docx_page_location", None)
        pending = getattr(router, "_pending_page_documents", {})
        if commit is None or not isinstance(pending, dict):
            return
        for local_file in local_files:
            if local_file.file_id in pending:
                await commit(local_file)

    async def apply_and_validate(candidate: dict[str, Any]) -> None:
        await asyncio.to_thread(
            apply_docx_page_location_sidecars,
            candidate,
            current_sidecars,
            strict=True,
        )
        if validate_coverage:
            await asyncio.to_thread(
                validate_public_page_coverage, candidate, current_sidecars
            )

    try:
        await apply_and_validate(logical_result)
        await commit_pending()
        return PageRecoveryOutput(logical_result, current_documents, current_sidecars)
    except WorkflowError as exc:
        first_error = exc
        budget.record_failure(_page_failure_code(first_error))
        if not is_page_refreshable_error(first_error):
            raise

    assert first_error is not None

    file_id = _failure_file_id(first_error)
    local_file = next((item for item in local_files if item.file_id == file_id), None)
    if local_file is None or local_file.detected_mime_type != DOCX_MIME:
        raise first_error

    last_error: WorkflowError = first_error
    for refresh_ocr in (False, True):
        if refresh_ocr and int(ocr_refresh_attempts) < 1:
            break
        code = _page_failure_code(last_error) or "PUBLIC_LOCATION_UNMAPPED"
        if refresh_ocr:
            allowed = budget.allow_ocr_refresh(code)
        else:
            allowed = budget.allow_page_sidecar_rebuild(code)
        if not allowed:
            raise last_error
        if progress_callback is not None:
            await progress_callback(
                progress_stage,
                95,
                "正在校验并补全公开证据页码",
            )
        try:
            try:
                rebuilt = await router.rebuild_docx_page_location(
                    local_file,
                    refresh_ocr=refresh_ocr,
                    persist=False,
                    timeout_seconds=budget.safe_timeout(ocr_timeout_seconds),
                )
            except TypeError:
                # Keep small in-memory/test routers compatible with the
                # production router's optional timeout parameter.
                rebuilt = await router.rebuild_docx_page_location(
                    local_file, refresh_ocr=refresh_ocr, persist=False
                )
            current_documents = [
                rebuilt if getattr(document, "file_id", None) == file_id else document
                for document in current_documents
            ]
            current_sidecars = dict(getattr(router, "page_location_sidecars", current_sidecars))
            candidate = page_free_result_copy(logical_result, docx_file_ids=docx_file_ids)
            await apply_and_validate(candidate)
        except WorkflowError as recovery_error:
            last_error = recovery_error
            budget.record_failure(_page_failure_code(recovery_error))
            if not is_page_refreshable_error(recovery_error):
                raise
            continue
        if refresh_ocr:
            budget.stats.ocr_cache_refreshed = True
        budget.stats.page_sidecar_rebuilt = True
        commit = getattr(router, "commit_docx_page_location", None)
        if commit is not None:
            await commit(local_file)
        return PageRecoveryOutput(candidate, current_documents, current_sidecars)
    raise last_error
