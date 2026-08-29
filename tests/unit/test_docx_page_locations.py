import pytest

from app.comparison.engine import CompareOptions, compare_documents
from app.core.errors import WorkflowError
from app.documents.models import (
    DocumentBlock,
    DocumentLocation,
    ParsedDocument,
    ParsedTable,
    TableCell,
    TableRow,
)
from app.documents.page_locations import (
    DocxPageLocationSidecar,
    apply_docx_page_location_sidecars,
    augment_unmapped_table_page_bindings,
    bind_docx_page_locations,
    build_docx_page_location_sidecar,
    validate_public_page_coverage,
)


def paragraph_block(
    file_id: str,
    index: int,
    text: str,
    *,
    page: int | None = None,
) -> DocumentBlock:
    return DocumentBlock(
        block_id=f"{file_id}_p{index}",
        type="PARAGRAPH",
        order=index,
        raw_text=text,
        normalized_text=text,
        location=DocumentLocation(
            paragraph_index=index,
            page=page,
            structure_id=f"paragraph:{index}",
        ),
    )


def table_block(
    file_id: str,
    table_index: int,
    rows: list[list[str]],
    *,
    page: int | None = None,
) -> DocumentBlock:
    parsed_rows = [
        TableRow(
            row=row_index,
            cells=[
                TableCell(
                    raw_text=text,
                    normalized_text=text,
                    location=DocumentLocation(
                        table_index=table_index,
                        row=row_index,
                        column=column,
                        page=page,
                        structure_id=(
                            f"table_cell:{table_index}:{row_index}:{column}"
                        ),
                    ),
                )
                for column, text in enumerate(row)
            ],
        )
        for row_index, row in enumerate(rows)
    ]
    raw = "\n".join("\t".join(row) for row in rows)
    return DocumentBlock(
        block_id=f"{file_id}_t{table_index}",
        type="TABLE",
        order=table_index,
        raw_text=raw,
        normalized_text=raw,
        location=DocumentLocation(
            table_index=table_index,
            page=page,
            structure_id=f"table:{table_index}",
        ),
        table=ParsedTable(table_index=table_index, rows=parsed_rows),
    )


def document(file_id: str, blocks: list[DocumentBlock], page_count: int | None) -> ParsedDocument:
    return ParsedDocument(
        file_id=file_id,
        role="TARGET",
        file_name=f"{file_id}.docx",
        sha256="a" * 64,
        page_count=page_count,
        blocks=blocks,
        parser_name="python-docx",
    )


def test_maps_ordered_paragraphs_to_physical_pages() -> None:
    local = document(
        "fil_local",
        [paragraph_block("fil_local", 0, "第一段"), paragraph_block("fil_local", 1, "第二段")],
        None,
    )
    external = document(
        "fil_local",
        [
            paragraph_block("fil_local", 0, "第一段", page=1),
            paragraph_block("fil_local", 1, "第二段", page=2),
        ],
        2,
    )

    sidecar = build_docx_page_location_sidecar(local, external)

    assert sidecar.coverage == 1
    assert sidecar.pages_for(local.blocks[0].location) == (1,)
    assert sidecar.pages_for(local.blocks[1].location) == (2,)


def test_repeated_text_uses_sequence_position() -> None:
    local = document(
        "fil_local",
        [paragraph_block("fil_local", 0, "重复文本"), paragraph_block("fil_local", 1, "重复文本")],
        None,
    )
    external = document(
        "fil_local",
        [
            paragraph_block("fil_local", 0, "重复文本", page=1),
            paragraph_block("fil_local", 1, "重复文本", page=2),
        ],
        2,
    )

    sidecar = build_docx_page_location_sidecar(local, external)

    assert sidecar.pages_for(local.blocks[0].location) == (1,)
    assert sidecar.pages_for(local.blocks[1].location) == (2,)


def test_ambiguous_duplicate_text_is_skipped_until_public_gate() -> None:
    local = document("fil_local", [paragraph_block("fil_local", 0, "重复文本")], None)
    external = document(
        "fil_local",
        [
            paragraph_block("fil_local", 0, "重复文本", page=1),
            paragraph_block("fil_local", 1, "重复文本", page=2),
        ],
        2,
    )

    sidecar = build_docx_page_location_sidecar(local, external)

    assert sidecar.mapped_location_count == 0
    assert sidecar.unmapped_location_count == 1


