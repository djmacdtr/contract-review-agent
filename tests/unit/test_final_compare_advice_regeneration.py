from types import SimpleNamespace

from app.core.config import Settings
from app.core.enums import FileRole
from app.workflows.final_compare_advice_regeneration import (
    FinalCompareAdviceRegenerationWorkflowExecutor,
)
from scripts.regenerate_final_compare_advice_host import canary_passed


def test_advice_canary_accepts_seven_of_eight_only_for_not_specific_items() -> None:
    stats = {
        "risk_count": 8,
        "accepted_count": 7,
        "fallback_count": 1,
        "quality_rejections": {
            "NOT_SPECIFIC": 3,
            "DUPLICATED": 0,
            "INTERNAL_ID": 0,
            "MULTI_SENTENCE": 0,
            "RISK_ID_INVALID": 0,
            "TECHNICAL_TERM": 0,
        },
        "failure_codes": {},
    }

    assert canary_passed(stats)

    stats["quality_rejections"]["DUPLICATED"] = 1
    assert not canary_passed(stats)

    stats["quality_rejections"]["DUPLICATED"] = 0
    stats["accepted_count"] = 6
    assert not canary_passed(stats)


def test_advice_regeneration_remaps_file_references_but_preserves_business_ids() -> None:
    source = SimpleNamespace(
        files=[
            SimpleNamespace(
                id="fil_source_base",
                role=FileRole.BASELINE,
                file_name="baseline.docx",
            ),
            SimpleNamespace(
                id="fil_source_target",
                role=FileRole.TARGET,
                file_name="target.docx",
            ),
        ]
    )
    source_result = {
        "task_id": "tsk_source",
        "files": [
            {"file_id": "fil_source_base", "role": "BASELINE", "file_name": "baseline.docx"},
            {"file_id": "fil_source_target", "role": "TARGET", "file_name": "target.docx"},
        ],
        "risk_items": [
            {
                "risk_id": "risk_stable",
                "source_evidence": [{"file_id": "fil_source_target"}],
                "related_diff_ids": ["diff_stable"],
            }
        ],
        "diff_items": [
            {
                "diff_id": "diff_stable",
                "baseline": {"file_id": "fil_source_base"},
                "target": {"file_id": "fil_source_target"},
            }
        ],
        "stamp_images": [],
    }
    current_files = [
        {"file_id": "fil_current_base", "role": "BASELINE", "file_name": "baseline.docx"},
        {"file_id": "fil_current_target", "role": "TARGET", "file_name": "target.docx"},
    ]
    executor = FinalCompareAdviceRegenerationWorkflowExecutor(
        Settings(_env_file=None),
        llm=None,
    )

    remapped = executor._remap_source_result(
        source,
        source_result,
        current_files,
        "tsk_regenerated",
    )

    assert remapped["task_id"] == "tsk_regenerated"
    assert [item["risk_id"] for item in remapped["risk_items"]] == ["risk_stable"]
    assert [item["diff_id"] for item in remapped["diff_items"]] == ["diff_stable"]
    assert remapped["files"][0]["file_id"] == "fil_current_base"
    assert remapped["files"][1]["file_id"] == "fil_current_target"
    assert remapped["risk_items"][0]["source_evidence"][0]["file_id"] == (
        "fil_current_target"
    )
    assert remapped["diff_items"][0]["baseline"]["file_id"] == "fil_current_base"
    assert remapped["diff_items"][0]["target"]["file_id"] == "fil_current_target"
