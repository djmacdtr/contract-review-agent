from app.comparison.engine import CompareOptions, compare_documents
from app.documents.models import (
    DocumentBlock,
    DocumentLocation,
    ParsedDocument,
    ParsedTable,
    TableCell,
    TableRow,
)


def paragraph_document(file_id: str, texts: list[str]) -> ParsedDocument:
    return ParsedDocument(
        file_id=file_id,
        role="BASELINE" if file_id == "base" else "TARGET",
        file_name=f"{file_id}.docx",
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
        parser_name="test",
    )


def table_document(file_id: str, amount: str) -> ParsedDocument:
    rows = []
    for row_index, values in enumerate((("项目", "金额"), ("设备A", amount))):
        rows.append(
            TableRow(
                row=row_index,
                cells=[
                    TableCell(
                        raw_text=value,
                        normalized_text=value,
                        location=DocumentLocation(table_index=0, row=row_index, column=column),
                    )
                    for column, value in enumerate(values)
                ],
            )
        )
    return ParsedDocument(
        file_id=file_id,
        role="BASELINE" if file_id == "base" else "TARGET",
        file_name=f"{file_id}.docx",
        sha256="b" * 64,
        page_count=None,
        blocks=[
            DocumentBlock(
                block_id=f"{file_id}_t0",
                type="TABLE",
                order=0,
                raw_text="",
                normalized_text="",
                location=DocumentLocation(table_index=0),
                table=ParsedTable(table_index=0, rows=rows),
            )
        ],
        parser_name="test",
    )


def test_same_document_has_no_differences() -> None:
    baseline = paragraph_document("base", ["第一条 合同金额为100万元。"])
    target = paragraph_document("target", ["第一条 合同金额为100万元。"])
    compared = compare_documents(baseline, target, CompareOptions())
    assert compared.diff_items == []


def test_paragraph_numeric_added_and_deleted_are_classified() -> None:
    baseline = paragraph_document("base", ["第一条 合同金额为100万元。", "第二条 保留条款", "将删除"])
    target = paragraph_document("target", ["第一条 合同金额为120万元。", "第二条 保留条款", "新增内容"])
    compared = compare_documents(baseline, target, CompareOptions())
    types = [item.diff_type for item in compared.diff_items]
    assert "NUMERIC_CHANGED" in types
    assert set(types) & {"MODIFIED", "ADDED", "DELETED"}
    numeric = next(item for item in compared.diff_items if item.diff_type == "NUMERIC_CHANGED")
    assert numeric.severity == "HIGH"
    assert numeric.baseline.location.paragraph_index == 0
    assert any(segment.operation == "DELETE" for segment in numeric.segments)


def test_table_cell_change_has_traceable_row_and_column() -> None:
    compared = compare_documents(table_document("base", "100万元"), table_document("target", "120万元"), CompareOptions())
    item = next(item for item in compared.diff_items if item.diff_type == "TABLE_CELL_CHANGED")
    assert item.baseline.location.table_index == 0
    assert item.baseline.location.row == 1
    assert item.baseline.location.column == 1
    assert item.severity == "HIGH"


def test_low_similarity_same_clause_number_is_not_forced_into_modified() -> None:
    baseline = paragraph_document("base", ["1.保证人是依法成立并有效存续的合法单位。"])
    target = paragraph_document("target", ["1.今天天气晴朗，设备已经交付。"])
    compared = compare_documents(baseline, target, CompareOptions())
    assert "MODIFIED" not in [item.diff_type for item in compared.diff_items]
    assert {item.diff_type for item in compared.diff_items} == {"ADDED", "DELETED"}


def test_table_rows_match_by_unique_first_column_when_order_changes() -> None:
    baseline = table_document("base", "100万元")
    target = table_document("target", "100万元")
    base_table = baseline.blocks[0].table
    target_table = target.blocks[0].table
    assert base_table and target_table
    extra_base = base_table.rows[1].model_copy(deep=True)
    extra_base.row = 2
    extra_base.cells[0].raw_text = extra_base.cells[0].normalized_text = "设备B"
    extra_target = extra_base.model_copy(deep=True)
    target_table.rows = [target_table.rows[0], extra_target, target_table.rows[1]]
    base_table.rows.append(extra_base)
    compared = compare_documents(baseline, target, CompareOptions())
    assert compared.diff_items == []