def test_cross_page_paragraph_maps_to_a_multi_page_range() -> None:
    local = document(
        "fil_local",
        [paragraph_block("fil_local", 0, "这一段内容跨越两个页面，需要按顺序保留。")],
        None,
    )
    external = document(
        "fil_local",
        [
            paragraph_block("fil_local", 0, "这一段内容跨越两个页面，", page=1),
            paragraph_block("fil_local", 1, "需要按顺序保留。", page=2),
        ],
        2,
    )

    sidecar = build_docx_page_location_sidecar(local, external)

    assert sidecar.pages_for(local.blocks[0].location) == (1, 2)


def test_table_block_and_cells_receive_external_pages() -> None:
    local_table = table_block("fil_local", 0, [["名称", "金额"], ["设备A", "100万元"]])
    external_table = table_block(
        "fil_local", 0, [["名称", "金额"], ["设备A", "100万元"]], page=2
    )
    local = document("fil_local", [local_table], None)
    external = document("fil_local", [external_table], 2)
    # A short non-table paragraph represents page 1; page 2 carries the table.
    external.blocks.insert(0, paragraph_block("fil_local", 1, "首页说明", page=1))

    sidecar = build_docx_page_location_sidecar(local, external)

    assert sidecar.pages_for(local_table.location) == (2,)
    assert sidecar.pages_for(local_table.table.rows[1].cells[1].location) == (2,)


def test_docx_table_maps_to_flattened_external_paragraphs_in_order() -> None:
    local_table = table_block(
        "fil_local", 0, [["名称", "金额"], ["设备A", "100万元"]]
    )
    external = document(
        "fil_local",
        [
            paragraph_block("fil_local", 0, "名称 金额", page=1),
            paragraph_block("fil_local", 1, "设备A 100万元", page=2),
        ],
        2,
    )
    external.parser_metadata["page_ids"] = [1, 2]

    sidecar = build_docx_page_location_sidecar(
        document("fil_local", [local_table], None), external
    )

    assert sidecar.pages_for(local_table.location) == (1, 2)
    assert sidecar.pages_for(local_table.table.rows[0].cells[0].location) == (1,)
    assert sidecar.pages_for(local_table.table.rows[1].cells[1].location) == (2,)


def test_unmapped_table_inherits_pages_from_ordered_flattened_anchors() -> None:
    local_table = table_block("fil_local", 0, [["名称", "金额"]])
    local = document("fil_local", [local_table], None)
    external = document(
        "fil_local",
        [paragraph_block("fil_local", 0, "名称 金额", page=3)],
        3,
    )
    sidecar = DocxPageLocationSidecar(
        file_id="fil_local",
        page_count=3,
        mappings={},
        required_location_count=3,
        candidate_mapping_count=0,
        local_structure_count=3,
        external_structure_count=1,
        external_detail_page_count=3,
    )

    rebound = augment_unmapped_table_page_bindings(local, external, sidecar)

    assert rebound.pages_for(local_table.location) == (3,)
    assert rebound.pages_for(local_table.table.rows[0].cells[0].location) == (3,)
    assert rebound.pages_for(local_table.table.rows[0].cells[1].location) == (3,)


def test_blank_physical_page_without_details_is_allowed() -> None:
    local = document(
        "fil_local", [paragraph_block("fil_local", 0, "唯一正文")], None
    )
    external = document(
        "fil_local", [paragraph_block("fil_local", 0, "唯一正文", page=1)], 2
    )
    external.parser_metadata["page_ids"] = [1, 2]

    sidecar = build_docx_page_location_sidecar(local, external)

    assert sidecar.pages_for(local.blocks[0].location) == (1,)
    assert sidecar.summary()["external_detail_page_count"] == 1


def test_unrelated_unmapped_structure_does_not_fail_sidecar() -> None:
    local = document(
        "fil_local",
        [
            paragraph_block("fil_local", 0, "需要展示的正文"),
            paragraph_block("fil_local", 1, "外部解析未返回的装饰文本"),
        ],
        None,
    )
    external = document(
        "fil_local", [paragraph_block("fil_local", 0, "需要展示的正文", page=1)], 1
    )

    sidecar = build_docx_page_location_sidecar(local, external)

    assert sidecar.pages_for(local.blocks[0].location) == (1,)
    assert sidecar.pages_for(local.blocks[1].location) is None
    assert sidecar.unmapped_location_count == 1


