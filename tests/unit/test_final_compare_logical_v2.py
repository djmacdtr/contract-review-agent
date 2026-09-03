from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.adapters.llm.base import LlmResult, MockContractLlmClient
from app.comparison.candidate_validation import (
    _diff_equivalent,
    validate_final_compare_candidates,
)
from app.comparison.duplicate_clusters import (
    DuplicateCluster,
    _boundary_noise_reason,
    _complete_relation_groups,
    _equivalent_candidate_pair,
    apply_deterministic_final_compare_filters,
    build_candidate_discovery_gold_audit,
    build_suspected_duplicate_clusters,
    build_v2_quality_audit,
    candidate_topology_fingerprint,
    replay_final_compare_gold,
    select_canary_clusters,
    validate_final_compare_duplicate_clusters,
)
from app.comparison.engine import CompareOptions, compare_documents
from app.comparison.logical_v2 import (
    build_logical_table,
    deduplicate_diff_candidates,
    deduplicate_diff_candidates_with_audit,
)
from app.comparison.models import ComparisonResult, DiffItem, DiffSide
from app.core.config import Settings
from app.documents.models import (
    DocumentBlock,
    DocumentLocation,
    ParsedDocument,
    ParsedTable,
    TableCell,
    TableRow,
)
from app.workflows.final_compare import FinalCompareWorkflowExecutor
from scripts.capture_final_compare_gold import (
    _candidate_binding_signature,
    _historical_catalog_binding_signature,
    _load_manual_logical_group_bindings,
)


def _paragraph_document(file_id: str, role: str, texts: list[str]) -> ParsedDocument:
    return ParsedDocument(
        file_id=file_id,
        role=role,
        file_name=f"{file_id}.docx",
        sha256=("a" if role == "BASELINE" else "b") * 64,
        page_count=1,
        blocks=[
            DocumentBlock(
                block_id=f"{file_id}_{index}",
                type="PARAGRAPH",
                order=index,
                raw_text=text,
                normalized_text=text,
                location=DocumentLocation(page=1, paragraph_index=index),
            )
            for index, text in enumerate(texts)
        ],
        parser_name="fixture",
    )


def test_gold_fingerprint_uses_file_sha_and_direction_not_runtime_candidate_id() -> None:
    baseline = _paragraph_document("baseline-a", "BASELINE", ["第1条 原文"])
    target = _paragraph_document("target-a", "TARGET", ["第1条 新文"])
    baseline_side = DiffSide(
        file_id="runtime-file-a",
        location=DocumentLocation(page=1, paragraph_index=0),
        text="第1条 原文",
    )
    target_side = DiffSide(
        file_id="runtime-file-b",
        location=DocumentLocation(page=1, paragraph_index=0),
        text="第1条 新文",
    )
    diff = DiffItem(
        diff_id="diff-a",
        candidate_id="candidate-a",
        diff_type="MODIFIED",
        title="文字变化",
        baseline=baseline_side,
        target=target_side,
        confidence=1,
    )
    first = candidate_topology_fingerprint(
        ["candidate-a"], {"candidate-a": diff}, baseline=baseline, target=target
    )
    runtime_id_variant = diff.model_copy(
        update={
            "candidate_id": "candidate-other",
            "baseline": baseline_side.model_copy(update={"file_id": "new-file-a"}),
            "target": target_side.model_copy(update={"file_id": "new-file-b"}),
        }
    )
    second = candidate_topology_fingerprint(
        ["candidate-other"],
        {"candidate-other": runtime_id_variant},
        baseline=baseline,
        target=target,
    )
    assert first == second

    changed_target = target.model_copy(update={"sha256": "c" * 64})
    assert candidate_topology_fingerprint(
        ["candidate-a"], {"candidate-a": diff}, baseline=baseline, target=changed_target
    ) != first


def test_manual_gold_binding_accepts_legacy_catalog_without_paragraph_index() -> None:
    diff = DiffItem(
        diff_id="diff-binding",
        candidate_id="candidate-binding",
        diff_type="MODIFIED",
        title="文字变化",
        baseline=DiffSide(
            file_id="runtime-base",
            location=DocumentLocation(page=1, section=None),
            text="基准摘要",
        ),
        target=DiffSide(
            file_id="runtime-target",
            location=DocumentLocation(page=2, section="章节"),
            text="目标摘要",
        ),
        confidence=1,
    )
    current = _candidate_binding_signature(diff)
    historical = _historical_catalog_binding_signature(
        {
            "baseline": {
                "text_sha256": hashlib.sha256("基准摘要".encode()).hexdigest()[:16],
                "locations": [{"page": 1, "section": None}],
            },
            "target": {
                "text_sha256": hashlib.sha256("目标摘要".encode()).hexdigest()[:16],
                "locations": [{"page": 2, "section": "章节"}],
            },
        }
    )

    assert current == historical


