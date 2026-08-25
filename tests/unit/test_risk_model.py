from app.comparison.models import DiffItem, DiffSegment, DiffSide, MissingDetail
from app.documents.models import DocumentLocation, ProcessingWarning
from app.results.risk_model import build_review_items, build_risk_items, build_statistics


def difference(*, review_reason: str | None = None) -> DiffItem:
    return DiffItem(
        diff_id="diff_000001",
        diff_type="MODIFIED",
        title="固定条款变化",
        baseline=DiffSide(
            file_id="fil_base",
            location=DocumentLocation(paragraph_index=1),
            text="原条款",
        ),
        target=DiffSide(
            file_id="fil_target",
            location=DocumentLocation(paragraph_index=1),
            text="新条款",
        ),
        confidence=0.9,
        review_reason=review_reason,
    )


def test_confirmed_difference_becomes_ungraded_risk() -> None:
    diff = difference()
    risks = build_risk_items([diff], module_code="VERSION_CHANGE")
    reviews = build_review_items([diff], [], module_code="DOCUMENT_RELIABILITY")
    statistics = build_statistics(risks, reviews, [])

    assert risks[0]["change_type"] == "MODIFIED"
    assert risks[0]["risk_type"] == "ADDITION_OR_CHANGE"
    assert reviews == []
    assert statistics["risk_count"] == 1
    assert "severity" not in risks[0]


def test_ocr_difference_becomes_risk_while_legacy_review_builder_remains_compatible() -> None:
    diff = difference(review_reason="OCR_LOW_CONFIDENCE_VARIANCE")
    warning = ProcessingWarning(code="ALIGNMENT_UNRELIABLE", message="对齐不可靠")

    risks = build_risk_items([diff], module_code="VERSION_CHANGE")
    reviews = build_review_items(
        [diff], [warning], module_code="DOCUMENT_RELIABILITY"
    )

    assert len(risks) == 1
    assert risks[0]["risk_type"] == "ADDITION_OR_CHANGE"
    assert {item["reason_code"] for item in reviews} == {
        "OCR_LOW_CONFIDENCE_VARIANCE",
        "ALIGNMENT_UNRELIABLE",
    }
    assert all(item["requires_manual_action"] for item in reviews)


def test_failed_required_rule_becomes_deletion_or_missing_risk() -> None:
    risks = build_risk_items(
        [],
        module_code="TEMPLATE_INTEGRITY",
        failed_rules=[
            {
                "rule_id": "draft.required_table_cell_empty.0001",
                "rule_name": "必填表格单元格为空",
                "status": "FAILED",
                "message": "需要填写",
                "location": {"file_id": "fil_target", "table_index": 0},
            }
        ],
    )

    assert risks[0]["risk_type"] == "DELETION_OR_MISSING"
    assert risks[0]["change_type"] == "RULE_FAILED"


def test_page_and_content_block_missing_are_deletion_risks() -> None:
    differences = []
    for index, diff_type in enumerate(
        ("PAGE_MISSING", "CONTENT_BLOCK_MISSING"), start=1
    ):
        differences.append(
            DiffItem(
                diff_id=f"diff_{index:06d}",
                diff_type=diff_type,
                title="页面内容缺失" if diff_type == "PAGE_MISSING" else "连续内容块缺失",
                baseline=DiffSide(
                    file_id="fil_base",
                    location=DocumentLocation(paragraph_index=4),
                    text="连续缺失内容",
                ),
                target=DiffSide(
                    file_id="fil_target",
                    location=DocumentLocation(page=2),
                    text="",
                ),
                segments=[DiffSegment(operation="DELETE", text="连续缺失内容")],
                confidence=0.99,
                certainty="CONFIRMED",
                missing_detail=MissingDetail(
                    boundary="MIDDLE",
                    structure_unit_count=2,
                    aggregated_diff_count=2,
                    content_summary="连续缺失内容",
                ),
            )
        )

    risks = build_risk_items(differences, module_code="VERSION_CHANGE")

    assert [item["risk_type"] for item in risks] == [
        "DELETION_OR_MISSING",
        "DELETION_OR_MISSING",
    ]
    assert all("连续内容" in item["description"] for item in risks)