def test_unmatched_table_cell_inherits_reliable_table_page() -> None:
    rows = [["名称", "金额"], ["设备A", "100万元"], ["地址", "上海市浦东新区"]]
    local_table = table_block("fil_local", 0, rows)
    external_table = table_block(
        "fil_local",
        0,
        [["名称", "金额"], ["设备A", "未识别金额"], ["地址", "上海市浦东新区"]],
        page=2,
    )
    local = document("fil_local", [local_table], None)
    external = document("fil_local", [external_table], 2)
    external.parser_metadata["page_ids"] = [1, 2]
    external.blocks.insert(0, paragraph_block("fil_local", 1, "页一说明", page=1))

    sidecar = build_docx_page_location_sidecar(local, external)

    assert sidecar.pages_for(local_table.table.rows[1].cells[1].location) == (2,)


def test_large_table_cell_mapping_does_not_use_recursion_depth() -> None:
    rows = [[f"字段{i}"] for i in range(1001)]
    local_table = table_block("fil_local", 0, rows)
    external_table = table_block("fil_local", 0, rows, page=1)

    sidecar = build_docx_page_location_sidecar(
        document("fil_local", [local_table], None),
        document("fil_local", [external_table], 1),
    )

    assert sidecar.pages_for(local_table.table.rows[1000].cells[0].location) == (1,)


def test_unmapped_public_evidence_fails_with_safe_stage() -> None:
    local = document(
        "fil_local",
        [
            paragraph_block("fil_local", 0, "可映射正文"),
            paragraph_block("fil_local", 1, "未返回正文"),
        ],
        None,
    )
    external = document(
        "fil_local", [paragraph_block("fil_local", 0, "可映射正文", page=1)], 1
    )
    sidecar = build_docx_page_location_sidecar(local, external)
    result = {
        "diff_items": [
            {
                "diff_id": "diff_public_unmapped",
                "baseline": {
                    "file_id": "fil_local",
                    "location": {"paragraph_index": 1},
                },
            }
        ]
    }

    with pytest.raises(WorkflowError) as caught:
        apply_docx_page_location_sidecars(result, {"fil_local": sidecar})

    assert caught.value.code == "DOCX_PAGE_LOCATION_INCOMPLETE"
    assert caught.value.details == {
        "failure_stage": "PUBLIC_EVIDENCE_MAPPING",
        "failure_code": "PUBLIC_LOCATION_UNMAPPED",
        "page_count": 1,
        "external_detail_page_count": 1,
        "local_structure_count": 2,
        "external_structure_count": 1,
        "candidate_mapping_count": 1,
        "mapped_location_count": 1,
        "unmapped_location_count": 1,
        "unmapped_structures": [
            {
                "structure_id": "paragraph:1",
                "kind": "PARAGRAPH",
                "candidate_count": 0,
                "candidate_pages": [],
                "diagnosis": "UNCLASSIFIED",
                "paragraph_index": 1,
            }
        ],
        "public_evidence_file_id": "fil_local",
        "public_evidence_location": {"paragraph_index": 1},
    }


def test_non_public_risk_evidence_does_not_widen_public_gate() -> None:
    local = document(
        "fil_local",
        [
            paragraph_block("fil_local", 0, "可映射正文"),
            paragraph_block("fil_local", 1, "规则诊断未返回正文"),
        ],
        None,
    )
    external = document(
        "fil_local", [paragraph_block("fil_local", 0, "可映射正文", page=1)], 1
    )
    sidecar = build_docx_page_location_sidecar(local, external)
    result = {
        "risk_items": [
            {
                "risk_id": "risk_rule_only",
                "related_diff_ids": [],
                "source_evidence": [
                    {
                        "file_id": "fil_local",
                        "location": {"paragraph_index": 1},
                    }
                ],
            }
        ]
    }

    apply_docx_page_location_sidecars(result, {"fil_local": sidecar})

    assert result["risk_items"][0]["source_evidence"][0]["location"] == {
        "paragraph_index": 1
    }


def test_incomplete_external_page_ids_fail_closed() -> None:
    local = document("fil_local", [paragraph_block("fil_local", 0, "内容")], None)
    external = document(
        "fil_local", [paragraph_block("fil_local", 0, "内容", page=1)], 2
    )

    with pytest.raises(WorkflowError) as caught:
        build_docx_page_location_sidecar(local, external)

    assert caught.value.code == "DOCX_PAGE_LOCATION_INCOMPLETE"
    assert caught.value.details["failure_stage"] == "PAGE_ID_VALIDATION"
    assert caught.value.details["failure_code"] == "EXTERNAL_PAGE_ID_INCOMPLETE"


