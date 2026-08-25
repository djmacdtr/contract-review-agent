import pytest

from app.comparison.engine import CompareOptions, compare_documents
from app.comparison.reliable import build_diff_segments, comparison_normalize
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


def paged_document(
    file_id: str,
    pages: list[list[str]],
    *,
    physical_pages: bool = True,
) -> ParsedDocument:
    blocks = []
    order = 0
    paragraph_index = 0
    for page, texts in enumerate(pages, start=1):
        for text in texts:
            blocks.append(
                DocumentBlock(
                    block_id=f"{file_id}_p{paragraph_index}",
                    type="PARAGRAPH",
                    order=order,
                    raw_text=text,
                    normalized_text=text,
                    location=DocumentLocation(
                        page=page,
                        paragraph_index=paragraph_index,
                        source="OCR",
                        confidence=0.99,
                    ),
                )
            )
            order += 1
            paragraph_index += 1
    return ParsedDocument(
        file_id=file_id,
        role="BASELINE" if file_id == "base" else "TARGET",
        file_name=f"{file_id}.pdf",
        sha256="c" * 64,
        page_count=len(pages),
        blocks=blocks,
        parser_name="fixture",
        parser_metadata={"physical_page_numbers": physical_pages},
    )


def table_document_from_rows(
    file_id: str,
    values_by_row: list[tuple[str, ...]],
    *,
    source: str | None = None,
    confidence: float | None = None,
) -> ParsedDocument:
    rows = []
    for row_index, values in enumerate(values_by_row):
        rows.append(
            TableRow(
                row=row_index,
                cells=[
                    TableCell(
                        raw_text=value,
                        normalized_text=value,
                        location=DocumentLocation(
                            page=1,
                            table_index=0,
                            row=row_index,
                            column=column,
                            source=source,
                            confidence=confidence,
                        ),
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
                location=DocumentLocation(page=1, table_index=0, source=source),
                table=ParsedTable(table_index=0, rows=rows),
            )
        ],
        parser_name="test",
    )


def table_document(file_id: str, amount: str) -> ParsedDocument:
    return table_document_from_rows(
        file_id,
        [("项目", "金额"), ("设备A", amount)],
    )


def asset_table_document(
    file_id: str,
    rows: list[tuple[str, ...]],
    *,
    source: str | None = "OCR",
) -> ParsedDocument:
    return table_document_from_rows(
        file_id,
        [
            ("序号", "设备名称", "设备编号", "数量", "金额", "交付日期"),
            *rows,
        ],
        source=source,
        confidence=0.99 if source == "OCR" else None,
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
    assert not hasattr(numeric, "severity")
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
    assert not hasattr(item, "severity")


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
    assert ordinary.review_reason == "OCR_LOW_CONFIDENCE_VARIANCE"
    assert ordinary.confidence == 0.55
    assert not hasattr(numeric, "severity")
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


def test_adjacent_text_only_table_continuation_row_is_merged() -> None:
    baseline = asset_table_document(
        "base",
        [("1", "大型钢制储罐设备", "EQ-001", "2", "100万元", "2026年8月20日")],
    )
    target = asset_table_document(
        "target",
        [
            ("1", "大型钢制储罐", "EQ-001", "2", "100万元", "2026年8月20日"),
            ("", "设备", "", "", "", ""),
        ],
    )

    compared = compare_documents(baseline, target, CompareOptions())

    assert compared.diff_items == []


def test_sparse_multirow_table_continuations_are_merged_before_compatibility_gate() -> None:
    baseline = asset_table_document(
        "base",
        [("1", "大型钢制储罐设备名称", "EQ-001", "2", "100万元", "2026年8月20日")],
    )
    target = asset_table_document(
        "target",
        [
            ("1", "大型", "EQ-001", "2", "100万元", "2026年8月20日"),
            ("", "钢制", "", "", "", ""),
            ("", "储罐", "", "", "", ""),
            ("", "设备", "", "", "", ""),
            ("", "名称", "", "", "", ""),
        ],
    )
    target_table = target.blocks[0].table
    assert target_table is not None
    for row in target_table.rows[2:]:
        row.cells = [row.cells[1]]

    compared = compare_documents(baseline, target, CompareOptions())

    assert compared.diff_items == []
    assert compared.diagnostics.compatible_table_count == 1


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


@pytest.mark.parametrize(
    "separator",
    ["<br>", "<br/>", "<br />", "\n", "\r\n", "\u200b"],
)
def test_display_only_break_and_zero_width_noise_does_not_create_diff(
    separator: str,
) -> None:
    compared = compare_documents(
        paragraph_document("base", ["付款日期为2026年8月20日。"]),
        paragraph_document("target", [f"付款日期为{separator}2026年8月20日。"]),
        CompareOptions(),
    )

    assert compared.diff_items == []


def test_noise_plus_real_change_only_emits_business_text_segments() -> None:
    compared = compare_documents(
        paragraph_document("base", ["付款<br>日期为2026年8月20日。"]),
        paragraph_document("target", ["付款\n日期为2026年9月20日。\u200b"]),
        CompareOptions(),
    )

    assert len(compared.diff_items) == 1
    item = compared.diff_items[0]
    assert item.diff_type == "NUMERIC_CHANGED"
    segment_text = "".join(segment.text for segment in item.segments)
    assert "<br" not in segment_text.lower()
    assert "\u200b" not in segment_text
    assert "\r" not in segment_text
    assert "".join(
        segment.text for segment in item.segments if segment.operation != "INSERT"
    ) == item.baseline.text
    assert "".join(
        segment.text for segment in item.segments if segment.operation != "DELETE"
    ) == item.target.text
    assert [
        segment.text for segment in item.segments if segment.operation == "DELETE"
    ] == ["8"]
    assert [
        segment.text for segment in item.segments if segment.operation == "INSERT"
    ] == ["9"]


@pytest.mark.parametrize(
    ("before", "after", "operation"),
    [("原完整条款", "", "DELETE"), ("", "新增完整条款", "INSERT")],
)
def test_pure_addition_or_deletion_uses_cleaned_full_text_segment(
    before: str, after: str, operation: str
) -> None:
    segments, baseline_text, target_text = build_diff_segments(before, after)

    assert [(segment.operation, segment.text) for segment in segments] == [
        (operation, before or after)
    ]
    assert baseline_text == before
    assert target_text == after


def test_legal_clause_single_character_ocr_variance_is_retained_as_review_only() -> None:
    prefix = "第十条争议解决条款约定双方应当依约履行义务" * 5
    compared = compare_documents(
        paragraph_document("base", [prefix + "双方同意继续履行。"], source="OCR", confidence=0.99),
        paragraph_document("target", [prefix + "双方同继续履行。"], source="OCR", confidence=0.99),
        CompareOptions(),
    )
    assert len(compared.diff_items) == 1
    assert compared.diff_items[0].diff_type == "MODIFIED"
    assert compared.diff_items[0].review_reason == "OCR_SINGLE_CHAR_VARIANCE"


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
    assert compared.diff_items[0].review_reason == "OCR_READING_ORDER_VARIANCE"


def test_amount_placeholder_single_character_variance_is_retained_for_review() -> None:
    common = "支付金额(大写):__________元;支付金额(小写):人民币__________元"
    compared = compare_documents(
        paragraph_document("base", [common + "D"], source="OCR", confidence=0.99),
        paragraph_document("target", [common], source="OCR", confidence=0.99),
        CompareOptions(),
    )

    assert len(compared.diff_items) == 1
    assert compared.diff_items[0].diff_type == "MODIFIED"
    assert compared.diff_items[0].review_reason == "OCR_PLACEHOLDER_VARIANCE"


def test_same_character_multiset_with_true_word_order_change_is_not_suppressed() -> None:
    compared = compare_documents(
        paragraph_document(
            "base",
            ["本合同约定先交付设备后支付全部价款"],
            source="OCR",
            confidence=0.99,
        ),
        paragraph_document(
            "target",
            ["本合同约定先支付全部价款后交付设备"],
            source="OCR",
            confidence=0.99,
        ),
        CompareOptions(),
    )

    assert compared.diff_items
    assert {item.diff_type for item in compared.diff_items} <= {"ADDED", "DELETED", "MODIFIED"}
    assert any(item.requires_manual_review for item in compared.diff_items)
    assert any(item.baseline is not None for item in compared.diff_items)
    assert any(item.target is not None for item in compared.diff_items)


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
    assert compared.diff_items[0].review_reason is None
    assert compared.diff_items[0].baseline.location.paragraph_index == 0
    assert compared.diff_items[0].target.location.paragraph_index == 0
    assert compared.diff_items[0].baseline.text == before
    assert compared.diff_items[0].target.text == after
    assert any(segment.operation == "DELETE" for segment in compared.diff_items[0].segments)
    assert any(segment.operation == "INSERT" for segment in compared.diff_items[0].segments)


def test_subject_and_clause_changes_remain_recallable() -> None:
    compared = compare_documents(
        paragraph_document("base", ["第一条甲方为北京示例公司。", "第二条保留条款。"]),
        paragraph_document("target", ["第一条甲方为上海示例公司。", "新增完整条款。"]),
        CompareOptions(),
    )
    assert compared.diff_items
    assert all(item.review_reason is None for item in compared.diff_items)


def test_subject_single_character_change_is_high_and_traceable() -> None:
    compared = compare_documents(
        paragraph_document("base", ["第一条甲方为北京示例科技有限公司。"]),
        paragraph_document("target", ["第一条甲方为北京示例科枝有限公司。"]),
        CompareOptions(),
    )

    assert len(compared.diff_items) == 1
    item = compared.diff_items[0]
    assert item.diff_type == "MODIFIED"
    assert item.review_reason is None
    assert item.baseline.location.paragraph_index == 0
    assert item.target.location.paragraph_index == 0
    assert any(segment.operation == "DELETE" and segment.text == "技" for segment in item.segments)
    assert any(segment.operation == "INSERT" and segment.text == "枝" for segment in item.segments)


def test_complete_clause_addition_and_deletion_preserve_type_and_locations() -> None:
    compared = compare_documents(
        paragraph_document("base", ["共同条款。", "原完整条款仅在基准文件中存在。"]),
        paragraph_document("target", ["共同条款。", "新完整约定仅在目标文件中出现。"]),
        CompareOptions(),
    )

    assert {item.diff_type for item in compared.diff_items} == {"ADDED", "DELETED"}
    deleted = next(item for item in compared.diff_items if item.diff_type == "DELETED")
    added = next(item for item in compared.diff_items if item.diff_type == "ADDED")
    assert deleted.baseline.location.paragraph_index == 1
    assert deleted.target is None
    assert added.target.location.paragraph_index == 1
    assert added.baseline is None


@pytest.mark.parametrize(
    ("column", "before", "after"),
    [
        (1, "大型钢制储罐", "大型不锈钢储罐"),
        (2, "EQ-001", "EQ-009"),
        (3, "2", "3"),
        (4, "100万元", "120万元"),
        (5, "2026年8月20日", "2026年9月20日"),
    ],
)
def test_real_table_cell_changes_are_not_absorbed(
    column: int,
    before: str,
    after: str,
) -> None:
    base_values = ["1", "大型钢制储罐", "EQ-001", "2", "100万元", "2026年8月20日"]
    target_values = base_values.copy()
    base_values[column] = before
    target_values[column] = after

    compared = compare_documents(
        asset_table_document("base", [tuple(base_values)]),
        asset_table_document("target", [tuple(target_values)]),
        CompareOptions(),
    )

    assert len(compared.diff_items) == 1
    item = compared.diff_items[0]
    assert item.diff_type == "TABLE_CELL_CHANGED"
    assert item.review_reason is None
    assert item.baseline.location.row == 1
    assert item.baseline.location.column == column
    assert item.target.location.row == 1
    assert item.target.location.column == column
    assert item.baseline.text == before
    assert item.target.text == after
    assert item.segments


@pytest.mark.parametrize(
    ("continuation", "expected_column"),
    [
        (("2", "设备B", "EQ-002", "1", "80万元", "2026年8月21日"), 0),
        (("", "设备B", "EQ-002", "", "", ""), 2),
        (("", "设备B", "", "1", "", ""), 3),
        (("", "设备B", "", "", "80万元", ""), 4),
        (("", "设备B", "", "", "", "2026年8月21日"), 5),
    ],
)
def test_table_rows_with_keys_or_critical_values_are_never_merged(
    continuation: tuple[str, ...], expected_column: int
) -> None:
    baseline = asset_table_document(
        "base",
        [("1", "设备A", "EQ-001", "2", "100万元", "2026年8月20日")],
    )
    target = asset_table_document(
        "target",
        [
            ("1", "设备A", "EQ-001", "2", "100万元", "2026年8月20日"),
            continuation,
        ],
    )

    compared = compare_documents(baseline, target, CompareOptions())

    assert compared.diff_items
    assert any(
        location.column == expected_column
        for item in compared.diff_items
        for side in (item.baseline, item.target)
        if side is not None
        for location in (side.locations or [side.location])
    )


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


def test_repeated_headers_and_footers_are_ignored_by_default() -> None:
    baseline = paragraph_document("base", ["第一条共同正文。"])
    target = paragraph_document("target", ["第一条共同正文。"])
    for document, marker in ((baseline, "基准页眉"), (target, "目标页眉")):
        document.blocks.extend(
            [
                DocumentBlock(
                    block_id=f"{document.file_id}_header_{page}",
                    type="HEADER" if page % 2 else "FOOTER",
                    order=page,
                    raw_text=f"{marker}-{page}",
                    normalized_text=f"{marker}-{page}",
                    location=DocumentLocation(page=page, source="OCR", confidence=0.99),
                )
                for page in range(1, 5)
            ]
        )

    compared = compare_documents(baseline, target, CompareOptions())

    assert compared.diff_items == []


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


def test_pdf_whole_page_deletion_is_one_confirmed_page_missing_diff() -> None:
    baseline = paged_document(
        "base",
        [
            ["第一条 双方确认本合同共同内容。"],
            ["第二条 本页包含连续缺失内容甲。", "第三条 本页包含连续缺失内容乙。"],
            ["第四条 双方继续履行共同内容。"],
        ],
    )
    target = paged_document(
        "target",
        [
            ["第一条 双方确认本合同共同内容。"],
            ["第四条 双方继续履行共同内容。"],
        ],
    )

    compared = compare_documents(baseline, target, CompareOptions())

    assert compared.diagnostics.reliable is True
    assert len(compared.diff_items) == 1
    item = compared.diff_items[0]
    assert item.diff_type == "PAGE_MISSING"
    assert item.certainty == "CONFIRMED"
    assert item.missing_detail is not None
    assert item.missing_detail.baseline_page_start == 2
    assert item.missing_detail.baseline_page_end == 2
    assert item.missing_detail.target_anchor_before_page == 1
    assert item.missing_detail.target_anchor_after_page == 2
    assert item.missing_detail.aggregated_diff_count == 2
    assert item.target is not None and item.target.text == ""
    assert [(segment.operation, segment.text) for segment in item.segments] == [
        ("DELETE", item.baseline.text)
    ]


def test_docx_to_pdf_page_equivalent_is_inferred_page_missing() -> None:
    common_before = "第一条 " + "共同起始内容" * 8
    common_after = "第四条 " + "共同后续内容" * 8
    baseline = paragraph_document(
        "base",
        [
            common_before,
            "第二条 " + "连续缺失正文甲" * 6,
            "第三条 " + "连续缺失正文乙" * 6,
            common_after,
        ],
    )
    target = paged_document("target", [[common_before], [common_after]])

    compared = compare_documents(baseline, target, CompareOptions())

    assert len(compared.diff_items) == 1
    item = compared.diff_items[0]
    assert item.diff_type == "PAGE_MISSING"
    assert item.certainty == "INFERRED"
    assert item.missing_detail is not None
    assert item.missing_detail.estimated_page_equivalent >= 0.8
    assert item.missing_detail.structure_unit_count == 2


def test_short_contiguous_deletion_is_content_block_missing() -> None:
    common_before = "第一条 " + "共同起始内容" * 20
    common_after = "第四条 " + "共同后续内容" * 20
    baseline = paragraph_document(
        "base",
        [common_before, "第二条 短内容甲。", "第三条 短内容乙。", common_after],
    )
    target = paged_document("target", [[common_before], [common_after]])

    compared = compare_documents(baseline, target, CompareOptions())

    assert len(compared.diff_items) == 1
    item = compared.diff_items[0]
    assert item.diff_type == "CONTENT_BLOCK_MISSING"
    assert item.certainty == "CONFIRMED"
    assert item.missing_detail is not None
    assert item.missing_detail.boundary == "MIDDLE"


def test_single_long_unit_can_be_inferred_as_a_missing_page() -> None:
    common_before = "第一条 " + "共同起始内容" * 6
    common_after = "第三条 " + "共同后续内容" * 6
    baseline = paragraph_document(
        "base",
        [common_before, "第二条 " + "单个超长正文块" * 18, common_after],
    )
    target = paged_document("target", [[common_before], [common_after]])

    compared = compare_documents(baseline, target, CompareOptions())

    assert [item.diff_type for item in compared.diff_items] == ["PAGE_MISSING"]
    assert compared.diff_items[0].certainty == "INFERRED"


def test_boundary_missing_uses_document_boundary_as_anchor() -> None:
    baseline = paged_document(
        "base",
        [
            ["第一条 文件开头整页缺失内容。"],
            ["第二条 其余合同内容保持一致。"],
            ["第三条 合同结尾内容保持一致。"],
        ],
    )
    target = paged_document(
        "target",
        [
            ["第二条 其余合同内容保持一致。"],
            ["第三条 合同结尾内容保持一致。"],
        ],
    )

    compared = compare_documents(baseline, target, CompareOptions())

    assert len(compared.diff_items) == 1
    item = compared.diff_items[0]
    assert item.diff_type == "PAGE_MISSING"
    assert item.certainty == "CONFIRMED"
    assert item.missing_detail is not None
    assert item.missing_detail.boundary == "START"
    assert item.missing_detail.target_anchor_before_page is None
    assert item.missing_detail.target_anchor_after_page == 1


def test_document_end_missing_uses_document_boundary_as_anchor() -> None:
    baseline = paged_document(
        "base",
        [
            ["第一条 合同开始内容保持一致。"],
            ["第二条 其余合同内容保持一致。"],
            ["第三条 文件末尾整页缺失内容。"],
        ],
    )
    target = paged_document(
        "target",
        [
            ["第一条 合同开始内容保持一致。"],
            ["第二条 其余合同内容保持一致。"],
        ],
    )

    compared = compare_documents(baseline, target, CompareOptions())

    assert len(compared.diff_items) == 1
    item = compared.diff_items[0]
    assert item.diff_type == "PAGE_MISSING"
    assert item.missing_detail is not None
    assert item.missing_detail.boundary == "END"
    assert item.missing_detail.target_anchor_before_page == 2
    assert item.missing_detail.target_anchor_after_page is None


def test_separate_missing_pages_remain_separate_aggregated_diffs() -> None:
    baseline = paged_document(
        "base",
        [[f"第{page}条 第{page}页独立合同内容。"] for page in range(1, 6)],
    )
    target = paged_document(
        "target",
        [
            ["第1条 第1页独立合同内容。"],
            ["第3条 第3页独立合同内容。"],
            ["第5条 第5页独立合同内容。"],
        ],
    )

    compared = compare_documents(baseline, target, CompareOptions())

    assert [item.diff_type for item in compared.diff_items] == [
        "PAGE_MISSING",
        "PAGE_MISSING",
    ]
    assert [item.missing_detail.baseline_page_start for item in compared.diff_items] == [
        2,
        4,
    ]


def test_page_sized_deletion_inside_one_target_page_is_content_block_missing() -> None:
    common_before = "第一条 " + "共同起始内容" * 8
    common_after = "第四条 " + "共同后续内容" * 8
    baseline = paragraph_document(
        "base",
        [
            common_before,
            "第二条 " + "连续缺失正文甲" * 8,
            "第三条 " + "连续缺失正文乙" * 8,
            common_after,
        ],
    )
    target = paged_document("target", [[common_before, common_after]])

    compared = compare_documents(baseline, target, CompareOptions())

    assert [item.diff_type for item in compared.diff_items] == [
        "CONTENT_BLOCK_MISSING"
    ]


def test_content_moved_to_another_position_is_not_labeled_missing() -> None:
    baseline = paragraph_document(
        "base",
        [
            "第一条 共同开始。",
            "第二条 移动内容甲。",
            "第三条 移动内容乙。",
            "第四条 共同中段。",
            "第五条 共同结尾。",
        ],
    )
    target = paragraph_document(
        "target",
        [
            "第一条 共同开始。",
            "第四条 共同中段。",
            "第五条 共同结尾。",
            "第二条 移动内容甲。",
            "第三条 移动内容乙。",
        ],
    )

    compared = compare_documents(baseline, target, CompareOptions())

    assert not any(
        item.diff_type in {"PAGE_MISSING", "CONTENT_BLOCK_MISSING"}
        for item in compared.diff_items
    )
    assert {item.diff_type for item in compared.diff_items} == {"ADDED", "DELETED"}


def test_large_confirmed_page_range_uses_effective_coverage() -> None:
    baseline_pages = [
        [f"第{page}条 " + ("合同页面内容" * 8)] for page in range(1, 11)
    ]
    target_pages = [baseline_pages[0], baseline_pages[-1]]

    compared = compare_documents(
        paged_document("base", baseline_pages),
        paged_document("target", target_pages),
        CompareOptions(),
    )

    assert compared.diagnostics.alignment_coverage_baseline < 0.3
    assert compared.diagnostics.effective_alignment_coverage_baseline == 1.0
    assert compared.diagnostics.reliable is True
    assert len(compared.diff_items) == 1
    item = compared.diff_items[0]
    assert item.diff_type == "PAGE_MISSING"
    assert item.missing_detail is not None
    assert (
        item.missing_detail.baseline_page_start,
        item.missing_detail.baseline_page_end,
    ) == (2, 9)
