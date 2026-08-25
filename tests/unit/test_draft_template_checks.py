import pytest

from app.comparison.models import DiffItem, DiffSide
from app.core.config import Settings
from app.core.errors import WorkflowError
from app.documents.models import (
    DocumentBlock,
    DocumentLocation,
    ParsedDocument,
    ParsedTable,
    TableCell,
    TableRow,
)
from app.draft_review.template_checks import _coalesce_positional_fills, analyze_template
from app.workflows.draft_review import DraftReviewWorkflowExecutor


def paragraph_document(file_id: str, role: str, *texts: str) -> ParsedDocument:
    return ParsedDocument(
        file_id=file_id,
        role=role,
        file_name=f"{role.lower()}.docx",
        sha256="a" * 64,
        page_count=None,
        blocks=[
            DocumentBlock(
                block_id=f"{file_id}_p{index}",
                type="PARAGRAPH",
                order=index,
                raw_text=text,
                normalized_text=text,
                location=DocumentLocation(paragraph_index=index),
            )
            for index, text in enumerate(texts)
        ],
        parser_name="python-docx",
    )


def table_document(file_id: str, role: str, value: str) -> ParsedDocument:
    rows = [
        TableRow(
            row=0,
            cells=[
                TableCell(
                    raw_text="字段",
                    normalized_text="字段",
                    location=DocumentLocation(table_index=0, row=0, column=0),
                ),
                TableCell(
                    raw_text="填写值",
                    normalized_text="填写值",
                    location=DocumentLocation(table_index=0, row=0, column=1),
                ),
            ],
        ),
        TableRow(
            row=1,
            cells=[
                TableCell(
                    raw_text="融资金额",
                    normalized_text="融资金额",
                    location=DocumentLocation(table_index=0, row=1, column=0),
                ),
                TableCell(
                    raw_text=value,
                    normalized_text=value,
                    location=DocumentLocation(table_index=0, row=1, column=1),
                ),
            ],
        ),
    ]
    table = ParsedTable(table_index=0, rows=rows)
    return ParsedDocument(
        file_id=file_id,
        role=role,
        file_name=f"{role.lower()}.docx",
        sha256="b" * 64,
        page_count=None,
        blocks=[
            DocumentBlock(
                block_id=f"{file_id}_t0",
                type="TABLE",
                order=0,
                raw_text="\n".join("\t".join(cell.raw_text for cell in row.cells) for row in rows),
                normalized_text="\n".join(
                    "\t".join(cell.normalized_text for cell in row.cells) for row in rows
                ),
                location=DocumentLocation(table_index=0),
                table=table,
            )
        ],
        parser_name="python-docx",
    )


def test_filled_template_placeholder_is_filtered_but_retained_for_traceability() -> None:
    template = paragraph_document(
        "fil_template", "TEMPLATE", "第一条 融资金额为##{融资金额}万元。"
    )
    target = paragraph_document("fil_target", "TARGET", "第一条 融资金额为1000万元。")

    result = analyze_template(template, target)

    assert result.diff_items == []
    assert result.failed_rule_checks == []
    assert result.diagnostics.raw_diff_count == 1
    assert result.diagnostics.filtered_diff_count == 1
    filtered = result.diagnostics.filtered_diff_items[0]
    assert filtered.filter_reason == "TEMPLATE_FILL_ALLOWED"
    assert filtered.diff.diff_type == "MODIFIED"
    assert filtered.diff.baseline and filtered.diff.target


def test_draft_template_aggregates_contiguous_missing_content() -> None:
    template = paragraph_document(
        "fil_template",
        "TEMPLATE",
        "第一条 共同开始内容。",
        "第二条 连续缺失内容甲。",
        "第三条 连续缺失内容乙。",
        "第四条 共同结束内容。",
    )
    target = paragraph_document(
        "fil_target",
        "TARGET",
        "第一条 共同开始内容。",
        "第四条 共同结束内容。",
    )

    result = analyze_template(template, target)

    assert len(result.diff_items) == 1
    assert result.diff_items[0].diff_type == "CONTENT_BLOCK_MISSING"
    assert result.diff_items[0].missing_detail is not None
    assert result.diff_items[0].missing_detail.structure_unit_count == 2


def test_unreplaced_placeholder_and_blank_marker_fail_with_locations() -> None:
    template = paragraph_document(
        "fil_template",
        "TEMPLATE",
        "第一条 合同编号：##{合同编号}",
        "第二条 付款日期：________",
    )
    target = paragraph_document(
        "fil_target",
        "TARGET",
        "第一条 合同编号：##{合同编号}",
        "第二条 付款日期：________",
    )

    result = analyze_template(template, target)

    assert {item["rule_id"].split(".")[1] for item in result.failed_rule_checks} == {
        "unresolved_placeholder",
        "unresolved_blank",
    }
    assert all(item["location"]["file_id"] == "fil_target" for item in result.failed_rule_checks)
    assert all(
        item["location"]["paragraph_index"] is not None
        for item in result.failed_rule_checks
    )