def test_result_enrichment_adds_pages_without_changing_diff_identity() -> None:
    local = document("fil_local", [paragraph_block("fil_local", 0, "旧内容")], None)
    external = document(
        "fil_local",
        [
            paragraph_block("fil_local", 0, "说明一", page=1),
            paragraph_block("fil_local", 1, "说明二", page=2),
            paragraph_block("fil_local", 2, "说明三", page=3),
            paragraph_block("fil_local", 3, "旧内容", page=4),
        ],
        4,
    )
    sidecar = build_docx_page_location_sidecar(local, external)
    result = {
        "files": [{"file_id": "fil_local", "parser_metadata": {}}],
        "diff_items": [
            {
                "diff_id": "diff_1",
                "baseline": {
                    "file_id": "fil_local",
                    "location": {"paragraph_index": 0},
                    "locations": [{"paragraph_index": 0}],
                },
            }
        ],
        "metadata": {
            "checkpoint_hit_key": "checkpoint_1",
            "payload_digest": "digest_1",
        },
    }

    apply_docx_page_location_sidecars(result, {"fil_local": sidecar})

    assert result["diff_items"][0]["diff_id"] == "diff_1"
    assert result["diff_items"][0]["baseline"]["location"] == {"paragraph_index": 0, "page": 4}
    assert result["diff_items"][0]["baseline"]["locations"][0]["page"] == 4
    assert result["files"][0]["page_count"] == 4
    assert result["metadata"] == {
        "checkpoint_hit_key": "checkpoint_1",
        "payload_digest": "digest_1",
    }


def test_diagnostics_contain_only_safe_structure_metadata() -> None:
    local = document(
        "fil_local",
        [
            paragraph_block("fil_local", 0, "未映射段落正文"),
            table_block("fil_local", 2, [["未映射单元格正文"]]),
        ],
        None,
    )
    external = document(
        "fil_local", [paragraph_block("fil_local", 0, "其他内容", page=1)], 1
    )
    sidecar = build_docx_page_location_sidecar(local, external)

    diagnostics = sidecar.unmapped_structures

    assert diagnostics
    serialized = repr(diagnostics)
    assert "未映射段落正文" not in serialized
    assert "未映射单元格正文" not in serialized
    assert all(
        set(item) <= {
            "structure_id",
            "kind",
            "candidate_count",
            "candidate_pages",
            "diagnosis",
            "paragraph_index",
            "table_index",
            "row",
            "column",
        }
        for item in diagnostics
    )


def test_unique_same_page_fallback_maps_repeated_structure_without_estimation() -> None:
    local = document(
        "fil_local", [paragraph_block("fil_local", 0, "唯一锚点内容")], None
    )
    external = document(
        "fil_local",
        [
            paragraph_block("fil_local", 0, "唯一锚点内容", page=2),
            paragraph_block("fil_local", 1, "唯一锚点内容", page=2),
        ],
        2,
    )
    external.parser_metadata["page_ids"] = [1, 2]

    sidecar = build_docx_page_location_sidecar(local, external)

    assert sidecar.pages_for(local.blocks[0].location) == (2,)
    assert sidecar.unmapped_structures == ()


def test_repeated_structure_with_different_pages_is_rejected() -> None:
    local = document(
        "fil_local", [paragraph_block("fil_local", 0, "重复锚点内容")], None
    )
    external = document(
        "fil_local",
        [
            paragraph_block("fil_local", 0, "重复锚点内容", page=1),
            paragraph_block("fil_local", 1, "重复锚点内容", page=2),
        ],
        2,
    )

    sidecar = build_docx_page_location_sidecar(local, external)

    assert sidecar.pages_for(local.blocks[0].location) is None
    assert sidecar.unmapped_structures[0]["diagnosis"] == "REPEATED_TEXT"


def test_page_boundary_anchor_maps_paragraph_to_next_real_page() -> None:
    local = document(
        "fil_local",
        [
            paragraph_block("fil_local", 0, "前一段"),
            paragraph_block("fil_local", 1, "边界段"),
            table_block("fil_local", 1, [["表头", "值"]]),
        ],
        None,
    )
    external = document(
        "fil_local",
        [
            paragraph_block("fil_local", 0, "前一段", page=1),
            table_block("fil_local", 1, [["表头", "值"]], page=2),
        ],
        2,
    )
    external.parser_metadata["page_ids"] = [1, 2]

    sidecar = build_docx_page_location_sidecar(local, external)

    assert sidecar.pages_for(local.blocks[1].location) == (2,)
    assert sidecar.unmapped_structures == ()


