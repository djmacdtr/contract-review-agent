"""Run one safe FINAL_LOGICAL_V2 duplicate-cluster Canary.

The command reconstructs the real comparison from a local DOCX and the
persisted PDF OCR cache.  It only calls the LLM for clusters found in the
current comparison and never writes task, result, checkpoint, or cache state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
FILE_ROOT = REPO_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.adapters.document_parser.cached_parser import (  # noqa: E402
    SqlAlchemyDocumentParseCache,
)
from app.adapters.llm.openai_client import OpenAIContractLlmClient  # noqa: E402
from app.comparison.duplicate_clusters import (  # noqa: E402
    _decision_is_safe,
    _safe_decision,
    build_candidate_discovery_gold_audit,
    build_suspected_duplicate_clusters,
    select_canary_clusters,
)
from app.comparison.engine import CompareOptions, compare_documents  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.errors import WorkflowError  # noqa: E402
from app.documents.parsers import ParserRegistry  # noqa: E402
from scripts.final_compare_logical_dry_run import (  # noqa: E402
    _host_database_url,
    _load_cached_pdf,
    _local_file,
)

DEFAULT_BASELINE = FILE_ROOT / "04 合同素材文件/02 合同起草版本/融资租赁合同（回租）.docx"
DEFAULT_TARGET = FILE_ROOT / "04 合同素材文件/03 合同盖章版本/金坛东旭农业-融资租赁合同（回租）.pdf"


def empty_cluster_canary_result() -> dict[str, Any]:
    """Return the successful no-work outcome used by the real-input gate."""

    return {
        "status": "SKIPPED_NO_CANDIDATES",
        "failure_stage": None,
        "failure_code": None,
        "cluster_count": 0,
        "cluster_sizes": [],
        "selected_cluster_count": 0,
        "unselected_cluster_count": 0,
        "llm_calls": 0,
        "ocr_calls": 0,
        "database_writes": 0,
    }


def _safe_error(error: BaseException) -> dict[str, Any]:
    details = getattr(error, "validation_summary", None)
    result: dict[str, Any] = {"failure_code": getattr(error, "failure_code", None)}
    if getattr(error, "finish_reason", None):
        result["finish_reason"] = error.finish_reason
    if getattr(error, "response_metadata", None):
        result["response_metadata"] = {
            key: value
            for key, value in error.response_metadata.items()
            if key in {"content_chars", "code_fence", "json_error_position", "usage", "max_tokens"}
        }
    if isinstance(details, dict):
        result["validation_summary"] = {
            key: details[key]
            for key in ("error_count", "missing_fields", "invalid_fields")
            if key in details
        }
    return {key: value for key, value in result.items() if value not in (None, {}, [])}


async def run(args: argparse.Namespace) -> dict[str, Any]:
    base = Settings()
    settings = base.model_copy(
        update={
            "DATABASE_URL": _host_database_url(base.DATABASE_URL),
            "LLM_HTTP_RETRY_ATTEMPTS": 0,
            "LLM_MAX_CONCURRENCY": 1,
            "LLM_STRUCTURE_RETRY_ATTEMPTS": 0,
        }
    )
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        if not args.baseline.is_file() or not args.target.is_file():
            return {
                "status": "BLOCKED",
                "failure_stage": "CANARY_INPUT",
                "failure_code": "LOCAL_FILE_MISSING",
                "llm_calls": 0,
            }
        baseline = _local_file(
            args.baseline,
            file_id="canary_baseline",
            role="BASELINE",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        target = _local_file(
            args.target, file_id="canary_target", role="TARGET", mime="application/pdf"
        )
        try:
            baseline_document = await ParserRegistry().docx.parse(baseline)
        except WorkflowError as error:
            return {
                "status": "FAILED",
                "failure_stage": "LOCAL_DOCX_PARSE",
                "failure_code": error.code,
                "llm_calls": 0,
            }
        parse_cache = SqlAlchemyDocumentParseCache(factory)
        target_document, cache_status = await _load_cached_pdf(parse_cache, settings, target)
        if target_document is None:
            return {
                "status": "BLOCKED",
                "failure_stage": "PDF_CACHE_ONLY",
                "failure_code": "OCR_CACHE_MISS",
                "target_cache_status": cache_status,
                "llm_calls": 0,
            }
        comparison = compare_documents(
            baseline_document,
            target_document,
            CompareOptions(comparison_mode="FINAL_LOGICAL_V2"),
        )
        clusters = build_suspected_duplicate_clusters(
            comparison, baseline=baseline_document, target=target_document
        )
        sizes = sorted(len(cluster.candidate_ids) for cluster in clusters)
        discovery_failure = comparison.validation_metadata.get(
            "candidate_discovery", {}
        ).get("failure_code")
        if discovery_failure:
            return {
                "status": "BLOCKED",
                "failure_stage": "CANDIDATE_DISCOVERY_GATE",
                "failure_code": discovery_failure,
                "cluster_count": len(clusters),
                "cluster_sizes": sizes,
                "llm_calls": 0,
                "ocr_calls": 0,
                "database_writes": 0,
            }
        if getattr(args, "require_gold", False):
            try:
                manifest = json.loads(args.gold_manifest.read_text(encoding="utf-8"))
                gold_audit = build_candidate_discovery_gold_audit(
                    comparison,
                    clusters,
                    manifest,
                    baseline=baseline_document,
                    target=target_document,
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                gold_audit = {
                    "status": "FAILED",
                    "failure_code": "GOLD_MANIFEST_INVALID",
                }
            if gold_audit.get("status") != "PASSED":
                return {
                    "status": "BLOCKED",
                    "failure_stage": "CANDIDATE_GOLD_GATE",
                    "failure_code": gold_audit.get(
                        "failure_code", "CANDIDATE_GOLD_MISMATCH"
                    ),
                    "gold_audit": {
                        key: value
                        for key, value in gold_audit.items()
                        if key not in {"matched_groups"}
                    },
                    "cluster_count": len(clusters),
                    "cluster_sizes": sizes,
                    "llm_calls": 0,
                    "ocr_calls": 0,
                    "database_writes": 0,
                }
        if not clusters:
            if getattr(args, "require_candidates", False):
                return {
                    **empty_cluster_canary_result(),
                    "status": "FAILED",
                    "failure_stage": "CANDIDATE_DISCOVERY_GATE",
                    "failure_code": "EXPECTED_CANDIDATES_MISSING",
                }
            return empty_cluster_canary_result()
        selected_clusters, selection_error = select_canary_clusters(clusters)
        if selection_error:
            return {
                "status": "FAILED",
                "failure_stage": "CANARY_CATEGORY_GATE",
                "failure_code": selection_error,
                "cluster_count": len(clusters),
                "cluster_sizes": sizes,
                "selected_cluster_count": 0,
                "unselected_cluster_count": len(clusters),
                "canary_categories": [cluster.canary_category for cluster in clusters],
                "llm_calls": 0,
                "ocr_calls": 0,
                "database_writes": 0,
            }
        client = OpenAIContractLlmClient(settings)
        try:
            response = await client.validate_final_compare_duplicate_clusters(
                {"groups": [cluster.payload for cluster in selected_clusters]}
            )
        except Exception as error:  # noqa: BLE001 - safe external boundary
            return {
                "status": "FAILED",
                "failure_stage": "LLM_DUPLICATE_CLUSTER_CANARY",
                "failure_code": "LLM_DUPLICATE_CLUSTER_CANARY_FAILED",
                "error": _safe_error(error),
                "llm_calls": 1,
            }
        decisions = response.value.get("groups")
        response_key = "group_id"
        if not isinstance(decisions, list):
            decisions = response.value.get("clusters", [])
            response_key = "cluster_id"
        by_group = {cluster.group_id: cluster for cluster in selected_clusters}
        by_cluster = {cluster.cluster_id: cluster for cluster in selected_clusters}
        by_candidate = {
            str(diff.candidate_id): diff
            for diff in comparison.diff_items
            if diff.candidate_id
        }
        valid = len(decisions) == len(selected_clusters)
        convergence: dict[str, int] = {}
        if valid:
            for decision in decisions:
                group_id = decision.get(response_key)
                cluster = by_group.get(group_id) or by_cluster.get(group_id)
                candidate_ids = set(cluster.candidate_ids) if cluster else set()
                parsed_decision = _safe_decision(decision, cluster) if cluster else None
                complete = (
                    cluster is not None
                    and parsed_decision is not None
                    and _decision_is_safe(
                        parsed_decision,
                        cluster,
                        by_candidate,
                        baseline=baseline_document,
                        target=target_document,
                    )
                    and set(parsed_decision.get("candidate_ids", []))
                    == set(cluster.candidate_ids)
                )
                valid = valid and complete
                if cluster is not None:
                    convergence[cluster.group_id] = len(candidate_ids) if complete else 0
        return {
            "status": "SUCCEEDED" if valid else "FAILED",
            "failure_stage": None if valid else "CANARY_APPLICATION_GATE",
            "failure_code": None if valid else "LLM_DUPLICATE_CLUSTER_DECISION_INVALID",
            "cluster_count": len(clusters),
            "cluster_sizes": sizes,
            "selected_cluster_count": len(selected_clusters),
            "unselected_cluster_count": len(clusters) - len(selected_clusters),
            "convergence_counts": convergence,
            "llm_calls": 1,
            "request_attempts": response.request_attempts,
            "finish_reason": response.finish_reason,
            "response_format": response.response_format,
            "configured_model": response.configured_model,
            "actual_model": response.actual_model,
            "response_metadata": response.response_metadata or {},
        }
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument(
        "--require-candidates",
        action="store_true",
        help="第一组真实验收要求必须存在实际候选。",
    )
    parser.add_argument(
        "--require-gold",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="要求显式脱敏金标及本次候选拓扑先通过，才允许调用模型。",
    )
    parser.add_argument(
        "--gold-manifest",
        type=Path,
        default=REPO_ROOT / "tests/fixtures/final_compare_gold/first_pair_deidentified.json",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if report.get("status") in {"SUCCEEDED", "SKIPPED_NO_CANDIDATES"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
