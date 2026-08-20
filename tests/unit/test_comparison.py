import pytest

from app.comparison.engine import CompareOptions, compare_documents
from app.comparison.reliable import comparison_normalize
from app.documents.models import (
    DocumentBlock,
    DocumentLocation,
    ParsedDocument,
    ParsedTable,
    ProcessingWarning,
    TableCell,
    TableRow,
)


def paragraph_document(
    file_id: str,
    texts: list[str],
    *,
    source: str | None = None,
    confidence: float | None = None,
) -> ParsedDocument:
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
                location=DocumentLocation(
                    paragraph_index=index,
                    source=source,
                    confidence=confidence,
                ),
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
    baseline = paragraph_document(
        "base", ["第一条 合同金额为100万元。", "第二条 保留条款", "将删除"]
    )
    target = paragraph_document(
        "target", ["第一条 合同金额为120万元。", "第二条 保留条款", "新增内容"]
    )
    compared = compare_documents(baseline, target, CompareOptions())
    types = [item.diff_type for item in compared.diff_items]
    assert "NUMERIC_CHANGED" in types
    assert set(types) & {"MODIFIED", "ADDED", "DELETED"}
    numeric = next(item for item in compared.diff_items if item.diff_type == "NUMERIC_CHANGED")
    assert numeric.severity == "HIGH"
    assert numeric.baseline.location.paragraph_index == 0
    assert any(segment.operation == "DELETE" for segment in numeric.segments)


def test_table_cell_change_has_traceable_row_and_column() -> None:
    compared = compare_documents(
        table_document("base", "100万元"), table_document("target", "120万元"), CompareOptions()
    )
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


def test_low_confidence_ocr_text_is_low_but_numeric_change_remains_high() -> None:
    baseline = paragraph_document(
        "base", ["第一条 普通说明。", "第二条 合同金额为100万元。"], source="OCR", confidence=0.55
    )
    target = paragraph_document(
        "target", ["第一条 普通描述。", "第二条 合同金额为120万元。"], source="OCR", confidence=0.55
    )
    compared = compare_documents(
        baseline,
        target,
        CompareOptions(ocr_low_confidence_threshold=0.8),
    )
    ordinary = next(item for item in compared.diff_items if item.diff_type == "MODIFIED")
    numeric = next(item for item in compared.diff_items if item.diff_type == "NUMERIC_CHANGED")
    assert ordinary.severity == "LOW"
    assert ordinary.confidence == 0.55
    assert numeric.severity == "HIGH"
    assert numeric.confidence == 0.55


def test_one_to_many_and_many_to_one_segmentation_are_equivalent() -> None:
    whole = "第一条合同金额为100万元，租赁期限为24个月。"
    split = ["第一条合同金额为100万元，", "租赁期限为24个月。"]
    one_to_many = compare_documents(
        paragraph_document("base", [whole]), paragraph_document("target", split), CompareOptions()
    )
    many_to_one = compare_documents(
        paragraph_document("base", split), paragraph_document("target", [whole]), CompareOptions()
    )
    assert one_to_many.diff_items == []
    assert many_to_one.diff_items == []
    assert one_to_many.diagnostics.reliable is True
    assert many_to_one.diagnostics.reliable is True


def test_many_to_many_segmentation_preserves_traceable_locations() -> None:
    baseline = paragraph_document("base", ["第一条付款金额为100万元，", "期限24个月。"])
    target = paragraph_document("target", ["第一条付款金额为120万元，期限24个月。"])
    compared = compare_documents(baseline, target, CompareOptions())
    numeric = next(item for item in compared.diff_items if item.diff_type == "NUMERIC_CHANGED")
    assert len(numeric.baseline.locations) == 2
    assert len(numeric.target.locations) == 1
    assert numeric.baseline.location == numeric.baseline.locations[0]
    assert compared.diagnostics.alignment_coverage_baseline == 1.0


def test_comparison_normalization_suppresses_spacing_linebreak_and_punctuation_noise() -> None:
    baseline = paragraph_document("base", ["第 一 条\u200b 合同金额：100万元。"])
    target = paragraph_document("target", ["第一条\n合同金额:100万元"])
    compared = compare_documents(baseline, target, CompareOptions())
    assert compared.diff_items == []


@pytest.mark.parametrize(
    ("baseline", "target"),
    [
        ("融资租赁合同", "融资<br>租赁**合同**"),
        ("设备规格A~B", "$\\mathrm {设备规格A}\\mathrm {\\sim B}$"),
        ("设备A 设备B", "| 设备A | 设备B |"),
    ],
)
def test_comparison_normalization_suppresses_external_parser_markup(
    baseline: str, target: str
) -> None:
    assert comparison_normalize(baseline)[1] == comparison_normalize(target)[1]


def test_tiny_high_confidence_ocr_variance_is_retained_as_review_only() -> None:
    prefix = "第一条" + "应当依约履行义务" * 8
    compared = compare_documents(
        paragraph_document("base", [prefix + "双方同意继续履行。"], source="OCR", confidence=0.99),
        paragraph_document("target", [prefix + "双方同继续履行。"], source="OCR", confidence=0.99),
        CompareOptions(),
    )
    assert len(compared.diff_items) == 1
    assert compared.diff_items[0].diff_type == "MODIFIED"
    assert compared.diff_items[0].severity == "LOW"


