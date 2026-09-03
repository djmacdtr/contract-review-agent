"""One-time manual capture of FINAL_COMPARE V2 gold fingerprints.

The historical result ordinals are an operator-reviewed input only.  Ordinary
dry-runs and tests never call this module and never rewrite the gold manifest.
The output contains hashes and topology metadata, not contract text.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
FILE_ROOT = REPO_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.adapters.document_parser.cached_parser import (  # noqa: E402
    SqlAlchemyDocumentParseCache,
)
from app.comparison.duplicate_clusters import (  # noqa: E402
    build_suspected_duplicate_clusters,
    candidate_topology_fingerprint,
)
from app.comparison.engine import CompareOptions, compare_documents  # noqa: E402
from app.comparison.reliable import comparison_normalize  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.enums import TaskStatus, TaskType  # noqa: E402
from app.db.models import CheckTask, TaskFile, TaskResult  # noqa: E402
from app.documents.page_location_cache import (  # noqa: E402
    SqlAlchemyPageLocationSidecarCache,
)
from app.documents.page_locations import bind_docx_page_locations  # noqa: E402
from app.documents.parsers import ParserRegistry  # noqa: E402
from app.schemas.results import TaskResultData  # noqa: E402
from scripts.final_compare_logical_dry_run import (  # noqa: E402
    _host_database_url,
    _load_cached_pdf,
    _local_file,
)

DEFAULT_MANIFEST = REPO_ROOT / "tests/fixtures/final_compare_gold/first_pair_deidentified.json"
DEFAULT_OUTPUT = REPO_ROOT / "tests/fixtures/final_compare_gold/first_pair_deidentified.json"
DEFAULT_HISTORICAL_CATALOG = (
    REPO_ROOT / ".real-diagnostic-temp/continued-dry-run-v18.json"
)
DEFAULT_LOGICAL_GROUP_BINDINGS: Path | None = None
DEFAULT_BASELINE = FILE_ROOT / "04 合同素材文件/02 合同起草版本/融资租赁合同（回租）.docx"
DEFAULT_TARGET = FILE_ROOT / "04 合同素材文件/03 合同盖章版本/金坛东旭农业-融资租赁合同（回租）.pdf"

MANUAL_GROUPS: dict[str, tuple[int, ...]] = {
    "gold_equivalent_formula": (29, 30, 31),
    "gold_equivalent_rent_01": (75, 88),
    "gold_equivalent_rent_02": (76, 89),
    "gold_equivalent_rent_03": (77, 90),
    "gold_equivalent_rent_04": (78, 91),
    "gold_boundary_noise_01": (1,),
}


def _safe_error(code: str, **details: Any) -> dict[str, Any]:
    return {
        "status": "FAILED",
        "failure_code": code,
        **{key: value for key, value in details.items() if value not in (None, [], {})},
        "contract_text_saved": False,
    }


def _stable_location_sort_key(value: tuple[Any, ...]) -> str:
    """Sort redacted location tuples without comparing nullable fields."""

    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _manifest_digest(manifest: dict[str, Any]) -> str:
    value = copy.deepcopy(manifest)
    value.pop("manifest_sha256", None)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _candidate_binding_signature(diff: Any) -> tuple[Any, Any]:
    """Return a body-free key for binding an audited ordinal to current V2."""

    def side_key(side: Any, direction: str) -> tuple[Any, ...] | None:
        if side is None:
            return None
        normalized = comparison_normalize(side.text or "")[1]
        locations = []
        for location in side.locations or [side.location]:
            locations.append(
                (
                    location.page,
                    location.table_index,
                    location.row,
                    location.column,
                    location.section,
                    tuple(location.bbox or ()),
                )
            )
        return (
            direction,
            hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16],
            tuple(sorted(locations, key=_stable_location_sort_key)),
        )

    return (
        side_key(diff.baseline, "BASELINE"),
        side_key(diff.target, "TARGET"),
    )


def _historical_catalog_binding_signature(item: dict[str, Any]) -> tuple[Any, Any]:
    """Convert a body-free dry-run catalog row to the current binding key."""

    def side_key(value: Any, direction: str) -> tuple[Any, ...] | None:
        if not isinstance(value, dict):
            return None
        locations = []
        for location in value.get("locations", []):
            if not isinstance(location, dict):
                continue
            locations.append(
                (
                    location.get("page"),
                    location.get("table_index"),
                    location.get("row"),
                    location.get("column"),
                    location.get("section"),
                    tuple(location.get("bbox") or ()),
                )
            )
        digest = value.get("text_sha256")
        if not isinstance(digest, str):
            return None
        return direction, digest, tuple(
            sorted(locations, key=_stable_location_sort_key)
        )

    return (
        side_key(item.get("baseline"), "BASELINE"),
        side_key(item.get("target"), "TARGET"),
    )


def _load_historical_candidate_catalog(
    path: Path, *, source_task_id: str
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Load the reviewed ordinal snapshot without accepting source-result order."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None, _safe_error("HISTORICAL_CANDIDATE_CATALOG_INVALID")
    if not isinstance(payload, dict):
        return None, _safe_error("HISTORICAL_CANDIDATE_CATALOG_INVALID")
    catalog = payload.get("candidate_catalog")
    if not isinstance(catalog, list) or not all(
        isinstance(item, dict) for item in catalog
    ):
        return None, _safe_error("HISTORICAL_CANDIDATE_CATALOG_INVALID")
    catalog_task_id = payload.get("task_id")
    if catalog_task_id and catalog_task_id != source_task_id:
        return None, _safe_error("HISTORICAL_CANDIDATE_CATALOG_TASK_MISMATCH")
    return catalog, None


def _binding_digest(signature: tuple[Any, Any]) -> str:
    payload = json.dumps(signature, ensure_ascii=False, sort_keys=True, default=list)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _load_manual_logical_group_bindings(
    path: Path | None,
    *,
    source_task_id: str,
    expected_groups: list[dict[str, Any]],
) -> tuple[dict[str, tuple[int, ...]] | None, dict[str, Any] | None]:
    """Load the operator-reviewed ordinal sets used for logical gold capture.

    The ordinal sets are a one-time manual assertion.  They are deliberately
    kept outside the manifest so an ordinary capture cannot infer the gold
    groups from the groups it just discovered.
    """

    if path is None:
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None, _safe_error("MANUAL_LOGICAL_GROUP_BINDING_INVALID")
    if not isinstance(payload, dict):
        return None, _safe_error("MANUAL_LOGICAL_GROUP_BINDING_INVALID")
    if payload.get("source_task_id") != source_task_id:
        return None, _safe_error("MANUAL_LOGICAL_GROUP_BINDING_TASK_MISMATCH")
    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, list) or any(
        not isinstance(item, dict) for item in raw_groups
    ):
        return None, _safe_error("MANUAL_LOGICAL_GROUP_BINDING_INVALID")
    expected_ids = {
        str(item.get("gold_group_id"))
        for item in expected_groups
        if item.get("gold_group_id")
    }
    actual_ids = {
        str(item.get("gold_group_id"))
        for item in raw_groups
        if item.get("gold_group_id")
    }
    if actual_ids != expected_ids or len(raw_groups) != len(actual_ids):
        return None, _safe_error(
            "MANUAL_LOGICAL_GROUP_BINDING_SET_MISMATCH",
            expected_group_count=len(expected_ids),
            actual_group_count=len(actual_ids),
        )

    expected_counts = {
        str(item["gold_group_id"]): int(item["fragment_count"])
        for item in expected_groups
    }
    bindings: dict[str, tuple[int, ...]] = {}
    used_ordinals: set[int] = set()
    for item in raw_groups:
        group_id = str(item.get("gold_group_id"))
        raw_ordinals = item.get("source_ordinals")
        if (
            not isinstance(raw_ordinals, list)
            or not raw_ordinals
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 1
                for value in raw_ordinals
            )
            or len(raw_ordinals) != expected_counts.get(group_id, -1)
            or len(set(raw_ordinals)) != len(raw_ordinals)
            or used_ordinals.intersection(raw_ordinals)
        ):
            return None, _safe_error(
                "MANUAL_LOGICAL_GROUP_BINDING_INVALID",
                gold_group_id=group_id,
            )
        normalized_ordinals = tuple(raw_ordinals)
        bindings[group_id] = normalized_ordinals
        used_ordinals.update(normalized_ordinals)
    return bindings, None


async def capture(args: argparse.Namespace) -> dict[str, Any]:
    for path in (args.baseline, args.target):
        if not path.is_file() or not path.stat().st_size:
            return _safe_error("LOCAL_FILE_MISSING")

    base = Settings()
    settings = base.model_copy(update={"DATABASE_URL": _host_database_url(base.DATABASE_URL)})
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        try:
            async with factory() as session:
                task = await session.get(CheckTask, args.source_task_id)
                stored = await session.get(TaskResult, args.source_task_id)
                files = list(
                    (
                        await session.execute(
                            select(TaskFile).where(
                                TaskFile.task_id == args.source_task_id
                            )
                        )
                    ).scalars()
                )
                if task is None or stored is None:
                    return _safe_error("SOURCE_TASK_NOT_FOUND")
                if (
                    task.status != TaskStatus.SUCCEEDED
                    or task.task_type != TaskType.FINAL_COMPARE
                ):
                    return _safe_error("SOURCE_TASK_NOT_ELIGIBLE")
                try:
                    source_result = TaskResultData.model_validate(stored.result)
                except Exception:  # noqa: BLE001 - safe capture boundary
                    return _safe_error("SOURCE_RESULT_SCHEMA_INVALID")
        except Exception as error:  # noqa: BLE001 - no database details escape
            return _safe_error(
                "CAPTURE_DATABASE_UNAVAILABLE",
                exception_type=type(error).__name__,
            )

        roles = {
            str(item.role.value if hasattr(item.role, "value") else item.role): item
            for item in files
        }
        baseline_file = roles.get("BASELINE")
        target_file = roles.get("TARGET")
        if baseline_file is None or target_file is None:
            return _safe_error("SOURCE_FILE_ROLES_MISSING")

        baseline_local = _local_file(
            args.baseline,
            file_id="capture_baseline",
            role="BASELINE",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        target_local = _local_file(
            args.target, file_id="capture_target", role="TARGET", mime="application/pdf"
        )
        if (
            baseline_file.sha256 != baseline_local.sha256
            or target_file.sha256 != target_local.sha256
        ):
            return _safe_error(
                "SOURCE_FILE_SHA_MISMATCH",
                baseline_sha256=baseline_local.sha256,
                target_sha256=target_local.sha256,
            )

        try:
            baseline_document = await ParserRegistry().docx.parse(baseline_local)
            target_document, cache_status = await _load_cached_pdf(
                SqlAlchemyDocumentParseCache(factory), settings, target_local
            )
        except Exception as error:  # noqa: BLE001 - no body is returned
            return _safe_error("CAPTURE_PARSE_SETUP_ERROR", exception_type=type(error).__name__)
        if target_document is None:
            return _safe_error("OCR_CACHE_MISS", target_cache_status=cache_status)
        page_sidecar = await SqlAlchemyPageLocationSidecarCache(factory).load(
            file_sha256=baseline_local.sha256,
            file_id=baseline_local.file_id,
        )
        if page_sidecar is None:
            return _safe_error("DOCX_PAGE_CACHE_MISS")
        await asyncio.to_thread(
            bind_docx_page_locations, baseline_document, page_sidecar
        )

        try:
            current_comparison = compare_documents(
                baseline_document,
                target_document,
                CompareOptions(comparison_mode="FINAL_LOGICAL_V2"),
            )
            current_clusters = build_suspected_duplicate_clusters(
                current_comparison,
                baseline=baseline_document,
                target=target_document,
            )
        except Exception as error:  # noqa: BLE001 - safe capture boundary
            return _safe_error(
                "CURRENT_CANDIDATE_REBUILD_ERROR",
                exception_type=type(error).__name__,
            )
        current_diffs = list(current_comparison.diff_items)
        if len(current_diffs) != 93:
            return _safe_error(
                "CURRENT_CANDIDATE_COUNT_UNEXPECTED",
                current_candidate_count=len(current_diffs),
            )
        try:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return _safe_error("GOLD_MANIFEST_INVALID")
        logical_gold = manifest.get("logical_gold")
        if not isinstance(logical_gold, dict):
            return _safe_error("GOLD_MANIFEST_INVALID")
        expected_logical_groups = [
            item
            for item in logical_gold.get("fragment_groups", [])
            if isinstance(item, dict)
        ]
        logical_bindings, logical_binding_error = _load_manual_logical_group_bindings(
            getattr(args, "logical_groups", DEFAULT_LOGICAL_GROUP_BINDINGS),
            source_task_id=args.source_task_id,
            expected_groups=expected_logical_groups,
        )
        if logical_binding_error is not None:
            return logical_binding_error
        historical_catalog, catalog_error = _load_historical_candidate_catalog(
            args.historical_catalog, source_task_id=args.source_task_id
        )
        if catalog_error is not None:
            return catalog_error
        assert historical_catalog is not None
        required_ordinals = {
            ordinal
            for numbers in MANUAL_GROUPS.values()
            for ordinal in numbers
        } | {
            ordinal
            for numbers in (logical_bindings or {}).values()
            for ordinal in numbers
        }
        if required_ordinals and max(required_ordinals) > len(historical_catalog):
            return _safe_error(
                "MANUAL_ORDINAL_OUT_OF_RANGE",
                historical_candidate_count=len(historical_catalog),
            )
        current_by_signature: dict[tuple[Any, Any], list[Any]] = {}
        for current_diff in current_diffs:
            current_by_signature.setdefault(
                _candidate_binding_signature(current_diff), []
            ).append(current_diff)
        binding_audit: list[dict[str, Any]] = []
        current_by_ordinal: dict[int, Any] = {}
        bound_current_ids: set[str] = set()
        for ordinal in sorted(required_ordinals):
            historical_item = historical_catalog[ordinal - 1]
            historical_signature = _historical_catalog_binding_signature(
                historical_item
            )
            matches = current_by_signature.get(
                historical_signature, []
            )
            audit_item: dict[str, Any] = {
                "historical_ordinal": ordinal,
                "source_ordinal": ordinal,
                "match_count": len(matches),
                "source_binding_digest": _binding_digest(
                    historical_signature
                ),
            }
            if len(matches) == 1:
                current_diff = matches[0]
                current_id = str(current_diff.candidate_id)
                if current_id in bound_current_ids:
                    audit_item["duplicate_current_candidate"] = True
                else:
                    bound_current_ids.add(current_id)
                    current_by_ordinal[ordinal] = current_diff
                    audit_item[
                        "current_topology_fingerprint"
                    ] = candidate_topology_fingerprint(
                        [current_id],
                        {current_id: current_diff},
                        baseline=baseline_document,
                        target=target_document,
                    )
            binding_audit.append(audit_item)
        ambiguous = [
            item
            for item in binding_audit
            if item["match_count"] != 1 or item.get("duplicate_current_candidate")
        ]
        if ambiguous:
            return _safe_error(
                "MANUAL_CANDIDATE_BINDING_AMBIGUOUS",
                current_candidate_count=len(current_diffs),
                historical_candidate_count=len(historical_catalog),
                binding_audit=binding_audit,
            )
        current_cluster_by_candidates: dict[frozenset[str], list[Any]] = {}
        for cluster in current_clusters:
            current_cluster_by_candidates.setdefault(
                frozenset(cluster.candidate_ids), []
            ).append(cluster)
        current_boundary_ids = set(
            current_comparison.validation_metadata.get("candidate_discovery", {}).get(
                "boundary_noise_candidate_ids", []
            )
        )
        if logical_bindings is None and getattr(args, "write", False):
            return _safe_error(
                "MANUAL_LOGICAL_GROUP_BINDING_REQUIRED",
                current_candidate_count=len(current_diffs),
                current_cluster_count=len(current_clusters),
                binding_audit=binding_audit,
            )

        group_entries = {
            str(item.get("gold_group_id") or item.get("gold_case_id")): item
            for item in [
                *logical_gold.get("fragment_groups", []),
                *logical_gold.get("equivalent_groups", []),
                *logical_gold.get("boundary_noise", []),
            ]
            if isinstance(item, dict)
        }
        captured: dict[str, dict[str, Any]] = {}
        for group_id, ordinals in (logical_bindings or {}).items():
            entry = group_entries.get(group_id)
            if entry is None:
                return _safe_error("GOLD_GROUP_MISSING", gold_group_id=group_id)
            selected = [current_by_ordinal[ordinal] for ordinal in ordinals]
            candidate_ids = [str(item.candidate_id) for item in selected]
            final_groups = [
                cluster.group_id
                for cluster in current_cluster_by_candidates.get(
                    frozenset(candidate_ids), []
                )
                if cluster.discovery_action == "SAME_LOGICAL_CHANGE"
            ]
            if len(final_groups) != 1:
                return _safe_error(
                    "MANUAL_LOGICAL_GROUP_TOPOLOGY_MISMATCH",
                    gold_group_id=group_id,
                    matched_candidate_count=len(candidate_ids),
                    final_group_count=len(final_groups),
                )
            fingerprint = candidate_topology_fingerprint(
                candidate_ids,
                {str(item.candidate_id): item for item in selected},
                baseline=baseline_document,
                target=target_document,
            )
            captured[group_id] = {
                "gold_group_id": group_id,
                "source_ordinals": list(ordinals),
                "fragment_count": len(ordinals),
                "expected": "SAME_LOGICAL_CHANGE",
                "category": entry.get("category"),
                "topology_fingerprint": fingerprint,
            }
            for item in binding_audit:
                if item["source_ordinal"] in ordinals:
                    item["gold_group_id"] = group_id
                    item["final_group_fingerprint"] = fingerprint
        for group_id, ordinals in MANUAL_GROUPS.items():
            entry = group_entries.get(group_id)
            if entry is None:
                return _safe_error("GOLD_GROUP_MISSING", gold_group_id=group_id)
            if len(set(ordinals)) != len(ordinals):
                return _safe_error("MANUAL_ORDINAL_DUPLICATE", gold_group_id=group_id)
            selected = [current_by_ordinal[ordinal] for ordinal in ordinals]
            candidate_ids = [str(item.candidate_id) for item in selected]
            expected_action = (
                "BOUNDARY_NOISE" if len(ordinals) == 1 else "EQUIVALENT_NO_CHANGE"
            )
            if expected_action == "BOUNDARY_NOISE":
                final_groups = (
                    ["BOUNDARY_NOISE"]
                    if candidate_ids[0] in current_boundary_ids
                    else []
                )
            else:
                final_groups = [
                    cluster.group_id
                    for cluster in current_cluster_by_candidates.get(
                        frozenset(candidate_ids), []
                    )
                    if cluster.discovery_action == expected_action
                ]
            if len(final_groups) != 1:
                return _safe_error(
                    "MANUAL_GROUP_TOPOLOGY_MISMATCH",
                    gold_group_id=group_id,
                    current_candidate_count=len(current_diffs),
                    matched_candidate_count=len(candidate_ids),
                    final_group_count=len(final_groups),
                )
            fingerprint = candidate_topology_fingerprint(
                candidate_ids,
                {str(item.candidate_id): item for item in selected},
                baseline=baseline_document,
                target=target_document,
            )
            captured[group_id] = {
                (
                    "gold_case_id"
                    if expected_action == "BOUNDARY_NOISE"
                    else "gold_group_id"
                ): group_id,
                "source_ordinals": list(ordinals),
                "fragment_count": len(ordinals),
                "expected": expected_action,
                "category": entry.get("category"),
                "topology_fingerprint": fingerprint,
            }
            for item in binding_audit:
                if item["source_ordinal"] in ordinals:
                    item["gold_group_id"] = group_id
                    item["final_group_fingerprint"] = fingerprint

        result = copy.deepcopy(manifest)
        result["capture_metadata"] = {
            "capture_version": "final-compare-v2-manual-gold-v1",
            "source_task_id": args.source_task_id,
            "baseline_sha256": baseline_local.sha256,
            "target_sha256": target_local.sha256,
            "baseline_parser_name": baseline_document.parser_name,
            "target_parser_name": target_document.parser_name,
            "baseline_parser_version": baseline_document.parser_metadata.get(
                "parser_version"
            ),
            "target_parser_version": target_document.parser_metadata.get(
                "parser_version"
            ),
            "source_diff_count": len(source_result.diff_items),
            "historical_candidate_count": len(historical_catalog),
            "current_candidate_count": len(current_diffs),
            "candidate_binding_audit": binding_audit,
            "external_calls": {"ocr": 0, "llm": 0},
            "logical_group_binding_source": (
                str(getattr(args, "logical_groups", None))
                if getattr(args, "logical_groups", None) is not None
                else None
            ),
            "gold_write_ready": logical_bindings is not None,
        }
        result["logical_gold"]["fragment_groups"] = [
            captured.get(str(item.get("gold_group_id")), item)
            for item in result["logical_gold"].get("fragment_groups", [])
        ]
        result["logical_gold"]["equivalent_groups"] = [
            captured.get(str(item.get("gold_group_id")), item)
            for item in result["logical_gold"].get("equivalent_groups", [])
        ]
        result["logical_gold"]["boundary_noise"] = [
            captured.get(str(item.get("gold_case_id")), item)
            for item in result["logical_gold"].get("boundary_noise", [])
        ]
        fingerprint_by_group = {
            group_id: item["topology_fingerprint"]
            for group_id, item in captured.items()
        }
        for item in result["logical_gold"].get("false_positives", []):
            group_id = str(item.get("fragment_group_ref") or "")
            if group_id in fingerprint_by_group:
                item["topology_fingerprint"] = fingerprint_by_group[group_id]
        result["manifest_sha256"] = _manifest_digest(result)
        if args.write:
            if not args.ack_manual_review:
                return _safe_error("MANUAL_CAPTURE_CONFIRMATION_REQUIRED")
            args.output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        return {
            "status": "SUCCEEDED",
            "source_task_id": args.source_task_id,
            "source_diff_count": len(source_result.diff_items),
            "historical_candidate_count": len(historical_catalog),
            "captured_groups": {
                group_id: {
                    "source_ordinals": value["source_ordinals"],
                    "fragment_count": value["fragment_count"],
                    "topology_fingerprint": value["topology_fingerprint"],
                }
                for group_id, value in captured.items()
            },
            "candidate_binding_audit": binding_audit,
            "output": str(args.output) if args.write else None,
            "manifest_sha256": result["manifest_sha256"],
            "gold_write_ready": logical_bindings is not None,
            "ocr_calls": 0,
            "llm_calls": 0,
            "database_writes": 0,
            "contract_text_saved": False,
        }
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-task-id", required=True)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--historical-catalog",
        type=Path,
        default=DEFAULT_HISTORICAL_CATALOG,
        help="Body-free reviewed candidate catalog containing the manual ordinals.",
    )
    parser.add_argument(
        "--logical-groups",
        type=Path,
        default=DEFAULT_LOGICAL_GROUP_BINDINGS,
        help=(
            "Body-free operator-reviewed logical gold bindings. Required for --write; "
            "the file must list source ordinals for every gold fragment group."
        ),
    )
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--ack-manual-review", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(capture(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "SUCCEEDED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
