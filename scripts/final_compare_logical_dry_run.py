"""Cache-only FINAL_LOGICAL_V2 replay for the signed-contract regression pair.

The command reconstructs comparison input from the local DOCX and the
persisted OCR document cache. It never calls OCR or an LLM and never writes
tasks, results, checkpoints, or other database state. A PDF cache miss is a
safe, explicit stop rather than a reason to fall back to a local parser.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
FILE_ROOT = REPO_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.adapters.document_parser.cached_parser import (  # noqa: E402
    CachedExternalDocumentParser,
    SqlAlchemyDocumentParseCache,
)
from app.comparison.duplicate_clusters import (  # noqa: E402
    _candidate_coordinate,
    _candidate_table_values,
    _formula_signature,
    apply_deterministic_final_compare_filters,
    build_candidate_discovery_audit,
    build_candidate_discovery_gold_audit,
    build_suspected_duplicate_clusters,
    build_v2_quality_audit,
    cluster_audit_summary,
    replay_final_compare_gold,
)
from app.comparison.engine import CompareOptions, compare_documents  # noqa: E402
from app.comparison.logical_v2 import logical_cell_count  # noqa: E402
from app.comparison.reliable import comparison_normalize  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.errors import WorkflowError  # noqa: E402
from app.db.models import CheckTask, TaskResult  # noqa: E402
from app.documents.models import ParsedDocument  # noqa: E402
from app.documents.page_location_cache import SqlAlchemyPageLocationSidecarCache  # noqa: E402
from app.documents.page_locations import (  # noqa: E402
    bind_docx_page_locations,
    validate_public_page_coverage,
)
from app.documents.parsers import ParserRegistry  # noqa: E402
from app.results.risk_model import (  # noqa: E402
    build_comparison_review_items,
    build_risk_items,
)
from app.schemas.results import TaskResultData  # noqa: E402
from app.services.downloader import DOCX_MIME, PDF_MIME, LocalFile  # noqa: E402

DEFAULT_TASK_ID = "tsk_01M1BBHY5424N69QRDFA8N96VZ"
DEFAULT_BASELINE = FILE_ROOT / "04 合同素材文件/02 合同起草版本/融资租赁合同（回租）.docx"
DEFAULT_TARGET = FILE_ROOT / "04 合同素材文件/03 合同盖章版本/金坛东旭农业-融资租赁合同（回租）.pdf"
DEFAULT_GOLD_MANIFEST = REPO_ROOT / "tests/fixtures/final_compare_gold/first_pair_deidentified.json"


def _host_database_url(value: str) -> str:
    url = make_url(value)
    if url.host == "postgres" and url.port == 5432:
        return url.set(host="127.0.0.1", port=15432).render_as_string(
            hide_password=False
        )
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_file(path: Path, *, file_id: str, role: str, mime: str) -> LocalFile:
    return LocalFile(
        file_id=file_id,
        role=role,
        file_name=path.name,
        safe_url=f"dry-run://{file_id}",
        path=path,
        file_size=path.stat().st_size,
        sha256=_sha256(path),
        detected_mime_type=mime,
    )


def _valid_cached_document(
    value: Any, expected_sha: str, file: LocalFile
) -> ParsedDocument | None:
    try:
        document = ParsedDocument.model_validate((value or {}).get("document"))
    except (AttributeError, TypeError, ValueError):
        return None
    if (
        document.sha256 != expected_sha
        or not document.blocks
        or not isinstance(document.page_count, int)
        or document.page_count < 1
    ):
        return None
    return CachedExternalDocumentParser._rebind(document, file)


def _dry_run_page_result(
    baseline: LocalFile,
    target: LocalFile,
    baseline_document: ParsedDocument,
    target_document: ParsedDocument,
    compared: Any,
) -> dict[str, Any]:
    """Materialize only the public evidence shape needed by the page gate."""

    is_v2 = compared.diagnostics.fallback_mode == "FINAL_LOGICAL_V2"
    public_diffs = (
        [item for item in compared.diff_items if item.validation_status == "CONFIRMED"]
        if is_v2
        else list(compared.diff_items)
    )
    return {
        "files": [
            {
                "file_id": document.file_id,
                "role": document.role,
                "file_name": document.file_name,
                "safe_url": f"dry-run://{document.file_id}",
                "sha256": document.sha256,
                "page_count": document.page_count,
            }
            for document in (baseline_document, target_document)
        ],
        "diff_items": [
            item.model_dump(mode="json") for item in public_diffs
        ],
        "risk_items": build_risk_items(
            public_diffs, module_code="VERSION_CHANGE"
        ),
        "review_items": (
            build_comparison_review_items(
                compared.diff_items, module_code="VERSION_CHANGE"
            )
            if is_v2
            else []
        ),
        "metadata": {"comparison_mode": compared.diagnostics.fallback_mode},
        "_input_file_ids": (baseline.file_id, target.file_id),
    }


def _safe_page_failure(error: WorkflowError) -> dict[str, Any]:
    details = error.details if isinstance(error.details, dict) else {}
    allowed = {
        "required_evidence_count",
        "covered_evidence_count",
        "missing_evidence_count",
        "page_count",
        "file_id",
        "public_evidence_file_id",
        "public_evidence_location",
    }
    return {key: details[key] for key in allowed if key in details}


def _safe_candidate_catalog(
    comparison: Any,
    *,
    baseline: ParsedDocument,
    target: ParsedDocument,
) -> list[dict[str, Any]]:
    """Return body-free candidate coordinates for local binding diagnostics."""

    def side_summary(side: Any, direction: str) -> dict[str, Any] | None:
        if side is None:
            return None
        normalized = comparison_normalize(side.text or "")[1]
        locations = []
        for location in side.locations or [side.location]:
            locations.append(
                {
                    "page": location.page,
                    "table_index": location.table_index,
                    "row": location.row,
                    "column": location.column,
                    "section": location.section,
                    "bbox": list(location.bbox or ()),
                }
            )
        return {
            "direction": direction,
            "text_chars": len(normalized),
            "text_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16],
            "locations": locations,
        }

    catalog: list[dict[str, Any]] = []
    for ordinal, diff in enumerate(comparison.diff_items, start=1):
        coordinate = _candidate_coordinate(
            diff, baseline=baseline, target=target
        )
        table_values = _candidate_table_values(diff, coordinate)
        formula_text = " ".join(
            side.text
            for side in (diff.baseline, diff.target)
            if side is not None
        )
        catalog.append(
            {
                "ordinal": ordinal,
                "candidate_id": diff.candidate_id,
                "diff_type": diff.diff_type,
                "validation_status": diff.validation_status,
                "coordinate": {
                    key: value
                    for key, value in coordinate.items()
                    if key in {
                        "kind",
                        "table_pair",
                        "fields",
                        "row_keys",
                        "logical_ids",
                        "chapters",
                        "anchors",
                    }
                },
                "table_value_digests": {
                    field: [
                        {
                            "chars": len(value),
                            "sha256": hashlib.sha256(
                                value.encode("utf-8")
                            ).hexdigest()[:16],
                        }
                        for value in sorted(values)
                    ]
                    for field, values in sorted(table_values.items())
                },
                "formula_signature": _formula_signature(formula_text),
                "baseline": side_summary(diff.baseline, "BASELINE"),
                "target": side_summary(diff.target, "TARGET"),
            }
        )
    return catalog


async def _load_cached_pdf(
    parse_cache: SqlAlchemyDocumentParseCache,
    settings: Settings,
    file: LocalFile,
) -> tuple[ParsedDocument | None, str]:
    cached_parser = CachedExternalDocumentParser(None, parse_cache, settings)
    cache_key = cached_parser._cache_key(mode="scan", include_stamp_images=True)
    cached = await parse_cache.load(file_sha256=file.sha256, cache_key=cache_key)
    document = _valid_cached_document(cached, file.sha256, file)
    return document, "HIT" if document is not None else "MISS"


async def _read_source(
    factory: async_sessionmaker[AsyncSession],
    task_id: str | None,
    baseline_path: Path,
    target_path: Path,
    settings: Settings,
    *,
    source_result_required: bool = True,
    require_candidates: bool = False,
    require_gold: bool = False,
) -> dict[str, Any]:
    for path in (baseline_path, target_path):
        if not path.is_file() or not path.stat().st_size:
            return {
                "status": "FAILED",
                "failure_stage": "DRY_RUN_SETUP",
                "failure_code": "LOCAL_FILE_MISSING",
            }

    baseline = _local_file(
        baseline_path, file_id="dryrun_baseline", role="BASELINE", mime=DOCX_MIME
    )
    target = _local_file(
        target_path, file_id="dryrun_target", role="TARGET", mime=PDF_MIME
    )
    parser = ParserRegistry()
    try:
        baseline_document = await parser.docx.parse(baseline)
    except WorkflowError as error:
        return {
            "status": "FAILED",
            "failure_stage": "LOCAL_DOCX_PARSE",
            "failure_code": error.code,
        }

    parse_cache = SqlAlchemyDocumentParseCache(factory)
    target_document, target_cache_status = await _load_cached_pdf(
        parse_cache, settings, target
    )
    if target_document is None:
        return {
            "status": "BLOCKED",
            "failure_stage": "PDF_CACHE_ONLY",
            "failure_code": "OCR_CACHE_MISS",
            "target_sha256": target.sha256,
            "target_cache_status": target_cache_status,
            "ocr_calls": 0,
            "llm_calls": 0,
            "database_writes": 0,
        }

    # A DOCX sidecar is not needed to compare logical cells, but using it when
    # present gives the replay the same physical-page bindings as production.
    page_cache = SqlAlchemyPageLocationSidecarCache(factory)
    sidecar = await page_cache.load(
        file_sha256=baseline.sha256, file_id=baseline.file_id
    )
    if sidecar is not None:
        await asyncio.to_thread(bind_docx_page_locations, baseline_document, sidecar)

    compared = compare_documents(
        baseline_document,
        target_document,
        CompareOptions(comparison_mode="FINAL_LOGICAL_V2"),
    )
    if not compared.diagnostics.reliable:
        return {
            "status": "FAILED",
            "failure_stage": "COMPARISON_RELIABILITY",
            "failure_code": "COMPARISON_UNRELIABLE",
            "v2_raw_candidate_count": compared.validation_stats.get(
                "raw_candidate_count", 0
            ),
            "ocr_calls": 0,
            "llm_calls": 0,
            "database_writes": 0,
        }

    raw_candidate_count = len(compared.diff_items)
    candidate_catalog = _safe_candidate_catalog(
        compared, baseline=baseline_document, target=target_document
    )
    clusters = build_suspected_duplicate_clusters(
        compared,
        baseline=baseline_document,
        target=target_document,
    )
    discovery_audit = build_candidate_discovery_audit(compared, clusters)
    cluster_summaries = cluster_audit_summary(
        clusters,
        comparison=compared,
        baseline=baseline_document,
        target=target_document,
    )
    compared = apply_deterministic_final_compare_filters(
        compared,
        baseline=baseline_document,
        target=target_document,
    )

    # The real-input gate is about the evidence that would be published, not
    # about every parser structure.  It remains entirely local and read-only.
    dry_run_result = _dry_run_page_result(
        baseline, target, baseline_document, target_document, compared
    )
    try:
        page_coverage = validate_public_page_coverage(
            dry_run_result,
            {baseline.file_id: sidecar} if sidecar is not None else {},
        )
    except WorkflowError as error:
        return {
            "status": "BLOCKED",
            "failure_stage": "PUBLIC_PAGE_EVIDENCE_AUDIT",
            "failure_code": error.code,
            "failure_details": _safe_page_failure(error),
            "suspected_cluster_count": len(clusters),
            "ocr_calls": 0,
            "llm_calls": 0,
            "database_writes": 0,
        }
    quality_audit = build_v2_quality_audit(
        compared, page_coverage=page_coverage
    )

    gold_audit: dict[str, Any] = {"status": "NOT_REQUESTED"}
    gold_replay: dict[str, Any] = {"status": "NOT_REQUESTED"}
    if require_gold:
        try:
            manifest = json.loads(DEFAULT_GOLD_MANIFEST.read_text(encoding="utf-8"))
            gold_audit = build_candidate_discovery_gold_audit(
                compared,
                clusters,
                manifest,
                baseline=baseline_document,
                target=target_document,
            )
            if gold_audit.get("status") == "PASSED":
                gold_replay = replay_final_compare_gold(
                    compared,
                    clusters,
                    gold_audit,
                    page_coverage=page_coverage,
                    documents=[baseline_document, target_document],
                )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            gold_audit = {
                "status": "FAILED",
                "failure_code": "GOLD_MANIFEST_INVALID",
            }

    if require_candidates and not clusters:
        return {
            "status": "FAILED",
            "failure_stage": "CANDIDATE_DISCOVERY_GATE",
            "failure_code": "EXPECTED_CANDIDATES_MISSING",
            "discovery_audit": discovery_audit,
            "gold_audit": gold_audit,
            "gold_replay": gold_replay,
            "quality_audit": quality_audit,
            "page_coverage": page_coverage,
            "ocr_calls": 0,
            "llm_calls": 0,
            "database_writes": 0,
        }
    if require_gold and (
        gold_audit.get("status") != "PASSED"
        or gold_replay.get("status") != "PASSED"
    ):
        return {
            "status": "BLOCKED",
            "failure_stage": (
                "CANDIDATE_GOLD_GATE"
                if gold_audit.get("status") != "PASSED"
                else "CANDIDATE_GOLD_REPLAY_GATE"
            ),
            "failure_code": (
                gold_audit.get("failure_code")
                or gold_replay.get("failure_code")
                or "CANDIDATE_GOLD_MISMATCH"
            ),
            "discovery_audit": discovery_audit,
            "gold_audit": gold_audit,
            "gold_replay": gold_replay,
            "quality_audit": quality_audit,
            "page_coverage": page_coverage,
            "ocr_calls": 0,
            "llm_calls": 0,
            "database_writes": 0,
        }

    old_report_diff_count = None
    task_status = None
    task_type = None
    if source_result_required:
        async with factory() as session:
            task = await session.get(CheckTask, task_id)
            stored = await session.get(TaskResult, task_id)
            if task is None or stored is None or not isinstance(stored.result, dict):
                return {
                    "status": "FAILED",
                    "failure_stage": "DRY_RUN_SETUP",
                    "failure_code": "SOURCE_RESULT_NOT_FOUND",
                }
            try:
                old_result = TaskResultData.model_validate(stored.result)
            except Exception:  # noqa: BLE001 - safe diagnostic boundary
                return {
                    "status": "FAILED",
                    "failure_stage": "DRY_RUN_RESULT_VALIDATION",
                    "failure_code": "RESULT_SCHEMA_INVALID",
                }
        old_report_diff_count = len(old_result.diff_items)
        task_status = task.status.value if hasattr(task.status, "value") else str(task.status)
        task_type = (
            task.task_type.value
            if hasattr(task.task_type, "value")
            else str(task.task_type)
        )

    stats = compared.validation_stats
    return {
        "status": "SUCCEEDED",
        "task_id": task_id,
        "task_status": task_status,
        "task_type": task_type,
        "comparison_mode": "FINAL_LOGICAL_V2",
        "baseline": {
            "file_name": baseline.file_name,
            "sha256": baseline.sha256,
            "logical_cell_count": logical_cell_count(baseline_document),
            "page_sidecar": "HIT" if sidecar is not None else "MISS",
        },
        "target": {
            "file_name": target.file_name,
            "sha256": target.sha256,
            "page_count": target_document.page_count,
            "ocr_cache": target_cache_status,
            "logical_cell_count": logical_cell_count(target_document),
        },
        "old_report_diff_count": old_report_diff_count,
        "v2_raw_candidate_count": raw_candidate_count,
        "logical_cell_count": logical_cell_count(baseline_document)
        + logical_cell_count(target_document),
        "rule_deduplicated_count": stats.get("rule_deduplicated_count", 0),
        "type_arbitration_count": stats.get("cross_type_merged_count", 0),
        "number_shift_merged_count": stats.get("number_shift_merged_count", 0),
        "consecutive_deletion_merge_count": stats.get(
            "consecutive_deletion_merge_count", 0
        ),
        "add_delete_recomposition_count": stats.get(
            "add_delete_recomposition_count", 0
        ),
        "table_mismatch_excluded_count": stats.get(
            "table_mismatch_excluded_count", 0
        ),
        "sparse_column_alignment_count": stats.get(
            "sparse_column_alignment_count", 0
        ),
        "vertical_merge_continuation_count": stats.get(
            "vertical_merge_continuation_count", 0
        ),
        "key_value_row_alignment_count": stats.get(
            "key_value_row_alignment_count", 0
        ),
        "confirmed_change_count": stats.get("confirmed_change_count", 0),
        "logical_area_merged_count": stats.get("logical_area_merged_count", 0),
        "llm_candidate_count": 0,
        "final_rule_candidate_count": len(compared.diff_items),
        "candidate_count_after_rules": len(compared.diff_items),
        "review_required_count": stats.get("review_required_count", 0),
        "suspected_cluster_count": len(clusters),
        "suspected_candidate_count": sum(len(item.candidate_ids) for item in clusters),
        "suspected_clusters": cluster_summaries,
        "logical_auto_merge_count": 0,
        "equivalent_filtered_count": stats.get("equivalent_filtered_count", 0),
        "boundary_noise_filtered_count": stats.get(
            "boundary_noise_filtered_count", 0
        ),
        "final_published_risk_count": stats.get(
            "final_published_risk_count", len(compared.diff_items)
        ),
        "llm_diff_adjudication_calls": 0,
        "equivalence_rejection_counts": discovery_audit.get(
            "equivalence_rejection_counts", {}
        ),
        "boundary_noise_rejection_count": discovery_audit.get(
            "boundary_noise_rejection_count", 0
        ),
        "canary_status": (
            "EXPECTED_CANDIDATES_MISSING"
            if require_candidates and not clusters
            else "SKIPPED_NO_CANDIDATES"
            if not clusters
            else "SKIPPED_LLM_ADJUDICATION_DISABLED"
        ),
        "discovery_audit": discovery_audit,
        "gold_audit": gold_audit,
        "gold_replay": gold_replay,
        "quality_audit": quality_audit,
        "page_coverage": page_coverage,
        "candidate_catalog": candidate_catalog,
        # Merge groups expose structural coordinates and text digests only;
        # raw contract text is deliberately not written to a diagnostic file.
        "merge_groups": compared.dedup_groups,
        "ocr_calls": 0,
        "llm_calls": 0,
        "database_writes": 0,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    engine = create_async_engine(
        _host_database_url(settings.DATABASE_URL), pool_pre_ping=True
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        try:
            return await _read_source(
                factory,
                args.task_id,
                args.baseline,
                args.target,
                settings,
                source_result_required=not getattr(args, "without_source_result", False),
                require_candidates=getattr(args, "require_candidates", False),
                require_gold=getattr(args, "require_gold", False),
            )
        except Exception as error:  # noqa: BLE001 - safe read-only diagnostic boundary
            return {
                "status": "FAILED",
                "failure_stage": "DRY_RUN_REPLAY",
                "failure_code": type(error).__name__,
                "ocr_calls": 0,
                "llm_calls": 0,
                "database_writes": 0,
            }
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", default=DEFAULT_TASK_ID)
    parser.add_argument(
        "--without-source-result",
        action="store_true",
        help="只比较当前文件，不读取历史 TaskResult。",
    )
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="保留显式语义；本脚本始终只读取本地 DOCX 与持久化 PDF 缓存。",
    )
    parser.add_argument(
        "--require-candidates",
        action="store_true",
        help="第一组真实验收要求必须发现候选。",
    )
    parser.add_argument(
        "--require-gold",
        action="store_true",
        help="要求脱敏人工金标的候选覆盖和分组数量通过。",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if report.get("status") == "SUCCEEDED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