def test_external_parser_reading_order_variance_is_review_only() -> None:
    common = "合同附件列明设备名称规格数量及存放地点并由双方确认"
    compared = compare_documents(
        paragraph_document(
            "base", [common + "钢制储罐其他约定"], source="OCR", confidence=0.99
        ),
        paragraph_document(
            "target", [common + "其他约定钢制储罐"], source="OCR", confidence=0.99
        ),
        CompareOptions(),
    )
    assert len(compared.diff_items) == 1
    assert compared.diff_items[0].severity == "LOW"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("付款日期为2026年8月20日。", "付款日期为2026年9月20日。"),
        ("年利率为3.5%。", "年利率为4.1%。"),
        ("租赁期限为24个月。", "租赁期限为36个月。"),
    ],
)
def test_critical_numeric_changes_are_never_absorbed_by_fuzzy_alignment(
    before: str, after: str
) -> None:
    compared = compare_documents(
        paragraph_document("base", [before]),
        paragraph_document("target", [after]),
        CompareOptions(),
    )
    assert [item.diff_type for item in compared.diff_items] == ["NUMERIC_CHANGED"]
    assert compared.diff_items[0].severity == "HIGH"


def test_subject_and_clause_changes_remain_recallable() -> None:
    compared = compare_documents(
        paragraph_document("base", ["第一条甲方为北京示例公司。", "第二条保留条款。"]),
        paragraph_document("target", ["第一条甲方为上海示例公司。", "新增完整条款。"]),
        CompareOptions(),
    )
    assert any(item.severity == "HIGH" for item in compared.diff_items)


def test_unrelated_documents_are_safely_suppressed_for_manual_review() -> None:
    compared = compare_documents(
        paragraph_document("base", ["第一条融资租赁合同金额为100万元。"]),
        paragraph_document("target", ["天气预报显示明日有雨，出行请携带雨具。"]),
        CompareOptions(),
    )
    assert compared.diff_items == []
    assert compared.diagnostics.reliable is False
    assert "DOCUMENT_PAIR_UNRELATED" in compared.diagnostics.reasons
    assert any(warning.code == "DOCUMENT_PAIR_UNRELATED" for warning in compared.warnings)


def test_structure_explosion_is_suppressed_and_reported() -> None:
    baseline = paragraph_document("base", [f"物理短行{i}" for i in range(20)])
    target = paragraph_document("target", ["完全不同的长段落A", "完全不同的长段落B"])
    compared = compare_documents(baseline, target, CompareOptions())
    assert compared.diff_items == []
    assert compared.diagnostics.candidate_diff_count > 0
    assert compared.diagnostics.emitted_diff_count == 0
    assert compared.diagnostics.reliable is False
    assert "ALIGNMENT_UNRELIABLE" in compared.diagnostics.reasons


def test_duplicate_warnings_are_aggregated_with_count() -> None:
    baseline = paragraph_document("base", ["相同内容"])
    target = paragraph_document("target", ["相同内容"])
    baseline.warnings = [
        ProcessingWarning(code="OCR_NOTE", message="相同警告", requires_manual_review=False),
        ProcessingWarning(code="OCR_NOTE", message="相同警告", requires_manual_review=False),
    ]
    compared = compare_documents(baseline, target, CompareOptions())
    notes = [warning for warning in compared.warnings if warning.code == "OCR_NOTE"]
    assert len(notes) == 1
    assert notes[0].details["count"] == 2


def test_page_flat_fallback_recovers_equivalent_split_pdf_text() -> None:
    parts = ["第一条", "合同", "金额", "为", "100", "万元", "期限", "24个月"]
    baseline = paragraph_document("base", parts)
    target = paragraph_document("target", ["".join(parts)])
    for document in (baseline, target):
        document.page_count = 1
        for block in document.blocks:
            block.location.page = 1
    compared = compare_documents(baseline, target, CompareOptions())
    assert compared.diff_items == []
    assert compared.diagnostics.fallback_mode == "PAGE_FLAT"
    assert compared.diagnostics.alignment_coverage_baseline == 1.0


def test_incompatible_tables_fall_back_without_row_explosion() -> None:
    baseline = table_document("base", "100万元")
    target = table_document("target", "100万元")
    assert target.blocks[0].table is not None
    for row in target.blocks[0].table.rows:
        row.cells.append(
            TableCell(
                raw_text="附加列",
                normalized_text="附加列",
                location=DocumentLocation(table_index=0, row=row.row, column=2),
            )
        )
    compared = compare_documents(baseline, target, CompareOptions())
    assert any(warning.code == "TABLE_STRUCTURE_INCOMPATIBLE" for warning in compared.warnings)
    assert not any(item.diff_type.startswith("TABLE_") for item in compared.diff_items)
    assert compared.diagnostics.compatible_table_count == 0
