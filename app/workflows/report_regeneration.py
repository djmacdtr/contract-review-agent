"""Private report-regeneration workflow support.

The public DRAFT_REVIEW API intentionally has no regeneration mode.  This
module is used by the host-side operator only: it runs the existing review
workflow with locally supplied files, prevents any fact extraction fallback,
and applies the final page-location gate before the worker persists the new
result.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.adapters.llm.base import ContractLlmClient
from app.core.config import Settings
from app.core.errors import WorkflowError
from app.documents.page_locations import apply_docx_page_location_sidecars
from app.draft_review.checkpoints import ExtractionCheckpoint
from app.draft_review.extraction import (
    DOCUMENT_EXTRACTION_CHECKPOINT_VERSION,
    _validated_document_checkpoint,
)
from app.results.advice import ensure_fallback_risk_advices
from app.results.risk_model import build_statistics
from app.schemas.results import TaskResultData
from app.services.downloader import DOCX_MIME, LocalFile
from app.workflows.draft_review import DraftReviewWorkflowExecutor
from app.workflows.types import WorkflowOutput

FILE_REFERENCE_KEYS = frozenset(
    {
        "file_id",
        "source_file_id",
        "reference_file_id",
        "target_file_id",
        "baseline_file_id",
        "file_ids",
        "source_file_ids",
        "reference_file_ids",
        "target_file_ids",
    }
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _map_file_id(value: str, file_id_map: dict[str, str]) -> str:
    if value in file_id_map:
        return file_id_map[value]
    for old_id, new_id in sorted(file_id_map.items(), key=lambda item: -len(item[0])):
        if value.startswith(f"{old_id}_"):
            return f"{new_id}{value[len(old_id):]}"
    return value


def remap_file_references(
    value: Any,
    file_id_map: dict[str, str],
    *,
    task_id: str | None = None,
) -> Any:
    """Copy a result while remapping only explicit file-reference fields.

    Business identities such as ``risk_id``, ``diff_id`` and ``fact_id`` are
    deliberately not rewritten.  Block IDs embedded in a file-reference
    value are handled as a prefix so internal locations remain traceable.
    """

    def visit(item: Any, key: str | None = None, *, root: bool = False) -> Any:
        if isinstance(item, dict):
            return {
                name: (
                    task_id
                    if root and name == "task_id" and task_id is not None
                    else visit(child, name)
                )
                for name, child in item.items()
            }
        if isinstance(item, list):
            return [visit(child, key) for child in item]
        if isinstance(item, tuple):
            return tuple(visit(child, key) for child in item)
        if isinstance(item, str) and (
            key in FILE_REFERENCE_KEYS
            or (key is not None and key.endswith("_file_id"))
        ):
            return _map_file_id(item, file_id_map)
        if isinstance(item, str) and key in {"file_ids", "source_file_ids"}:
            return _map_file_id(item, file_id_map)
        return item

    result = visit(value, root=True)
    if task_id is not None and isinstance(result, dict):
        result["task_id"] = task_id
    return result


def file_reference_values(value: Any) -> list[tuple[str, str]]:
    """Collect safe key/value pairs used by the remapping preflight."""

    found: list[tuple[str, str]] = []

    def visit(item: Any, key: str | None = None) -> None:
        if isinstance(item, dict):
            for name, child in item.items():
                visit(child, name)
        elif isinstance(item, list | tuple):
            for child in item:
                visit(child, key)
        elif isinstance(item, str) and (
            key in FILE_REFERENCE_KEYS
            or (key is not None and key.endswith("_file_id"))
        ):
            found.append((key or "", item))

    visit(value)
    return found


def validate_file_reference_remap(
    result: dict[str, Any],
    *,
    old_file_ids: set[str],
    new_file_ids: set[str],
) -> None:
    """Reject old, unknown, or partially remapped explicit file references."""

    for key, value in file_reference_values(result):
        resolved = value
        if resolved in old_file_ids:
            raise WorkflowError(
                "REPORT_REGENERATION_FILE_REMAP_INCOMPLETE",
                "再生成结果仍引用来源文件身份",
                details={"failure_stage": "FILE_ID_REMAP", "failure_code": "OLD_FILE_ID_REMAINED"},
            )
        if resolved.startswith(tuple(f"{item}_" for item in old_file_ids)):
            raise WorkflowError(
                "REPORT_REGENERATION_FILE_REMAP_INCOMPLETE",
                "再生成结果仍引用来源结构身份",
                details={"failure_stage": "FILE_ID_REMAP", "failure_code": "OLD_FILE_ID_REMAINED"},
            )
        if key in FILE_REFERENCE_KEYS and resolved not in new_file_ids:
            raise WorkflowError(
                "REPORT_REGENERATION_FILE_REMAP_INCOMPLETE",
                "再生成结果包含未知文件身份",
                details={"failure_stage": "FILE_ID_REMAP", "failure_code": "UNKNOWN_FILE_ID"},
            )


class LocalRegenerationDownloader:
    """Downloader-compatible adapter that reads only prevalidated local files."""

    def __init__(
        self,
        paths_by_file_id: dict[str, Path],
        hashes_by_file_id: dict[str, str],
    ) -> None:
        self.paths_by_file_id = paths_by_file_id
        self.hashes_by_file_id = hashes_by_file_id

    async def prepare(self, files: list[dict[str, Any]], workspace: Any) -> list[LocalFile]:
        del workspace
        prepared: list[LocalFile] = []
        for item in files:
            file_id = str(item["file_id"])
            path = self.paths_by_file_id.get(file_id)
            expected_sha = self.hashes_by_file_id.get(file_id)
            if path is None or expected_sha is None or not path.is_file():
                raise WorkflowError(
                    "REPORT_REGENERATION_FILE_MISMATCH",
                    "再生成本地文件未通过安全校验",
                    details={
                        "failure_stage": "LOCAL_FILE_PREFLIGHT",
                        "failure_code": "FILE_NOT_AVAILABLE",
                    },
                )
            if item.get("sha256") and item["sha256"] != expected_sha:
                raise WorkflowError(
                    "REPORT_REGENERATION_FILE_MISMATCH",
                    "再生成文件摘要与任务文件不一致",
                    details={
                        "failure_stage": "LOCAL_FILE_PREFLIGHT",
                        "failure_code": "SHA256_MISMATCH",
                    },
                )
            if _sha256(path) != expected_sha:
                raise WorkflowError(
                    "REPORT_REGENERATION_FILE_MISMATCH",
                    "本地文件内容摘要校验失败",
                    details={
                        "failure_stage": "LOCAL_FILE_PREFLIGHT",
                        "failure_code": "SHA256_MISMATCH",
                    },
                )
            prepared.append(
                LocalFile(
                    file_id=file_id,
                    role=str(item["role"]),
                    file_name=str(item["file_name"]),
                    safe_url=str(item.get("safe_url") or ""),
                    path=path,
                    file_size=path.stat().st_size,
                    sha256=expected_sha,
                    detected_mime_type=DOCX_MIME,
                )
            )
        return prepared


class _SnapshotOnlyLlm:
    """Allow mapping/advice but make missing fact snapshots fail without HTTP."""

    def __init__(self, delegate: ContractLlmClient) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    @staticmethod
    def _missing_snapshot(*_args: Any, **_kwargs: Any) -> Any:
        raise WorkflowError(
            "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
            "来源文档抽取快照缺失，已安全停止再生成",
            details={
                "failure_stage": "SNAPSHOT_PREFLIGHT",
                "failure_code": "DOCUMENT_EXTRACTION_CHECKPOINT_MISSING",
                "fact_extraction_calls": 0,
            },
        )

    extract_document_profile = _missing_snapshot
    extract_fact_batch = _missing_snapshot
    extract_numeric_candidates = _missing_snapshot
    extract_text_facts = _missing_snapshot
    extract_facts = _missing_snapshot
    plan_semantics = _missing_snapshot
    review_facts = _missing_snapshot


class ReportRegenerationWorkflowExecutor(DraftReviewWorkflowExecutor):
    """Run mapping, result generation, Advice and strict page enrichment only."""

    def __init__(
        self,
        settings: Settings,
        *,
        source_result: dict[str, Any],
        source_file_ids: set[str],
        current_file_ids: set[str],
        file_id_map: dict[str, str],
        source_task_id: str,
        downloader: LocalRegenerationDownloader,
        llm: ContractLlmClient,
        source_snapshot_records: dict[str, dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(settings, downloader=downloader, llm=_SnapshotOnlyLlm(llm), **kwargs)
        self.source_result = deepcopy(source_result)
        self.source_file_ids = set(source_file_ids)
        self.current_file_ids = set(current_file_ids)
        self.file_id_map = dict(file_id_map)
        self.source_task_id = source_task_id
        self.source_snapshot_records = deepcopy(source_snapshot_records or {})

    async def load_report_regeneration_snapshots(
        self,
        *,
        documents: list[Any],
        template_review: Any,
        task_id: str | None,
        source_task_id: str | None,
    ) -> dict[str, dict[str, Any]]:
        """Load exact source snapshots and materialize them for this task.

        Regeneration deliberately does not derive a new shard identity.  The
        source task, file digest, and document snapshot version are the sole
        lookup contract; evidence validation and file-ID rebinding happen
        against the formally parsed current document.
        """

        del template_review
        if source_task_id != self.source_task_id or not task_id:
            raise WorkflowError(
                "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
                "报告再生成缺少受控来源快照路由",
                details={
                    "failure_stage": "SNAPSHOT_INJECTION",
                    "failure_code": "SNAPSHOT_SOURCE_TASK_ID_INVALID",
                    "fact_extraction_calls": 0,
                },
            )
        if len(self.source_snapshot_records) != 3:
            raise WorkflowError(
                "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
                "报告再生成来源快照数量不完整",
                details={
                    "failure_stage": "SNAPSHOT_INJECTION",
                    "failure_code": "DOCUMENT_EXTRACTION_CHECKPOINT_MISSING",
                    "source_snapshot_count": len(self.source_snapshot_records),
                    "fact_extraction_calls": 0,
                },
            )
        source_ids_by_current_id = {
            current_id: source_id for source_id, current_id in self.file_id_map.items()
        }
        if len(documents) != 3:
            raise WorkflowError(
                "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
                "报告再生成当前文档数量不完整",
                details={
                    "failure_stage": "SNAPSHOT_INJECTION",
                    "failure_code": "CURRENT_DOCUMENT_COUNT_INVALID",
                    "fact_extraction_calls": 0,
                },
            )

        extractions: dict[str, dict[str, Any]] = {}
        materialized: list[ExtractionCheckpoint] = []
        for document in documents:
            source_record = self.source_snapshot_records.get(document.sha256)
            if source_record is None or not isinstance(source_record.get("value"), dict):
                raise WorkflowError(
                    "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
                    "报告再生成来源文档快照缺失",
                    details={
                        "failure_stage": "SNAPSHOT_INJECTION",
                        "failure_code": "DOCUMENT_EXTRACTION_CHECKPOINT_MISSING",
                        "file_id": document.file_id,
                        "fact_extraction_calls": 0,
                    },
                )
            extraction = _validated_document_checkpoint(
                document,
                source_record["value"],
                source_file_id=source_ids_by_current_id.get(document.file_id),
            )
            if extraction is None:
                raise WorkflowError(
                    "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
                    "来源文档快照证据无法重绑定到当前文件",
                    details={
                        "failure_stage": "SNAPSHOT_INJECTION",
                        "failure_code": "SNAPSHOT_EVIDENCE_REBIND_FAILED",
                        "file_id": document.file_id,
                        "fact_extraction_calls": 0,
                    },
                )
            batch_id = source_record.get("batch_id")
            payload_digest = source_record.get("payload_digest")
            if not isinstance(batch_id, str) or not isinstance(payload_digest, str):
                raise WorkflowError(
                    "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
                    "来源文档快照身份不完整",
                    details={
                        "failure_stage": "SNAPSHOT_INJECTION",
                        "failure_code": "SNAPSHOT_IDENTITY_INVALID",
                        "file_id": document.file_id,
                        "fact_extraction_calls": 0,
                    },
                )
            materialized.append(
                ExtractionCheckpoint(
                    task_id=task_id,
                    file_sha256=document.sha256,
                    extraction_version=DOCUMENT_EXTRACTION_CHECKPOINT_VERSION,
                    batch_id=batch_id,
                    payload_digest=payload_digest,
                    value=extraction.model_dump(mode="json"),
                    status="SUCCEEDED",
                    model_name=source_record.get("model_name"),
                    source_task_id=self.source_task_id,
                )
            )
            extractions[document.file_id] = {
                "value": extraction.model_dump(mode="json"),
                "status": "SUCCEEDED",
                "checkpoint_reused": True,
                "document_checkpoint_reused": True,
                "configured_model": source_record.get("model_name"),
                "actual_model": None,
                "duration_ms": 0,
                "request_attempts": 0,
                "structure_retries": 0,
                "batch_id": batch_id,
                "extraction_version": DOCUMENT_EXTRACTION_CHECKPOINT_VERSION,
            }

        if self.checkpoint_store is None:
            raise WorkflowError(
                "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
                "报告再生成缺少 checkpoint 存储",
                details={
                    "failure_stage": "SNAPSHOT_INJECTION",
                    "failure_code": "SNAPSHOT_STORE_NOT_CONFIGURED",
                    "fact_extraction_calls": 0,
                },
            )
        for checkpoint in materialized:
            await self.checkpoint_store.save(checkpoint)
        if len(extractions) != 3 or len(materialized) != 3:
            raise WorkflowError(
                "REPORT_REGENERATION_SNAPSHOT_INCOMPLETE",
                "报告再生成当前任务快照物化不完整",
                details={
                    "failure_stage": "SNAPSHOT_INJECTION",
                    "failure_code": "SNAPSHOT_MATERIALIZATION_INCOMPLETE",
                    "materialized_snapshot_count": len(materialized),
                    "fact_extraction_calls": 0,
                },
            )
        return extractions

    def _merge_source_template_baseline(
        self,
        generated: dict[str, Any],
        task_id: str,
    ) -> dict[str, Any]:
        source = remap_file_references(self.source_result, self.file_id_map, task_id=task_id)
        source_diff_ids = [str(item["diff_id"]) for item in source.get("diff_items", [])]
        generated_diff_by_id = {
            str(item["diff_id"]): item for item in generated.get("diff_items", [])
        }
        if any(diff_id not in generated_diff_by_id for diff_id in source_diff_ids):
            raise WorkflowError(
                "REPORT_REGENERATION_IDENTITY_MISMATCH",
                "再生成未保留来源报告的确定性差异身份",
                details={
                    "failure_stage": "RESULT_REBUILD",
                    "failure_code": "SOURCE_DIFF_ID_MISSING",
                },
            )
        generated_risks = {
            str(item["risk_id"]): item for item in generated.get("risk_items", [])
        }
        source_risks = [str(item["risk_id"]) for item in source.get("risk_items", [])]
        if any(risk_id not in generated_risks for risk_id in source_risks):
            raise WorkflowError(
                "REPORT_REGENERATION_IDENTITY_MISMATCH",
                "再生成未保留来源报告的风险身份",
                details={
                    "failure_stage": "RESULT_REBUILD",
                    "failure_code": "SOURCE_RISK_ID_MISSING",
                },
            )

        merged = deepcopy(generated)
        merged["diff_items"] = [
            deepcopy(
                next(
                    item
                    for item in source.get("diff_items", [])
                    if item["diff_id"] == diff_id
                )
            )
            for diff_id in source_diff_ids
        ] + [
            item
            for item in generated.get("diff_items", [])
            if item["diff_id"] not in set(source_diff_ids)
        ]
        merged_risks: list[dict[str, Any]] = []
        for source_risk in source.get("risk_items", []):
            item = deepcopy(source_risk)
            refreshed = generated_risks.get(str(source_risk["risk_id"]))
            if refreshed and refreshed.get("analysis_advice"):
                item["analysis_advice"] = refreshed["analysis_advice"]
            merged_risks.append(item)
        merged["risk_items"] = merged_risks + [
            item
            for item in generated.get("risk_items", [])
            if item["risk_id"] not in set(source_risks)
        ]
        source_passed_ids = {
            str(item["check_id"]) for item in source.get("passed_checks", [])
        }
        merged["passed_checks"] = [deepcopy(item) for item in source.get("passed_checks", [])] + [
            item
            for item in generated.get("passed_checks", [])
            if item["check_id"] not in source_passed_ids
        ]
        merged["summary"]["statistics"] = build_statistics(
            merged["risk_items"], merged.get("review_items", []), merged["passed_checks"]
        )
        merged["summary"]["description"] = (
            f"已完成 {len(merged.get('files', []))} 份文件检查，确认 "
            f"{len(merged['risk_items'])} 项风险，{len(merged['passed_checks'])} 项校验通过。"
        )
        merged["conclusion"] = "RISK_FOUND" if merged["risk_items"] else "PASS"
        return merged

    @staticmethod
    def _require_public_pages(
        result: dict[str, Any],
        *,
        sidecars: dict[str, Any],
        current_file_ids: set[str],
    ) -> dict[str, int]:
        if set(sidecars) != current_file_ids:
            raise WorkflowError(
                "DOCX_PAGE_LOCATION_INCOMPLETE",
                "三份 DOCX 均未获得完整真实页码映射",
                details={
                    "failure_stage": "PUBLIC_EVIDENCE_MAPPING",
                    "failure_code": "SIDECAR_COVERAGE_INCOMPLETE",
                    "sidecar_count": len(sidecars),
                    "file_count": len(current_file_ids),
                },
            )
        page_counts = {
            file_id: int(sidecar.page_count)
            for file_id, sidecar in sidecars.items()
            if isinstance(sidecar.page_count, int) and sidecar.page_count > 0
        }
        if set(page_counts) != current_file_ids:
            raise WorkflowError(
                "DOCX_PAGE_LOCATION_INCOMPLETE",
                "公开证据缺少真实页码范围",
                details={
                    "failure_stage": "PUBLIC_EVIDENCE_MAPPING",
                    "failure_code": "PAGE_COUNT_MISSING",
                },
            )

        def require_location(location: Any, file_id: str | None = None) -> None:
            if not isinstance(location, dict) or not isinstance(location.get("page"), int):
                raise WorkflowError(
                    "DOCX_PAGE_LOCATION_INCOMPLETE",
                    "公开证据位置缺少真实页码",
                    details={
                        "failure_stage": "PUBLIC_EVIDENCE_MAPPING",
                        "failure_code": "PUBLIC_EVIDENCE_PAGE_MISSING",
                    },
                )
            page = int(location["page"])
            if page < 1 or (file_id and page > page_counts.get(file_id, 0)):
                raise WorkflowError(
                    "DOCX_PAGE_LOCATION_INCOMPLETE",
                    "公开证据页码超出真实文档范围",
                    details={
                        "failure_stage": "PUBLIC_EVIDENCE_MAPPING",
                        "failure_code": "PUBLIC_EVIDENCE_PAGE_OUT_OF_RANGE",
                    },
                )

        for diff in result.get("diff_items", []):
            for side_name in ("baseline", "target"):
                side = diff.get(side_name)
                if isinstance(side, dict) and side:
                    file_id = side.get("file_id")
                    require_location(side.get("location"), file_id)
                    for location in side.get("locations", []):
                        require_location(location, file_id)
        for risk in result.get("risk_items", []):
            if not risk.get("related_diff_ids"):
                continue
            for evidence in risk.get("source_evidence", []):
                if not isinstance(evidence, dict) or not evidence.get("file_id"):
                    raise WorkflowError(
                        "DOCX_PAGE_LOCATION_INCOMPLETE",
                        "公开风险证据缺少文件身份",
                        details={
                            "failure_stage": "PUBLIC_EVIDENCE_MAPPING",
                            "failure_code": "PUBLIC_EVIDENCE_FILE_MISSING",
                        },
                    )
                locations = evidence.get("locations") or [evidence.get("location")]
                for location in locations:
                    require_location(location, evidence.get("file_id"))
        return page_counts

    async def run(self, **kwargs: Any) -> WorkflowOutput:
        options = dict(kwargs.get("options") or {})
        options["_report_regeneration_explicit_snapshot"] = True
        kwargs["options"] = options
        output = await super().run(**kwargs)
        result = self._merge_source_template_baseline(output.result, kwargs["task_id"])
        remapped = remap_file_references(result, {}, task_id=kwargs["task_id"])
        validate_file_reference_remap(
            remapped,
            old_file_ids=self.source_file_ids,
            new_file_ids=self.current_file_ids,
        )
        sidecars = getattr(self.parsers, "page_location_sidecars", {})
        apply_docx_page_location_sidecars(remapped, sidecars, strict=True)
        page_counts = self._require_public_pages(
            remapped,
            sidecars=sidecars,
            current_file_ids=self.current_file_ids,
        )
        ensure_fallback_risk_advices(remapped)
        metadata = remapped.setdefault("metadata", {})
        runs = metadata.get("model_runs", [])
        metadata["report_regeneration"] = {
            "source_task_id": self.source_task_id,
            "document_checkpoint_version": DOCUMENT_EXTRACTION_CHECKPOINT_VERSION,
            "document_snapshot_count": 3,
            "fact_extraction_calls": 0,
            "mapping_call_count": sum(
                1
                for item in runs
                if isinstance(item, dict)
                and item.get("purpose") in {"FACT_MAPPING", "FACT_MAPPING_REVIEW"}
            ),
            "advice_call_count": sum(
                1
                for item in runs
                if isinstance(item, dict) and item.get("purpose") == "RISK_ADVICE"
            ),
            "page_counts": page_counts,
            "file_id_remapped": True,
        }
        TaskResultData.model_validate(remapped)
        return WorkflowOutput(result=remapped, file_metadata=output.file_metadata)