def test_page_boundary_without_unique_next_page_remains_unmapped() -> None:
    local = document(
        "fil_local",
        [
            paragraph_block("fil_local", 0, "前一段"),
            paragraph_block("fil_local", 1, "边界段"),
            table_block("fil_local", 1, [["表头", "值"]]),
        ],
        None,
    )
    external = document(
        "fil_local",
        [
            paragraph_block("fil_local", 0, "前一段", page=1),
            table_block("fil_local", 1, [["表头", "值"]], page=3),
        ],
        3,
    )
    external.parser_metadata["page_ids"] = [1, 2, 3]

    sidecar = build_docx_page_location_sidecar(local, external)

    assert sidecar.pages_for(local.blocks[1].location) is None
    assert any(
        item["structure_id"] == "paragraph:1"
        and item["diagnosis"] == "PAGE_BOUNDARY"
        for item in sidecar.unmapped_structures
    )


def test_bound_structure_pages_are_propagated_into_diff_evidence() -> None:
    baseline = document(
        "fil_local", [paragraph_block("fil_local", 0, "第1条 合同金额为原始金额")], None
    )
    target = document(
        "fil_local", [paragraph_block("fil_local", 0, "第1条 合同金额为变更金额", page=3)], 3
    )
    target.parser_metadata["page_ids"] = [1, 2, 3]
    sidecar = build_docx_page_location_sidecar(baseline, target)
    bind_docx_page_locations(baseline, sidecar)

    compared = compare_documents(baseline, target, CompareOptions())

    diff = next(item for item in compared.diff_items if item.baseline is not None)
    assert diff.baseline is not None
    assert diff.baseline.location.page == 3
    assert diff.baseline.location.structure_id == "paragraph:0"
    assert "structure_id" not in diff.model_dump(mode="json")["baseline"]["location"]


def test_public_page_coverage_requires_diff_sides_and_linked_risk_evidence() -> None:
    external = document(
        "fil_doc", [paragraph_block("fil_doc", 0, "证据", page=2)], 2
    )
    external.parser_metadata["page_ids"] = [1, 2]
    sidecar = build_docx_page_location_sidecar(
        document("fil_doc", [paragraph_block("fil_doc", 0, "证据")], None),
        external,
    )
    result = {
        "files": [{"file_id": "fil_doc", "page_count": 2}],
        "diff_items": [
            {
                "diff_id": "diff_1",
                "baseline": {
                    "file_id": "fil_doc",
                    "location": {"paragraph_index": 0, "page": 2},
                },
                "target": {
                    "file_id": "fil_doc",
                    "location": {"paragraph_index": 0, "page": 2},
                },
            }
        ],
        "risk_items": [
            {
                "risk_id": "risk_1",
                "related_diff_ids": ["diff_1"],
                "source_evidence": [
                    {
                        "file_id": "fil_doc",
                        "location": {"paragraph_index": 0, "page": 2},
                    }
                ],
            }
        ],
    }

    coverage = validate_public_page_coverage(result, {"fil_doc": sidecar})

    assert coverage == {
        "required_evidence_count": 3,
        "covered_evidence_count": 3,
        "missing_evidence_count": 0,
    }


def test_public_page_coverage_fails_without_a_public_page() -> None:
    external = document(
        "fil_doc", [paragraph_block("fil_doc", 0, "证据", page=1)], 1
    )
    external.parser_metadata["page_ids"] = [1]
    sidecar = build_docx_page_location_sidecar(
        document("fil_doc", [paragraph_block("fil_doc", 0, "证据")], None),
        external,
    )
    result = {
        "files": [{"file_id": "fil_doc", "page_count": 1}],
        "diff_items": [
            {
                "diff_id": "diff_1",
                "baseline": {
                    "file_id": "fil_doc",
                    "location": {"paragraph_index": 0},
                },
            }
        ],
        "risk_items": [],
    }

    with pytest.raises(WorkflowError) as caught:
        validate_public_page_coverage(result, {"fil_doc": sidecar})

    assert caught.value.code == "DOCX_PAGE_LOCATION_INCOMPLETE"
    assert caught.value.details["failure_code"] == "PUBLIC_DIFF_PAGE_MISSING"
    assert caught.value.details["public_evidence_file_id"] == "fil_doc"
    assert caught.value.details["public_evidence_location"] == {"paragraph_index": 0}
