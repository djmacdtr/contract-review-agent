from app.comparison.engine import CompareOptions, compare_documents
from app.documents.models import DocumentBlock, DocumentLocation, ParsedDocument
from app.results.passed_checks import build_comparison_passed_checks


def document(file_id: str, text: str) -> ParsedDocument:
    return ParsedDocument(
        file_id=file_id,
        role="BASELINE" if file_id == "base" else "TARGET",
        file_name=f"{file_id}.docx",
        sha256="a" * 64,
        page_count=1,
        blocks=[
            DocumentBlock(
                block_id=f"{file_id}_p0",
                type="PARAGRAPH",
                order=0,
                raw_text=text,
                normalized_text=text,
                location=DocumentLocation(page=1, paragraph_index=0),
            )
        ],
        parser_name="fixture",
    )


def passed(before: str, after: str, *, numeric_sensitive: bool = True) -> list[dict]:
    documents = [document("base", before), document("target", after)]
    comparison = compare_documents(documents[0], documents[1], CompareOptions())
    return build_comparison_passed_checks(
        documents,
        comparison.diff_items,
        comparison.diagnostics,
        check_prefix="check_fixture",
        module_code="VERSION_CHANGE",
        content_title="全文未发生变化",
        numeric_sensitive=numeric_sensitive,
    )


def test_actual_date_content_creates_pass_but_does_not_invent_duration_check() -> None:
    checks = passed("签署日期为2026年8月20日。", "签署日期为2026年8月20日。")
    titles = {item["title"] for item in checks}

    assert "日期未发生变化" in titles
    assert "期限未发生变化" not in titles


def test_disabled_numeric_content_checks_do_not_create_date_pass() -> None:
    checks = passed(
        "签署日期为2026年8月20日。",
        "签署日期为2026年8月20日。",
        numeric_sensitive=False,
    )

    assert "日期未发生变化" not in {item["title"] for item in checks}


def test_changed_date_is_not_marked_as_passed() -> None:
    checks = passed("签署日期为2026年8月20日。", "签署日期为2026年9月20日。")

    assert "日期未发生变化" not in {item["title"] for item in checks}


def test_chinese_numeral_date_is_not_marked_as_passed_when_changed() -> None:
    checks = passed("签署日期为二〇二六年八月二十日。", "签署日期为二〇二六年九月二十日。")

    assert "日期未发生变化" not in {item["title"] for item in checks}


def test_pending_v2_difference_blocks_passed_checks() -> None:
    documents = [
        document("base", "租赁期限为二十四个月。"),
        document("target", "租赁期限为三十六个月。"),
    ]
    comparison = compare_documents(documents[0], documents[1], CompareOptions())

    checks = build_comparison_passed_checks(
        documents,
        [],
        comparison.diagnostics,
        check_prefix="check_fixture",
        module_code="VERSION_CHANGE",
        content_title="全文未发生变化",
        numeric_sensitive=True,
        pending_differences=comparison.diff_items,
    )

    assert "期限未发生变化" not in {item["title"] for item in checks}
