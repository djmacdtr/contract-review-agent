from app.comparison.models import ComparisonDiagnostics, DiffItem, DiffSide
from app.documents.models import DocumentBlock, DocumentLocation, ParsedDocument
from app.draft_review.golden_annotations import (
    build_annotation_candidates,
    merge_existing_annotations,
    validate_annotations,
)
from app.draft_review.template_checks import TemplateReviewDiagnostics, TemplateReviewResult


def document(role: str, sha256: str, text: str, *, table_index: int | None = None):
    return ParsedDocument(
        file_id=f"fil_{role.lower()}",
        role=role,
        file_name=f"{role.lower()}.docx",
        sha256=sha256,
        page_count=None,
        parser_name="python-docx",
        blocks=[
            DocumentBlock(
                block_id="block_1",
                type="TABLE" if table_index is not None else "PARAGRAPH",
                order=0,
                raw_text=text,
                normalized_text=text,
                location=DocumentLocation(
                    paragraph_index=None if table_index is not None else 0,
                    table_index=table_index,
                ),
            )
        ],
    )


def review() -> TemplateReviewResult:
    diff = DiffItem(
        diff_id="unstable_1",
        diff_type="MODIFIED",
        title="changed",
        baseline=DiffSide(
            file_id="fil_template",
            location=DocumentLocation(paragraph_index=0),
            text="金额 ##{金额}",
        ),
        target=DiffSide(
            file_id="fil_target",
            location=DocumentLocation(paragraph_index=0),
            text="金额 100 万元",
        ),
        confidence=1,
    )
    comparison = ComparisonDiagnostics(
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
        candidate_diff_count=1,
        emitted_diff_count=1,
        compatible_table_count=0,
        fallback_mode="STRUCTURED",
    )
    return TemplateReviewResult(
        diff_items=[diff],
        rule_checks=[],
        warnings=[],
        diagnostics=TemplateReviewDiagnostics(
            comparison=comparison,
            raw_diff_count=1,
            retained_diff_count=1,
            filtered_diff_count=0,
            expanded_table_count=1,
            expanded_table_indexes=[2],
        ),
    )


def test_golden_fingerprints_ignore_runtime_diff_ids_and_do_not_store_text() -> None:
    template = document("TEMPLATE", "a" * 64, "模板表格", table_index=2)
    target = document("TARGET", "b" * 64, "目标表格", table_index=2)
    first = build_annotation_candidates(template, target, review())
    changed_review = review()
    changed_review.diff_items[0].diff_id = "unstable_999"
    second = build_annotation_candidates(template, target, changed_review)

    assert [item.fingerprint for item in first.candidates] == [
        item.fingerprint for item in second.candidates
    ]
    assert len(first.candidates) == 2
    assert "100 万元" not in first.model_dump_json()
    assert {item.candidate_type for item in first.candidates} == {
        "MODIFIED",
        "TABLE_STRUCTURE_EXPANDED",
    }
    assert {item.actual_outcome for item in first.candidates} == {"RISK", "MANUAL_REVIEW"}


def test_golden_validation_detects_missing_and_stale_annotations() -> None:
    generated = build_annotation_candidates(
        document("TEMPLATE", "a" * 64, "模板表格", table_index=2),
        document("TARGET", "b" * 64, "目标表格", table_index=2),
        review(),
    )
    partial = generated.model_copy(
        update={
            "candidates": [
                generated.candidates[0].model_copy(update={"classification": "RISK"}),
            ]
        }
    )
    result = validate_annotations(generated, partial)
    assert result.complete is False
    assert result.missing_annotation_count == 1

    merged = merge_existing_annotations(generated, partial)
    assert sum(item.classification is not None for item in merged.candidates) == 1


def test_suppressed_allowed_candidate_passes_but_emitted_allowed_candidate_mismatches() -> None:
    generated = build_annotation_candidates(
        document("TEMPLATE", "a" * 64, "模板表格", table_index=2),
        document("TARGET", "b" * 64, "目标表格", table_index=2),
        review(),
    )
    fully_annotated = generated.model_copy(
        update={
            "candidates": [
                item.model_copy(
                    update={
                        "classification": (
                            "ALLOWED_FILL" if item.actual_outcome == "RISK" else "MANUAL_REVIEW"
                        )
                    }
                )
                for item in generated.candidates
            ]
        }
    )
    emitted = validate_annotations(generated, fully_annotated)
    assert emitted.classification_mismatch_count == 1
    assert emitted.complete is False

    only_table = generated.model_copy(
        update={
            "candidates": [
                item for item in generated.candidates if item.actual_outcome == "MANUAL_REVIEW"
            ]
        }
    )
    suppressed = validate_annotations(only_table, fully_annotated)
    assert suppressed.suppressed_annotation_count == 1
    assert suppressed.complete is True