def test_manual_logical_gold_bindings_require_explicit_operator_input(tmp_path: Path) -> None:
    expected = [
        {
            "gold_group_id": "gold_group_01",
            "fragment_count": 2,
        }
    ]
    assert _load_manual_logical_group_bindings(
        None,
        source_task_id="task-01",
        expected_groups=expected,
    ) == (None, None)

    bindings_path = tmp_path / "logical-groups.json"
    bindings_path.write_text(
        json.dumps(
            {
                "source_task_id": "task-01",
                "groups": [
                    {
                        "gold_group_id": "gold_group_01",
                        "source_ordinals": [7, 19],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    bindings, error = _load_manual_logical_group_bindings(
        bindings_path,
        source_task_id="task-01",
        expected_groups=expected,
    )

    assert error is None
    assert bindings == {"gold_group_01": (7, 19)}


def test_manual_logical_gold_bindings_reject_overlap_and_group_set_drift(
    tmp_path: Path,
) -> None:
    expected = [
        {"gold_group_id": "gold_group_01", "fragment_count": 2},
        {"gold_group_id": "gold_group_02", "fragment_count": 2},
    ]
    bindings_path = tmp_path / "logical-groups.json"
    bindings_path.write_text(
        json.dumps(
            {
                "source_task_id": "task-01",
                "groups": [
                    {
                        "gold_group_id": "gold_group_01",
                        "source_ordinals": [7, 19],
                    },
                    {
                        "gold_group_id": "gold_group_02",
                        "source_ordinals": [19, 25],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    bindings, error = _load_manual_logical_group_bindings(
        bindings_path,
        source_task_id="task-01",
        expected_groups=expected,
    )

    assert bindings is None
    assert error is not None
    assert error["failure_code"] == "MANUAL_LOGICAL_GROUP_BINDING_INVALID"

    bindings_path.write_text(
        json.dumps(
            {
                "source_task_id": "task-01",
                "groups": [
                    {
                        "gold_group_id": "gold_group_01",
                        "source_ordinals": [7, 19],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    bindings, error = _load_manual_logical_group_bindings(
        bindings_path,
        source_task_id="task-01",
        expected_groups=expected,
    )

    assert bindings is None
    assert error is not None
    assert error["failure_code"] == "MANUAL_LOGICAL_GROUP_BINDING_SET_MISMATCH"


def _table_document(
    file_id: str,
    rows: list[list[tuple[str, str | None]]],
    *,
    header: str = "项目|金额",
) -> ParsedDocument:
    parsed_rows = []
    for row_index, row in enumerate(rows):
        parsed_rows.append(
            TableRow(
                row=row_index,
                cells=[
                    TableCell(
                        raw_text=text,
                        normalized_text=text,
                        logical_cell_id=logical_id,
                        location=DocumentLocation(
                            table_index=0,
                            row=row_index,
                            column=column,
                            page=1,
                        ),
                    )
                    for column, (text, logical_id) in enumerate(row)
                ],
            )
        )
    return ParsedDocument(
        file_id=file_id,
        role="BASELINE" if file_id == "base" else "TARGET",
        file_name=f"{file_id}.docx",
        sha256=("a" if file_id == "base" else "b") * 64,
        page_count=1,
        blocks=[
            DocumentBlock(
                block_id=f"{file_id}_table",
                type="TABLE",
                order=0,
                raw_text=header,
                normalized_text=header,
                location=DocumentLocation(table_index=0, page=1),
                table=ParsedTable(table_index=0, rows=parsed_rows),
            )
        ],
        parser_name="test",
    )


def _coordinate_table_document(
    file_id: str,
    rows: list[list[tuple[int, str, str | None]]],
    *,
    header: str = "",
) -> ParsedDocument:
    parsed_rows = [
        TableRow(
            row=row_index,
            cells=[
                TableCell(
                    raw_text=text,
                    normalized_text=text,
                    logical_cell_id=logical_id,
                    location=DocumentLocation(
                        table_index=0,
                        row=row_index,
                        column=column,
                        page=1,
                    ),
                )
                for column, text, logical_id in row
            ],
        )
        for row_index, row in enumerate(rows)
    ]
    return ParsedDocument(
        file_id=file_id,
        role="BASELINE" if file_id == "base" else "TARGET",
        file_name=f"{file_id}.docx",
        sha256=("a" if file_id == "base" else "b") * 64,
        page_count=1,
        blocks=[
            DocumentBlock(
                block_id=f"{file_id}_table",
                type="TABLE",
                order=0,
                raw_text=header,
                normalized_text=header,
                location=DocumentLocation(table_index=0, page=1),
                table=ParsedTable(table_index=0, rows=parsed_rows),
            )
        ],
        parser_name="test",
    )


def test_logical_table_collapses_merged_cell_but_keeps_physical_locations() -> None:
    document = _table_document(
        "base",
        [
            [("项目", "header-project"), ("金额", "header-amount")],
            [("设备", "merged"), ("100", "amount-1")],
            [("设备", "merged"), ("120", "amount-2")],
        ],
    )

    table = build_logical_table(document.blocks[0])

    merged = next(cell for row in table.rows for cell in row if cell.cell_id == "merged")
    assert merged.row == 1
    assert len(merged.locations) == 2
    assert sum(cell.cell_id == "merged" for row in table.rows for cell in row) == 1


def test_logical_v2_compares_table_cells_and_numeric_priority() -> None:
    baseline = _table_document(
        "base",
        [
            [("项目", "h1"), ("金额", "h2")],
            [("设备A", "a"), ("100万元", "b")],
        ],
    )
    target = _table_document(
        "target",
        [
            [("项目", "h1"), ("金额", "h2")],
            [("设备A", "a"), ("120万元", "b")],
        ],
    )

    result = compare_documents(
        baseline,
        target,
        CompareOptions(comparison_mode="FINAL_LOGICAL_V2"),
    )

    assert [item.diff_type for item in result.diff_items] == ["NUMERIC_CHANGED"]
    assert result.diff_items[0].baseline.location.column == 1
    assert result.diff_items[0].logical_area_key is not None
    assert result.diagnostics.fallback_mode == "FINAL_LOGICAL_V2"


def test_v2_ignores_clause_number_shift_when_semantic_body_is_unchanged() -> None:
    baseline = _paragraph_document("base", "BASELINE", ["第十九条 租赁期限为24个月。"])
    target = _paragraph_document("target", "TARGET", ["第二十条 租赁期限为24个月。"])

    result = compare_documents(
        baseline,
        target,
        CompareOptions(comparison_mode="FINAL_LOGICAL_V2"),
    )

    assert result.diff_items == []
    assert result.validation_stats["number_shift_merged_count"] == 1


def test_v2_ignores_list_number_shift_but_keeps_body_changes() -> None:
    baseline = _paragraph_document(
        "base", "BASELINE", ["1. 应提交租赁物清单。", "2. 应提交付款凭证。"]
    )
    target = _paragraph_document(
        "target", "TARGET", ["1. 应提交付款凭证。", "2. 应提交新增的付款凭证。"]
    )

    result = compare_documents(
        baseline,
        target,
        CompareOptions(comparison_mode="FINAL_LOGICAL_V2"),
    )

    diff_shapes = [
        (item.diff_type, item.baseline is not None, item.target is not None)
        for item in result.diff_items
    ]
    assert diff_shapes == [
        ("DELETED", True, False),
        ("ADDED", False, True),
    ]
    assert result.validation_stats["number_shift_merged_count"] == 1


def test_v2_matches_table_rows_by_business_key_not_physical_order() -> None:
    baseline = _table_document(
        "base",
        [
            [("序号", "h0"), ("名称", "h1"), ("金额", "h2")],
            [("1", "r1n"), ("设备A", "r1a"), ("100", "r1v")],
            [("2", "r2n"), ("设备B", "r2a"), ("200", "r2v")],
        ],
        header="序号|名称|金额",
    )
    target = _table_document(
        "target",
        [
            [("序号", "h0"), ("名称", "h1"), ("金额", "h2")],
            [("2", "r2n"), ("设备B", "r2a"), ("200", "r2v")],
            [("1", "r1n"), ("设备A", "r1a"), ("120", "r1v")],
        ],
        header="序号|名称|金额",
    )

    result = compare_documents(
        baseline,
        target,
        CompareOptions(comparison_mode="FINAL_LOGICAL_V2"),
    )

    assert [item.diff_type for item in result.diff_items] == ["NUMERIC_CHANGED"]
    assert result.diff_items[0].baseline.location.row == 1
    assert result.diff_items[0].target.location.row == 2


def test_v2_uses_sparse_physical_columns_without_shifting_values() -> None:
    headers = [
        (0, "序号", "h0"),
        (1, "名称", "h1"),
        (2, "型号", "h2"),
        (3, "位置", "h3"),
        (4, "单位", "h4"),
        (5, "数量", "h5"),
        (6, "金额", "h6"),
    ]
    baseline = _coordinate_table_document(
        "base",
        [
            headers,
            [
                (0, "1", "r1n"),
                (1, "设备A", "r1a"),
                (2, "型号A", "r1m"),
                (3, "仓库", "r1p"),
                (4, "台", "r1u"),
                (5, "2", "r1q"),
                (6, "100", "r1v"),
            ],
            [
                (0, "2", "r2n"),
                (1, "设备B", "r2a"),
                (2, "型号B", "r2m"),
                (3, "仓库", "r2p"),
                (4, "台", "r2u"),
                (5, "3", "r2q"),
                (6, "200", "r2v"),
            ],
        ],
    )
    target = _coordinate_table_document(
        "target",
        [
            headers,
            [
                (0, "1", "r1n"),
                (1, "设备A", "r1a"),
                (2, "型号A", "r1m"),
                (3, "仓库", "r1p"),
                (4, "台", "r1u"),
                (5, "2", "r1q"),
                (6, "100", "r1v"),
            ],
            [
                (0, "2", "r2n"),
                (1, "设备B", "r2a"),
                (2, "型号B", "r2m"),
                (4, "台", "r2u"),
                (5, "3", "r2q"),
                (6, "220", "r2v"),
            ],
        ],
    )

    result = compare_documents(
        baseline,
        target,
        CompareOptions(comparison_mode="FINAL_LOGICAL_V2"),
    )

    assert [item.diff_type for item in result.diff_items] == ["NUMERIC_CHANGED"]
    assert result.diff_items[0].baseline.location.column == 6
    assert result.diff_items[0].target.location.column == 6
    assert result.validation_stats["sparse_column_alignment_count"] == 1


def test_v2_accepts_reliable_vertical_merge_continuation_without_fake_missing_cell() -> None:
    headers = [
        (0, "序号", "h0"),
        (1, "名称", "h1"),
        (2, "数量", "h2"),
        (3, "位置", "h3"),
    ]
    base_rows = [
        headers,
        [(0, "1", "r1"), (1, "设备A", "a1"), (2, "1", "q1"), (3, "仓库", "p1")],
        [(0, "2", "r2"), (1, "设备B", "a2"), (2, "2", "q2"), (3, "仓库", "p2")],
    ]
    target_rows = [
        headers,
        [(0, "1", "r1"), (1, "设备A", "a1"), (2, "1", "q1"), (3, "仓库", "p1")],
        [(0, "2", "r2"), (1, "设备B", "a2"), (2, "2", "q2")],
    ]

    result = compare_documents(
        _coordinate_table_document("base", base_rows),
        _coordinate_table_document("target", target_rows),
        CompareOptions(comparison_mode="FINAL_LOGICAL_V2"),
    )

    assert result.diff_items == []
    assert result.validation_stats["vertical_merge_continuation_count"] == 1


def test_v2_matches_key_value_rows_by_group_and_subnumber_when_labels_are_blank() -> None:
    baseline = _coordinate_table_document(
        "base",
        [
            [(0, "租金", "rent-group"), (1, "1. 每期租金", "rent-1")],
            [(0, "租金", "rent-group"), (1, "2. 租金期数", "rent-2")],
            [(0, "保证金", "deposit-group"), (1, "1. 保证金金额", "deposit-1")],
        ],
    )
    target = _coordinate_table_document(
        "target",
        [
            [(0, "租金", "rent-1-group"), (1, "1. 每期租金", "rent-1")],
            [(0, "", "rent-2-group"), (1, "2. 租金期数", "rent-2")],
            [(0, "保证金", "deposit-group"), (1, "1. 保证金金额", "deposit-1")],
        ],
    )

    result = compare_documents(
        baseline,
        target,
        CompareOptions(comparison_mode="FINAL_LOGICAL_V2"),
    )

    assert result.diff_items == []
    assert result.validation_stats["key_value_row_alignment_count"] == 3


def test_table_structure_candidate_is_kept_for_review_when_unmatched() -> None:
    baseline = _table_document(
        "base",
        [[("项目", "h1"), ("金额", "h2")], [("设备A", "a"), ("100", "b")]],
    )
    target = _table_document(
        "target",
        [[("不同表", "x1"), ("数量", "x2")], [("设备A", "a2"), ("1", "b2")]],
    )

    result = compare_documents(
        baseline,
        target,
        CompareOptions(comparison_mode="FINAL_LOGICAL_V2"),
    )

    assert result.diff_items
    assert all(item.validation_status == "REVIEW_REQUIRED" for item in result.diff_items)
    assert result.candidate_records


def test_v2_uncertain_table_candidates_are_published_as_review_items() -> None:
    baseline = _table_document(
        "base",
        [[("项目", "h1"), ("金额", "h2")], [("设备A", "a"), ("100", "b")]],
    )
    target = _table_document(
        "target",
        [[("不同表", "x1"), ("数量", "x2")], [("设备A", "a2"), ("1", "b2")]],
    )
    comparison = compare_documents(
        baseline,
        target,
        CompareOptions(comparison_mode="FINAL_LOGICAL_V2"),
    )

    result = FinalCompareWorkflowExecutor(Settings(_env_file=None))._build_result(
        "tsk_v2_review",
        [
            {"file_id": "base", "safe_url": "dry-run://base"},
            {"file_id": "target", "safe_url": "dry-run://target"},
        ],
        [baseline, target],
        comparison,
    )

    assert result["risk_items"] == []
    assert result["diff_items"] == []
    assert result["review_items"]
    assert result["summary"]["statistics"]["review_count"] == len(
        result["review_items"]
    )
    assert result["review_items"][0]["related_diff_ids"]


@pytest.mark.asyncio
async def test_candidate_llm_keep_marks_review_candidate_confirmed() -> None:
    baseline = _table_document(
        "base",
        [[("项目", "h1"), ("金额", "h2")], [("设备A", "a"), ("100", "b")]],
    )
    target = _table_document(
        "target",
        [[("不同表", "x1"), ("数量", "x2")], [("设备A", "a2"), ("1", "b2")]],
    )
    result = compare_documents(
        baseline,
        target,
        CompareOptions(comparison_mode="FINAL_LOGICAL_V2"),
    )

    await validate_final_compare_candidates(result, MockContractLlmClient())

    assert result.diff_items
    assert all(item.validation_status == "CONFIRMED" for item in result.diff_items)
    assert all(item.validation_source == "RULE_AND_LLM" for item in result.diff_items)
    assert result.validation_stats["llm_reviewed_count"] == len(result.candidate_records)


@pytest.mark.asyncio
async def test_candidate_validation_failure_keeps_diff_and_marks_review() -> None:
    class FailingClient:
        async def validate_final_compare_candidates(self, _payload: dict) -> None:
            raise RuntimeError("provider detail must not escape")

    baseline = _table_document(
        "base",
        [[("项目", "h1"), ("金额", "h2")], [("设备A", "a"), ("100", "b")]],
    )
    target = _table_document(
        "target",
        [[("不同表", "x1"), ("数量", "x2")], [("设备A", "a2"), ("1", "b2")]],
    )
    result = compare_documents(
        baseline,
        target,
        CompareOptions(comparison_mode="FINAL_LOGICAL_V2"),
    )
    await validate_final_compare_candidates(result, FailingClient())

    assert result.diff_items
    assert all(item.validation_status == "REVIEW_REQUIRED" for item in result.diff_items)
    assert result.validation_stats["candidate_validation_failures"] == 1
    assert "provider detail" not in str(result.validation_stats)


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence, expected_removed", [(0.94, 0), (0.95, 1)])
async def test_duplicate_of_requires_high_confidence_and_logical_area(
    confidence: float, expected_removed: int
) -> None:
    baseline = _table_document(
        "base",
        [[("不同表", "x1"), ("数量", "x2")], [("设备A", "a2"), ("1", "b2")]],
    )
    target = _table_document(
        "target",
        [[("项目", "h1"), ("金额", "h2")], [("设备A", "a"), ("100", "b")]],
    )
    result = compare_documents(
        baseline,
        target,
        CompareOptions(comparison_mode="FINAL_LOGICAL_V2"),
    )
    original = result.diff_items[0]
    first = original.model_copy(
        update={"candidate_id": "candidate_first1234", "logical_area_key": "area-1"}
    )
    second = original.model_copy(
        update={
            "diff_id": "diff_duplicate",
            "candidate_id": "candidate_second1234",
            "logical_area_key": "area-1",
        }
    )
    result.diff_items = [first, second]
    record = result.candidate_records[0]
    result.candidate_records = [
        {**record, "candidate_id": first.candidate_id},
        {**record, "candidate_id": second.candidate_id},
    ]

    class DuplicateClient:
        async def validate_final_compare_candidates(self, payload: dict) -> LlmResult:
            first_id, second_id = [item["candidate_id"] for item in payload["candidates"]]
            return LlmResult(
                value={
                    "decisions": [
                        {
                            "candidate_id": first_id,
                            "decision": "KEEP_CHANGE",
                            "duplicate_of": None,
                            "reason_code": "KEEP",
                            "confidence": 1.0,
                        },
                        {
                            "candidate_id": second_id,
                            "decision": "DUPLICATE_OF",
                            "duplicate_of": first_id,
                            "reason_code": "DUPLICATE",
                            "confidence": confidence,
                        },
                    ]
                },
                configured_model="test",
                actual_model="test",
                mock=True,
            )

    await validate_final_compare_candidates(result, DuplicateClient())

    assert len(result.diff_items) == 2 - expected_removed


def test_dedup_keeps_evidence_local_and_counts_exact_duplicates() -> None:
    location = DocumentLocation(page=1, paragraph_index=0)
    left = DiffSide(file_id="base", location=location, text="原文")
    right = DiffSide(file_id="target", location=location, text="新文")
    first = DiffItem(
        diff_id="diff_000001",
        diff_type="MODIFIED",
        title="变化",
        baseline=left,
        target=right,
        confidence=0.9,
    )
    duplicate = first.model_copy(update={"diff_id": "diff_000002"})

    kept, stats = deduplicate_diff_candidates([first, duplicate])

    assert kept == [first]
    assert stats["rule_deduplicated_count"] == 1


def _area_diff(
    diff_id: str,
    diff_type: str,
    *,
    area: str,
    baseline_row: int,
    target_row: int,
) -> DiffItem:
    return DiffItem(
        diff_id=diff_id,
        diff_type=diff_type,
        title="表格单元格发生变化",
        baseline=DiffSide(
            file_id="base",
            location=DocumentLocation(table_index=0, row=baseline_row, column=0, page=1),
            text="原值",
        ),
        target=DiffSide(
            file_id="target",
            location=DocumentLocation(table_index=0, row=target_row, column=0, page=1),
            text="新值",
        ),
        confidence=0.9,
        logical_area_key=area,
    )


def test_logical_area_dedup_merges_locations_and_keeps_numeric_priority() -> None:
    modified = _area_diff(
        "diff_000001", "MODIFIED", area="pair:0/cell:amount", baseline_row=1, target_row=1
    )
    numeric = _area_diff(
        "diff_000002", "NUMERIC_CHANGED", area="pair:0/cell:amount", baseline_row=2, target_row=2
    )

    kept, stats, groups = deduplicate_diff_candidates_with_audit([modified, numeric])

    assert len(kept) == 1
    assert kept[0].diff_type == "NUMERIC_CHANGED"
    assert {location.row for location in kept[0].baseline.locations} == {1, 2}
    assert stats["cross_type_merged_count"] == 1
    assert stats["logical_area_merged_count"] == 1
    assert groups[0]["reason_code"] == "CROSS_TYPE_MERGED"


def test_logical_area_dedup_preserves_all_physical_locations() -> None:
    first = _area_diff(
        "diff_000001", "TABLE_CELL_CHANGED", area="pair:0/cell:name", baseline_row=1, target_row=1
    )
    second = _area_diff(
        "diff_000002", "TABLE_CELL_CHANGED", area="pair:0/cell:name", baseline_row=2, target_row=2
    )

    kept, stats = deduplicate_diff_candidates([first, second])

    assert len(kept) == 1
    assert stats["rule_deduplicated_count"] == 1
    assert [location.row for location in kept[0].baseline.locations] == [1, 2]
    assert [location.row for location in kept[0].target.locations] == [1, 2]


def test_logical_area_dedup_does_not_cross_table_pairs() -> None:
    first = _area_diff(
        "diff_000001", "TABLE_CELL_CHANGED", area="pair:0/cell:total", baseline_row=1, target_row=1
    )
    second = _area_diff(
        "diff_000002", "TABLE_CELL_CHANGED", area="pair:1/cell:total", baseline_row=1, target_row=1
    )

    kept, stats = deduplicate_diff_candidates([first, second])

    assert len(kept) == 2
    assert stats["rule_deduplicated_count"] == 0


def test_candidate_equivalence_uses_logical_area_but_still_requires_text_identity() -> None:
    left = _area_diff(
        "diff_000001", "TABLE_CELL_CHANGED", area="pair:0/cell:amount", baseline_row=1, target_row=1
    )
    same_area = _area_diff(
        "diff_000002", "TABLE_CELL_CHANGED", area="pair:0/cell:amount", baseline_row=2, target_row=2
    )
    different_text = same_area.model_copy(
        update={"target": same_area.target.model_copy(update={"text": "另一值"})}
    )

    assert _diff_equivalent(left, same_area)
    assert not _diff_equivalent(left, different_text)
    assert not _diff_equivalent(
        left, same_area.model_copy(update={"logical_area_key": "pair:1/cell:amount"})
    )


def _cluster_comparison(group_sizes: list[int]) -> ComparisonResult:
    from app.comparison.models import ComparisonDiagnostics

    differences: list[DiffItem] = []
    candidate_records: list[dict[str, str]] = []
    candidate_number = 1
    for table_index, size in enumerate(group_sizes):
        for row in range(1, size + 1):
            candidate_id = f"candidate_cluster_{candidate_number:04d}"
            diff = DiffItem(
                diff_id=f"diff_{candidate_number:06d}",
                diff_type="TABLE_CELL_CHANGED",
                title="表格单元格发生变化",
                baseline=DiffSide(
                    file_id="base",
                    location=DocumentLocation(
                        page=1,
                        table_index=table_index,
                        row=row,
                        column=1,
                    ),
                    text="合计",
                ),
                target=DiffSide(
                    file_id="target",
                    location=DocumentLocation(
                        page=1,
                        table_index=table_index,
                        row=row,
                        column=1,
                    ),
                    text="合计",
                ),
                confidence=0.9,
                candidate_id=candidate_id,
            )
            differences.append(diff)
            candidate_records.append({"candidate_id": candidate_id})
            candidate_number += 1
    return ComparisonResult(
        diff_items=differences,
        diagnostics=ComparisonDiagnostics(
            reliable=True,
            baseline_unit_count=1,
            target_unit_count=1,
            aligned_unit_count=1,
            unmatched_baseline_count=0,
            unmatched_target_count=0,
            alignment_coverage_baseline=1,
            alignment_coverage_target=1,
            unmatched_ratio_baseline=0,
            unmatched_ratio_target=0,
            global_text_similarity=1,
            candidate_diff_count=len(differences),
            emitted_diff_count=len(differences),
            compatible_table_count=3,
            fallback_mode="FINAL_LOGICAL_V2",
        ),
        candidate_records=candidate_records,
    )


def test_suspected_clusters_select_known_group_sizes_without_cross_table_merge() -> None:
    comparison = _cluster_comparison([10, 16, 5])

    clusters = build_suspected_duplicate_clusters(comparison)

    assert max(len(cluster.candidate_ids) for cluster in clusters) <= 3
    assert len({cluster.cluster_id for cluster in clusters}) == len(clusters)
    assert all(
        cluster.discovery_action == "EQUIVALENT_NO_CHANGE" for cluster in clusters
    )


def test_deterministic_v2_filter_removes_only_safe_equivalence_without_merging_changes() -> None:
    comparison = _cluster_comparison([2])
    comparison.diff_items = [
        comparison.diff_items[0].model_copy(
            update={"diff_type": "DELETED", "target": None}
        ),
        comparison.diff_items[1].model_copy(
            update={"diff_type": "ADDED", "baseline": None}
        ),
    ]

    apply_deterministic_final_compare_filters(comparison)

    assert comparison.diff_items == []
    assert comparison.validation_stats["equivalent_filtered_count"] == 2
    assert comparison.validation_stats["boundary_noise_filtered_count"] == 0
    assert comparison.validation_stats["llm_diff_adjudication_calls"] == 0
    assert comparison.validation_stats["final_published_risk_count"] == 0


def test_deterministic_v2_filter_keeps_logical_change_clusters_as_risks() -> None:
    comparison = _cluster_comparison([2])
    for diff in comparison.diff_items:
        diff.baseline.text = "原始金额100"
        diff.target.text = "变更金额120"

    apply_deterministic_final_compare_filters(comparison)

    assert len(comparison.diff_items) == 2
    assert comparison.validation_stats["equivalent_filtered_count"] == 0
    assert comparison.validation_stats["final_published_risk_count"] == 2


def test_adjacent_added_deleted_paragraphs_form_one_local_logical_group() -> None:
    comparison = _cluster_comparison([1])
    comparison.diff_items = [
        DiffItem(
            diff_id="diff_000001",
            diff_type="DELETED",
            title="目标文件缺少内容",
            baseline=DiffSide(
                file_id="base",
                location=DocumentLocation(page=1, paragraph_index=4),
                text="第二条 租赁期限为二十四个月",
            ),
            target=None,
            confidence=0.99,
            candidate_id="candidate_deleted1",
        ),
        DiffItem(
            diff_id="diff_000002",
            diff_type="ADDED",
            title="目标文件新增内容",
            baseline=None,
            target=DiffSide(
                file_id="target",
                location=DocumentLocation(page=1, paragraph_index=5),
                text="第二条 租赁期限改为三十六个月",
            ),
            confidence=0.99,
            candidate_id="candidate_added1",
        ),
    ]

    clusters = build_suspected_duplicate_clusters(comparison)

    assert [list(cluster.candidate_ids) for cluster in clusters] == [
        ["candidate_deleted1", "candidate_added1"]
    ]


def test_same_side_paragraph_fragments_join_their_shared_replacement_block() -> None:
    comparison = _cluster_comparison([1])
    comparison.diff_items = [
        DiffItem(
            diff_id="diff_deleted_block",
            diff_type="DELETED",
            title="目标文件缺少内容",
            baseline=DiffSide(
                file_id="base",
                location=DocumentLocation(
                    page=1, paragraph_index=10, section="租赁物的保险"
                ),
                text="甲方认可的保险公司投保指定险种，保险费由乙方承担。乙方应在租赁期限内履行保险义务。",
            ),
            target=None,
            confidence=0.99,
            candidate_id="candidate_deleted_block",
        ),
        DiffItem(
            diff_id="diff_added_first",
            diff_type="ADDED",
            title="目标文件新增内容",
            baseline=None,
            target=DiffSide(
                file_id="target",
                location=DocumentLocation(
                    page=2, paragraph_index=20, section="租赁物的保险"
                ),
                text="甲方认可的保险公司投保指定险种，保险费由乙方承担。",
            ),
            confidence=0.99,
            candidate_id="candidate_added_first",
        ),
        DiffItem(
            diff_id="diff_added_second",
            diff_type="ADDED",
            title="目标文件新增内容",
            baseline=None,
            target=DiffSide(
                file_id="target",
                location=DocumentLocation(
                    page=2, paragraph_index=21, section="租赁物的保险"
                ),
                text="乙方应在租赁期限内履行保险义务。",
            ),
            confidence=0.99,
            candidate_id="candidate_added_second",
        ),
    ]

    clusters = build_suspected_duplicate_clusters(comparison)

    assert [set(cluster.candidate_ids) for cluster in clusters] == [
        {
            "candidate_deleted_block",
            "candidate_added_first",
            "candidate_added_second",
        }
    ]


def test_same_chapter_distant_additions_do_not_form_a_split_group() -> None:
    comparison = _cluster_comparison([1])
    comparison.diff_items = [
        DiffItem(
            diff_id="diff_added_near",
            diff_type="ADDED",
            title="目标文件新增内容",
            baseline=None,
            target=DiffSide(
                file_id="target",
                location=DocumentLocation(
                    page=1, paragraph_index=1, section="租赁物的保险"
                ),
                text="保险费由乙方承担。",
            ),
            confidence=0.99,
            candidate_id="candidate_added_near",
        ),
        DiffItem(
            diff_id="diff_added_far",
            diff_type="ADDED",
            title="目标文件新增内容",
            baseline=None,
            target=DiffSide(
                file_id="target",
                location=DocumentLocation(
                    page=8, paragraph_index=20, section="租赁物的保险"
                ),
                text="保险事故应当及时通知甲方。",
            ),
            confidence=0.99,
            candidate_id="candidate_added_far",
        ),
    ]

    assert build_suspected_duplicate_clusters(comparison) == []


@pytest.mark.asyncio
async def test_duplicate_cluster_llm_merges_only_complete_safe_clusters() -> None:
    comparison = _cluster_comparison([10, 16, 5])

    class MergeClient:
        async def validate_final_compare_duplicate_clusters(
            self, payload: dict
        ) -> LlmResult:
            return LlmResult(
                value={
                    "clusters": [
                        {
                            "cluster_id": cluster["cluster_id"],
                            "decision": "SAME_LOGICAL_DIFF",
                            "representative_candidate_id": cluster["candidate_ids"][0],
                            "duplicate_candidate_ids": cluster["candidate_ids"][1:],
                            "reason_code": "MERGED_CELL_GRID_EXPANSION",
                            "confidence": 0.98,
                        }
                        for cluster in payload["clusters"]
                    ]
                },
                configured_model="GLM-5.3-Flash",
                actual_model="GLM-5.3-Flash",
                mock=True,
                response_format="json_schema",
                finish_reason="stop",
            )

    await validate_final_compare_duplicate_clusters(comparison, MergeClient())

    assert len(comparison.diff_items) == 16
    assert comparison.validation_stats["llm_same_logical_count"] == 15
    assert comparison.validation_stats["llm_removed_candidate_count"] == 15
    assert all(
        len(cluster["removed_candidate_ids"]) <= 2
        for cluster in comparison.dedup_groups
    )


@pytest.mark.asyncio
async def test_logical_group_protocol_merges_only_complete_candidate_sets() -> None:
    comparison = _cluster_comparison([2])

    class GroupClient:
        async def validate_final_compare_duplicate_clusters(
            self, payload: dict
        ) -> LlmResult:
            groups = [
                {
                    "group_id": item["group_id"],
                    "candidate_ids": item["candidate_ids"],
                    "decision": "SAME_LOGICAL_CHANGE",
                    "reason_code": "MERGED_LOGICAL_REGION",
                    "confidence": 0.97,
                }
                for item in payload["groups"]
            ]
            return LlmResult(
                value={"groups": groups},
                configured_model="GLM-5.3-Flash",
                actual_model="GLM-5.3-Flash",
                mock=True,
                response_format="json_schema",
                finish_reason="stop",
            )

    await validate_final_compare_duplicate_clusters(comparison, GroupClient())

    assert len(comparison.diff_items) == 1
    assert comparison.validation_stats["llm_same_logical_change_count"] == 1
    assert comparison.validation_stats["llm_removed_candidate_count"] == 1
    assert comparison.dedup_groups[0]["group_id"].startswith("group_")


@pytest.mark.asyncio
async def test_equivalent_no_change_requires_normalized_text_equality() -> None:
    comparison = _cluster_comparison([2])

    class GroupClient:
        async def validate_final_compare_duplicate_clusters(
            self, payload: dict
        ) -> LlmResult:
            item = payload["groups"][0]
            return LlmResult(
                value={
                    "groups": [
                        {
                            "group_id": item["group_id"],
                            "candidate_ids": item["candidate_ids"],
                            "decision": "EQUIVALENT_NO_CHANGE",
                            "reason_code": "EQUIVALENT_LAYOUT",
                            "confidence": 0.99,
                        }
                    ]
                },
                configured_model="GLM-5.3-Flash",
                actual_model="GLM-5.3-Flash",
                mock=True,
                response_format="json_schema",
                finish_reason="stop",
            )

    await validate_final_compare_duplicate_clusters(comparison, GroupClient())

    assert comparison.diff_items == []
    assert comparison.validation_stats["llm_equivalent_no_change_count"] == 1


@pytest.mark.asyncio
async def test_logical_group_failure_retains_candidates_for_review() -> None:
    comparison = _cluster_comparison([2])

    class FailingClient:
        async def validate_final_compare_duplicate_clusters(self, _payload: dict) -> LlmResult:
            raise RuntimeError("provider response must not escape")

    await validate_final_compare_duplicate_clusters(comparison, FailingClient())

    assert len(comparison.diff_items) == 2
    assert all(item.validation_status == "REVIEW_REQUIRED" for item in comparison.diff_items)
    assert comparison.validation_stats["validation_failure_count"] == 2


def test_numeric_change_is_eligible_for_logical_change_cluster() -> None:
    comparison = _cluster_comparison([2])
    for diff in comparison.diff_items:
        diff.diff_type = "NUMERIC_CHANGED"
        diff.baseline.text = "100"
        diff.target.text = "120"

    clusters = build_suspected_duplicate_clusters(comparison)

    assert len(clusters) == 1
    assert len(clusters[0].candidate_ids) == 2
    assert clusters[0].payload["candidate_kind"] == "VALUE_CHANGE_OR_VALUE_CONTEXT"


def test_logical_group_finds_distant_added_deleted_paragraphs_by_context() -> None:
    comparison = _cluster_comparison([1])
    comparison.diff_items = [
        DiffItem(
            diff_id="diff_000001",
            diff_type="DELETED",
            title="目标文件缺少内容",
            baseline=DiffSide(
                file_id="base",
                location=DocumentLocation(page=2, paragraph_index=75),
                text="第七十五条 租金支付方式按约定执行。",
            ),
            target=None,
            confidence=0.99,
            candidate_id="candidate_distant_deleted",
        ),
        DiffItem(
            diff_id="diff_000002",
            diff_type="ADDED",
            title="目标文件新增内容",
            baseline=None,
            target=DiffSide(
                file_id="target",
                location=DocumentLocation(page=6, paragraph_index=88),
                text="第八十八条 租金支付方式按约定执行。",
            ),
            confidence=0.99,
            candidate_id="candidate_distant_added",
        ),
    ]

    clusters = build_suspected_duplicate_clusters(comparison)

    assert [list(cluster.candidate_ids) for cluster in clusters] == [
        ["candidate_distant_deleted", "candidate_distant_added"]
    ]


def test_contiguous_deleted_heading_and_list_fragments_form_one_block() -> None:
    comparison = _cluster_comparison([1])
    comparison.diff_items = [
        DiffItem(
            diff_id=f"diff_deleted_{index}",
            diff_type="DELETED",
            title="目标文件缺少内容",
            baseline=DiffSide(
                file_id="base",
                location=DocumentLocation(
                    page=12,
                    paragraph_index=index,
                    section="第十五条陈述和保证",
                ),
                locations=[
                    DocumentLocation(
                        page=12,
                        paragraph_index=paragraph_index,
                        section="第十五条陈述和保证",
                    )
                    for paragraph_index in range(index, index + 2)
                ],
                text=text,
            ),
            target=None,
            confidence=0.99,
            candidate_id=f"candidate_deleted_{index}",
        )
        for index, text in (
            (165, "11.担保人发生下列情形之一的"),
            (167, "1.经营或财务状况严重恶化2.到期债务未偿还"),
            (169, "3.担保人卷入法律纠纷4.抵押物价值减少"),
            (171, "5.停产歇业或破产6.其他影响担保能力的事件"),
        )
    ]

    clusters = build_suspected_duplicate_clusters(comparison)

    assert len(clusters) == 1
    assert list(clusters[0].candidate_ids) == [
        "candidate_deleted_165",
        "candidate_deleted_167",
        "candidate_deleted_169",
        "candidate_deleted_171",
    ]
    assert comparison.validation_metadata["candidate_discovery"][
        "relation_reasons"
    ]["CONTIGUOUS_DELETION_BLOCK"] > 0


def test_v2_quality_audit_reports_safe_duplicate_and_change_counts() -> None:
    location = DocumentLocation(page=2, table_index=0, row=3, column=1)
    first = DiffItem(
        diff_id="diff_000001",
        diff_type="NUMERIC_CHANGED",
        title="通用分类标题",
        baseline=DiffSide(
            file_id="base",
            location=location,
            text="租赁期限24个月，金额100万元，编号AB-123",
        ),
        target=DiffSide(
            file_id="target",
            location=location,
            text="租赁期限36个月，金额120万元，编号AB-124",
        ),
        confidence=1.0,
        validation_status="REVIEW_REQUIRED",
    )
    duplicate = first.model_copy(update={"diff_id": "diff_000002"})
    cross_type = first.model_copy(
        update={"diff_id": "diff_000003", "diff_type": "MODIFIED"}
    )
    comparison = _cluster_comparison([1])
    comparison.diff_items = [first, duplicate, cross_type]
    comparison.warnings = []

    audit = build_v2_quality_audit(comparison)

    assert audit["candidate_count"] == 3
    assert audit["exact_duplicate_signature_count"] == 1
    assert audit["exact_duplicate_excess_count"] == 1
    assert audit["same_position_cross_type_group_count"] == 1
    assert audit["same_position_cross_type_excess_count"] == 1
    assert audit["review_required_count"] == 3
    assert audit["bilateral_normalized_text_different_count"] == 3
    assert audit["numeric_change_count"] == 2
    assert audit["amount_change_count"] == 3
    assert audit["date_change_count"] == 0
    assert audit["term_change_count"] == 3
    assert audit["identifier_change_count"] == 3
    assert "租赁期限" not in str(audit)


def test_empty_duplicate_canary_is_a_successful_no_call() -> None:
    from scripts.final_compare_duplicate_canary import empty_cluster_canary_result

    result = empty_cluster_canary_result()

    assert result["status"] == "SKIPPED_NO_CANDIDATES"
    assert result["llm_calls"] == 0
    assert result["ocr_calls"] == 0
    assert result["database_writes"] == 0


def test_first_pair_gold_manifest_is_deidentified_and_balanced() -> None:
    manifest_path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "final_compare_gold"
        / "first_pair_deidentified.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    gold = manifest["logical_gold"]

    assert sum(item["fragment_count"] for item in gold["fragment_groups"]) == 41
    assert len(gold["fragment_groups"]) == 17
    assert len(gold["false_positives"]) == 12
    assert all("text" not in item and "raw_text" not in item for item in gold["fragment_groups"])
    assert len(gold["equivalent_groups"]) == 5
    assert sum(item["fragment_count"] for item in gold["equivalent_groups"]) == 11
    assert len(gold["boundary_noise"]) == 1
    assert sum(item["fragment_count"] for item in gold["equivalent_groups"]) + len(
        gold["boundary_noise"]
    ) == 12
    assert all(
        "topology_fingerprint" in item
        for item in [*gold["fragment_groups"], *gold["equivalent_groups"], *gold["boundary_noise"]]
    )


def test_gold_audit_rejects_placeholder_equivalence_fingerprints() -> None:
    comparison = _cluster_comparison([2])
    clusters = build_suspected_duplicate_clusters(comparison)
    manifest = {
        "logical_gold": {
            "fragment_groups": [],
            "equivalent_groups": [
                {
                    "gold_group_id": "eq_placeholder",
                    "fragment_count": 2,
                    "expected": "EQUIVALENT_NO_CHANGE",
                    "topology_fingerprint": "0" * 24,
                }
            ],
            "boundary_noise": [],
        }
    }

    audit = build_candidate_discovery_gold_audit(comparison, clusters, manifest)

    assert audit["status"] == "FAILED"
    assert audit["failure_code"] == "GOLD_TOPOLOGY_SIGNATURE_PLACEHOLDER"
    assert audit["missing_gold_ids"] == ["eq_placeholder"]


def test_gold_audit_rejects_stale_capture_file_sha() -> None:
    comparison = _cluster_comparison([1])
    manifest = {
        "capture_metadata": {
            "baseline_sha256": "f" * 64,
            "target_sha256": "b" * 64,
        },
        "logical_gold": {"fragment_groups": [], "equivalent_groups": []},
    }
    audit = build_candidate_discovery_gold_audit(
        comparison,
        [],
        manifest,
        baseline=_paragraph_document("base", "BASELINE", ["原文"]),
        target=_paragraph_document("target", "TARGET", ["新文"]),
    )

    assert audit == {"status": "FAILED", "failure_code": "GOLD_MANIFEST_STALE"}


def test_equivalent_layout_fragments_are_classified_without_body_diagnostics() -> None:
    comparison = _cluster_comparison([3, 2, 2, 2, 2])
    clusters = build_suspected_duplicate_clusters(comparison)

    assert len(clusters) == 5
    assert all(
        cluster.discovery_action == "EQUIVALENT_NO_CHANGE" for cluster in clusters
    )
    assert all(
        cluster.payload["discovery_action"] == "EQUIVALENT_NO_CHANGE"
        for cluster in clusters
    )


def test_gold_audit_matches_actual_topology_once_and_not_declared_counts() -> None:
    comparison = _cluster_comparison([2])
    clusters = build_suspected_duplicate_clusters(comparison)
    by_id = {str(diff.candidate_id): diff for diff in comparison.diff_items}
    fingerprint = candidate_topology_fingerprint(clusters[0].candidate_ids, by_id)
    manifest = {
        "logical_gold": {
            "fragment_groups": [],
            "equivalent_groups": [
                {
                    "gold_group_id": "eq_01",
                    "fragment_count": 2,
                    "expected": "EQUIVALENT_NO_CHANGE",
                    "topology_fingerprint": fingerprint,
                }
            ],
            "boundary_noise": [],
        }
    }

    audit = build_candidate_discovery_gold_audit(comparison, clusters, manifest)

    assert audit["status"] == "PASSED"
    assert audit["matched_group_count"] == 1
    assert audit["matched_false_positive_count"] == 2

    manifest["logical_gold"]["equivalent_groups"][0]["topology_fingerprint"] = "0" * 24
    failed = build_candidate_discovery_gold_audit(comparison, clusters, manifest)
    assert failed["status"] == "FAILED"
    assert failed["missing_gold_ids"] == ["eq_01"]


def test_gold_replay_applies_only_topology_matched_actions() -> None:
    comparison = _cluster_comparison([2, 1])
    clusters = build_suspected_duplicate_clusters(comparison)
    by_id = {str(diff.candidate_id): diff for diff in comparison.diff_items}
    fingerprint = candidate_topology_fingerprint(clusters[0].candidate_ids, by_id)
    manifest = {
        "logical_gold": {
            "fragment_groups": [],
            "equivalent_groups": [
                {
                    "gold_group_id": "eq_01",
                    "fragment_count": 2,
                    "expected": "EQUIVALENT_NO_CHANGE",
                    "topology_fingerprint": fingerprint,
                }
            ],
            "boundary_noise": [],
            "local_replay": {
                "formal_risk_count": 1,
                "review_required_count": 0,
                "date_passed_conflict_count": 0,
                "rent_payment_plan_missing_count": 0,
                "page_coverage_required": False,
            },
        }
    }

    audit = build_candidate_discovery_gold_audit(comparison, clusters, manifest)
    replay = replay_final_compare_gold(comparison, clusters, audit)

    assert audit["status"] == "PASSED"
    assert replay["status"] == "PASSED"
    assert replay["formal_risk_count"] == 1
    assert replay["removed_candidate_count"] == 2
    assert replay["llm_calls"] == 0
    assert replay["database_writes"] == 0


def test_boundary_noise_is_tracked_separately_from_llm_clusters() -> None:
    comparison = _cluster_comparison([1])
    comparison.diff_items[0] = comparison.diff_items[0].model_copy(
        update={
            "review_reason": "OCR_READING_ORDER_VARIANCE",
            "baseline": comparison.diff_items[0].baseline.model_copy(update={"text": "分页标题。"}),
            "target": comparison.diff_items[0].target.model_copy(update={"text": "分页标题"}),
        }
    )
    clusters = build_suspected_duplicate_clusters(comparison)

    assert clusters == []
    assert comparison.validation_metadata["candidate_discovery"]["boundary_noise_count"] == 1


def test_canary_selection_requires_real_categories_in_stable_order() -> None:
    clusters = [
        DuplicateCluster(
            cluster_id=f"cluster_{index}",
            candidate_ids=(f"candidate_{index}", f"candidate_{index}_2"),
            payload={
                "candidate_kind": "TABLE_LAYOUT_EQUIVALENCE"
                if category.endswith("EQUIVALENCE")
                else "TABLE_FIELD_CROSS_SIDE",
                "candidate_coordinate_kind": "TABLE"
                if category != "PARAGRAPH_MERGE"
                else "PARAGRAPH",
            },
            discovery_action=(
                "EQUIVALENT_NO_CHANGE"
                if category.endswith("EQUIVALENCE")
                else "SAME_LOGICAL_CHANGE"
            ),
        )
        for index, category in enumerate(
            (
                "PARAGRAPH_MERGE",
                "TABLE_MERGE",
                "FORMULA_EQUIVALENCE",
                "TABLE_FIELD_EQUIVALENCE",
            )
        )
    ]
    # The category is a diagnostic-only explicit value; it must still be
    # selected from actual clusters rather than fabricated by the caller.
    for cluster, category in zip(
        clusters,
        (
            "PARAGRAPH_MERGE",
            "TABLE_MERGE",
            "FORMULA_EQUIVALENCE",
            "TABLE_FIELD_EQUIVALENCE",
        ),
        strict=True,
    ):
        cluster.payload["canary_category"] = category

    selected, error = select_canary_clusters(clusters)

    assert error is None
    assert [cluster.payload["canary_category"] for cluster in selected] == [
        "PARAGRAPH_MERGE",
        "TABLE_MERGE",
        "FORMULA_EQUIVALENCE",
        "TABLE_FIELD_EQUIVALENCE",
    ]


def test_complete_relation_groups_maximize_coverage_for_overlapping_cliques() -> None:
    template = _cluster_comparison([1]).diff_items[0]
    candidates = [
        template.model_copy(
            update={"candidate_id": f"candidate_{index}", "diff_id": f"diff_{index}"}
        )
        for index in range(6)
    ]
    compatibility = {
        frozenset({0, 1}),
        frozenset({0, 2}),
        frozenset({1, 2}),
        frozenset({2, 3}),
        frozenset({2, 4}),
        frozenset({3, 4}),
        frozenset({3, 5}),
        frozenset({4, 5}),
        frozenset({3, 5}),
    }
    candidate_indexes = {id(candidate): index for index, candidate in enumerate(candidates)}

    def is_compatible(left: DiffItem, right: DiffItem) -> bool:
        left_index = candidate_indexes[id(left)]
        right_index = candidate_indexes[id(right)]
        return frozenset({left_index, right_index}) in compatibility

    groups, remaining = _complete_relation_groups(
        list(range(6)),
        candidates,
        compatible=is_compatible,
        max_size=3,
    )

    assert {frozenset(group) for group in groups} == {
        frozenset({0, 1, 2}),
        frozenset({3, 4, 5}),
    }
    assert remaining == 0


def test_table_equivalence_uses_business_field_and_standardized_value() -> None:
    baseline = _coordinate_table_document(
        "base",
        [
            [(0, "项目", "h0"), (1, "期数", "h1")],
            [(0, "租金", "r0"), (1, "共7期", "r1")],
        ],
    )
    target = _coordinate_table_document(
        "target",
        [
            [(0, "项目", "h0"), (1, "期数", "h1")],
            [(0, "租金", "r0"), (1, "7期", "r1")],
        ],
    )
    left = DiffItem(
        diff_id="diff_delete_period",
        diff_type="DELETED",
        title="表格内容",
        baseline=DiffSide(
            file_id="base",
            location=DocumentLocation(page=1, table_index=0, row=1, column=1),
            text="共7期",
        ),
        target=None,
        confidence=1,
        candidate_id="candidate_delete_period",
    )
    right = DiffItem(
        diff_id="diff_add_period",
        diff_type="ADDED",
        title="表格内容",
        baseline=None,
        target=DiffSide(
            file_id="target",
            location=DocumentLocation(page=1, table_index=0, row=1, column=1),
            text="7期",
        ),
        confidence=1,
        candidate_id="candidate_add_period",
    )

    assert _equivalent_candidate_pair(
        left, right, baseline=baseline, target=target
    )


def test_table_equivalence_survives_shifted_rows_for_a_unique_field_value() -> None:
    baseline = _coordinate_table_document(
        "base",
        [
            [(0, "项目", "h0"), (1, "期数", "h1")],
            [(0, "租金", "r0"), (1, "共7期", "r1")],
        ],
    )
    target = _coordinate_table_document(
        "target",
        [
            [(0, "项目", "h0"), (1, "期数", "h1")],
            [(0, "其他行", "x0"), (1, "金额", "x1")],
            [(0, "租金", "r2"), (1, "7期", "r3")],
        ],
    )
    left = DiffItem(
        diff_id="diff_shifted_delete",
        diff_type="DELETED",
        title="表格内容",
        baseline=DiffSide(
            file_id="base",
            location=DocumentLocation(page=1, table_index=0, row=1, column=1),
            text="共7期",
        ),
        target=None,
        confidence=1,
        candidate_id="candidate_shifted_delete",
    )
    right = DiffItem(
        diff_id="diff_shifted_add",
        diff_type="ADDED",
        title="表格内容",
        baseline=None,
        target=DiffSide(
            file_id="target",
            location=DocumentLocation(page=1, table_index=0, row=2, column=1),
            text="7期",
        ),
        confidence=1,
        candidate_id="candidate_shifted_add",
    )

    assert _equivalent_candidate_pair(
        left, right, baseline=baseline, target=target
    )


def test_equivalence_bucket_over_three_is_blocked_and_not_sent_as_logic() -> None:
    baseline = _coordinate_table_document(
        "base",
        [
            [(0, "项目", "h0"), (1, "期数", "h1")],
            [(0, "租金", "r1"), (1, "共7期", "v1")],
            [(0, "租金", "r2"), (1, "共7期", "v2")],
            [(0, "租金", "r3"), (1, "共7期", "v3")],
            [(0, "租金", "r4"), (1, "共7期", "v4")],
        ],
    )
    target = _coordinate_table_document("target", [[(0, "项目", "h0"), (1, "期数", "h1")]])
    comparison = _cluster_comparison([1])
    comparison.diff_items = [
        DiffItem(
            diff_id=f"diff_overmerged_{row}",
            diff_type="DELETED",
            title="表格内容",
            baseline=DiffSide(
                file_id="base",
                location=DocumentLocation(page=1, table_index=0, row=row, column=1),
                text="共7期",
            ),
            target=None,
            confidence=1,
            candidate_id=f"candidate_overmerged_{row}",
        )
        for row in range(1, 5)
    ]

    clusters = build_suspected_duplicate_clusters(
        comparison, baseline=baseline, target=target
    )

    discovery = comparison.validation_metadata["candidate_discovery"]
    assert clusters == []
    assert discovery["failure_code"] == "EQUIVALENT_COMPONENT_OVERMERGED"
    assert discovery["equivalence_overmerged_candidate_count"] == 4


def test_formula_equivalence_is_checked_after_group_concatenation() -> None:
    comparison = _cluster_comparison([1])
    comparison.diff_items = [
        DiffItem(
            diff_id=f"diff_formula_{index}",
            diff_type=diff_type,
            title="内容变化",
            baseline=baseline_side,
            target=target_side,
            confidence=1,
            candidate_id=f"candidate_formula_{index}",
        )
        for index, (diff_type, baseline_side, target_side) in enumerate(
            (
                (
                    "DELETED",
                    DiffSide(
                        file_id="base",
                        location=DocumentLocation(
                            page=1, paragraph_index=1, section="公式条款"
                        ),
                        text="公式",
                    ),
                    None,
                ),
                (
                    "DELETED",
                    DiffSide(
                        file_id="base",
                        location=DocumentLocation(
                            page=1, paragraph_index=2, section="公式条款"
                        ),
                        text="租金=5%",
                    ),
                    None,
                ),
                (
                    "ADDED",
                    None,
                    DiffSide(
                        file_id="target",
                        location=DocumentLocation(
                            page=1, paragraph_index=3, section="公式条款"
                        ),
                        text="公式租金=5%",
                    ),
                ),
            ),
            start=1,
        )
    ]

    clusters = build_suspected_duplicate_clusters(comparison)

    assert any(
        cluster.discovery_action == "EQUIVALENT_NO_CHANGE"
        and len(cluster.candidate_ids) == 3
        for cluster in clusters
    )


def test_boundary_noise_requires_containment_by_a_nearby_block() -> None:
    comparison = _cluster_comparison([1])
    noise = comparison.diff_items[0].model_copy(
        update={
            "baseline": comparison.diff_items[0].baseline.model_copy(
                update={"text": "分页标题"}
            ),
            "target": None,
        }
    )
    containing = noise.model_copy(
        update={
            "diff_id": "diff_containing",
            "candidate_id": "candidate_containing",
            "baseline": noise.baseline.model_copy(
                update={
                    "text": "分页标题正文",
                    "location": DocumentLocation(
                        page=1, table_index=0, row=2, column=1
                    ),
                }
            ),
        }
    )

    assert _boundary_noise_reason(noise, [noise, containing], 0) == "BOUNDARY_NOISE"


def test_candidate_graph_uses_key_value_subnumbers_without_cross_row_merge() -> None:
    baseline = _coordinate_table_document(
        "base",
        [
            [(0, "租金", "group") , (1, "项目", "item")],
            [(0, "租金", "group-1"), (1, "1. 每期租金", "item-1")],
            [(0, "租金", "group-2"), (1, "2. 租金期数", "item-2")],
        ],
        header="租金|项目",
    )
    target = _coordinate_table_document(
        "target",
        [
            [(0, "租金", "target-group"), (1, "项目", "target-item")],
            [(0, "租金", "target-group-1"), (1, "1. 每期租金", "target-item-1")],
            [(0, "租金", "target-group-2"), (1, "2. 租金期数", "target-item-2")],
        ],
        header="租金|项目",
    )
    comparison = _cluster_comparison([1])
    comparison.diff_items = [
        DiffItem(
            diff_id="diff_delete_1",
            diff_type="DELETED",
            title="目标文件缺少内容",
            baseline=DiffSide(
                file_id="base",
                location=DocumentLocation(table_index=0, row=1, column=1, page=1),
                text="1. 每期租金（旧）",
            ),
            target=None,
            confidence=0.9,
            candidate_id="candidate_delete_1",
        ),
        DiffItem(
            diff_id="diff_add_1",
            diff_type="ADDED",
            title="目标文件新增内容",
            baseline=None,
            target=DiffSide(
                file_id="target",
                location=DocumentLocation(table_index=0, row=1, column=1, page=1),
                text="1. 每期租金（新）",
            ),
            confidence=0.9,
            candidate_id="candidate_add_1",
        ),
        DiffItem(
            diff_id="diff_delete_2",
            diff_type="DELETED",
            title="目标文件缺少内容",
            baseline=DiffSide(
                file_id="base",
                location=DocumentLocation(table_index=0, row=2, column=1, page=1),
                text="2. 租金期数（旧）",
            ),
            target=None,
            confidence=0.9,
            candidate_id="candidate_delete_2",
        ),
        DiffItem(
            diff_id="diff_add_2",
            diff_type="ADDED",
            title="目标文件新增内容",
            baseline=None,
            target=DiffSide(
                file_id="target",
                location=DocumentLocation(table_index=0, row=2, column=1, page=1),
                text="2. 租金期数（新）",
            ),
            confidence=0.9,
            candidate_id="candidate_add_2",
        ),
    ]

    clusters = build_suspected_duplicate_clusters(
        comparison, baseline=baseline, target=target
    )

    assert {frozenset(cluster.candidate_ids) for cluster in clusters} == {
        frozenset(("candidate_delete_1", "candidate_add_1")),
        frozenset(("candidate_delete_2", "candidate_add_2")),
    }


def test_candidate_graph_does_not_cross_business_fields_in_one_table() -> None:
    baseline = _coordinate_table_document(
        "base",
        [
            [(0, "序号", "h0"), (1, "名称", "h1"), (2, "金额", "h2")],
            [(0, "1", "r1"), (1, "设备A", "name-1"), (2, "100", "amount-1")],
        ],
        header="序号|名称|金额",
    )
    target = _coordinate_table_document(
        "target",
        [
            [(0, "序号", "th0"), (1, "名称", "th1"), (2, "金额", "th2")],
            [(0, "1", "tr1"), (1, "设备B", "tname-1"), (2, "120", "tamount-1")],
        ],
        header="序号|名称|金额",
    )
    comparison = _cluster_comparison([1])
    comparison.diff_items = [
        DiffItem(
            diff_id="diff_name",
            diff_type="TABLE_CELL_CHANGED",
            title="表格单元格发生变化",
            baseline=DiffSide(
                file_id="base",
                location=DocumentLocation(table_index=0, row=1, column=1, page=1),
                text="设备A",
            ),
            target=DiffSide(
                file_id="target",
                location=DocumentLocation(table_index=0, row=1, column=1, page=1),
                text="设备B",
            ),
            confidence=0.9,
            candidate_id="candidate_name",
        ),
        DiffItem(
            diff_id="diff_amount",
            diff_type="NUMERIC_CHANGED",
            title="数值发生变化",
            baseline=DiffSide(
                file_id="base",
                location=DocumentLocation(table_index=0, row=1, column=2, page=1),
                text="100",
            ),
            target=DiffSide(
                file_id="target",
                location=DocumentLocation(table_index=0, row=1, column=2, page=1),
                text="120",
            ),
            confidence=0.9,
            candidate_id="candidate_amount",
        ),
    ]

    clusters = build_suspected_duplicate_clusters(
        comparison, baseline=baseline, target=target
    )

    assert clusters == []