def test_fixed_numeric_and_text_changes_remain_business_diffs() -> None:
    template = paragraph_document(
        "fil_template",
        "TEMPLATE",
        "第一条 租赁期限固定为24个月。",
        "第二条 未经出租人同意不得转让。",
    )
    target = paragraph_document(
        "fil_target",
        "TARGET",
        "第一条 租赁期限固定为36个月。",
        "第二条 经出租人同意可以转让。",
    )

    result = analyze_template(template, target)

    assert [item.diff_type for item in result.diff_items] == ["NUMERIC_CHANGED", "MODIFIED"]
    assert result.diagnostics.filtered_diff_count == 0
    assert all(item.baseline and item.target for item in result.diff_items)


def test_required_empty_table_cell_is_reported_but_filled_cell_is_allowed() -> None:
    template = table_document("fil_template", "TEMPLATE", "")
    empty_target = table_document("fil_target", "TARGET", "")
    filled_target = table_document("fil_target", "TARGET", "1000万元")

    empty_result = analyze_template(template, empty_target)
    filled_result = analyze_template(template, filled_target)

    assert [item["rule_id"].split(".")[1] for item in empty_result.failed_rule_checks] == [
        "required_table_cell_empty"
    ]
    assert empty_result.failed_rule_checks[0]["location"] == {
        "file_id": "fil_target",
        "table_index": 0,
        "row": 1,
        "column": 1,
    }
    assert filled_result.failed_rule_checks == []
    assert filled_result.diff_items == []
    assert filled_result.diagnostics.filtered_diff_count == 1


def test_identical_fixed_content_passes_without_rule_failures() -> None:
    template = paragraph_document("fil_template", "TEMPLATE", "第一条 固定内容保持不变。")
    target = paragraph_document("fil_target", "TARGET", "第一条 固定内容保持不变。")

    result = analyze_template(template, target)

    assert result.diff_items == []
    assert result.failed_rule_checks == []
    assert result.diagnostics.comparison.reliable is True


def test_expanded_template_table_does_not_make_paragraph_alignment_unreliable() -> None:
    template = paragraph_document("fil_template", "TEMPLATE", "第一条 固定内容保持不变。")
    target = paragraph_document("fil_target", "TARGET", "第一条 固定内容保持不变。")
    template.blocks.extend(table_document("fil_template", "TEMPLATE", "").blocks)
    expanded = table_document("fil_target", "TARGET", "已填写")
    expanded.blocks[0].table.rows.append(
        TableRow(
            row=2,
            cells=[
                TableCell(
                    raw_text="附加字段",
                    normalized_text="附加字段",
                    location=DocumentLocation(table_index=0, row=2, column=0),
                ),
                TableCell(
                    raw_text="附加内容",
                    normalized_text="附加内容",
                    location=DocumentLocation(table_index=0, row=2, column=1),
                ),
            ],
        )
    )
    target.blocks.extend(expanded.blocks)

    result = analyze_template(template, target)

    assert result.diagnostics.comparison.reliable is True
    assert result.diagnostics.expanded_table_count == 1
    assert result.diff_items == []
    assert [warning.code for warning in result.warnings] == [
        "TEMPLATE_TABLE_STRUCTURE_EXPANDED"
    ]
    executor = DraftReviewWorkflowExecutor(Settings(_env_file=None))
    with pytest.raises(WorkflowError, match="扩展表格"):
        executor._build_result(
            "tsk_expanded_table",
            [],
            [template, target],
            result,
            {},
        )


def test_positional_added_deleted_placeholder_pair_is_coalesced() -> None:
    location = DocumentLocation(paragraph_index=7)
    added = DiffItem(
        diff_id="diff_added",
        diff_type="ADDED",
        title="added",
        baseline=None,
        target=DiffSide(
            file_id="fil_target", location=location, text="合同编号：ABC-001"
        ),
        confidence=1,
    )
    deleted = DiffItem(
        diff_id="diff_deleted",
        diff_type="DELETED",
        title="deleted",
        baseline=DiffSide(
            file_id="fil_template", location=location, text="合同编号：##{合同编号}"
        ),
        target=None,
        confidence=1,
    )

    differences, count = _coalesce_positional_fills([added, deleted])

    assert count == 1
    assert len(differences) == 1
    assert differences[0].diff_type == "MODIFIED"
    assert differences[0].baseline and differences[0].target
    assert differences[0].requires_manual_review is False
