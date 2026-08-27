from app.schemas.results import TaskResultData


def base_result() -> dict:
    return {
        "schema_version": "2.0",
        "task_id": "tsk_schema_v2",
        "task_type": "FINAL_COMPARE",
        "conclusion": "RISK_FOUND",
        "summary": {
            "title": "比对结果",
            "description": "存在一项确认变化",
            "statistics": {
                "risk_count": 1,
                "deletion_or_missing_count": 0,
                "addition_or_change_count": 1,
                "review_count": 0,
                "passed_check_count": 1,
            },
        },
        "files": [],
        "risk_items": [
            {
                "risk_id": "risk_000001",
                "module_code": "VERSION_CHANGE",
                "risk_type": "ADDITION_OR_CHANGE",
                "change_type": "NUMERIC_CHANGED",
                "title": "期限发生变化",
                "description": "基准文件与当前文件的期限不同。",
                "source_evidence": [],
                "related_diff_ids": ["diff_000001"],
                "related_rule_ids": [],
                "requires_manual_action": True,
            }
        ],
        "review_items": [],
        "passed_checks": [
            {
                "check_id": "check_alignment",
                "module_code": "DOCUMENT_ALIGNMENT",
                "title": "文档对齐可靠",
                "description": "覆盖率达到规则阈值。",
            }
        ],
        "diff_items": [
            {
                "diff_id": "diff_000001",
                "diff_type": "NUMERIC_CHANGED",
                "title": "期限变化",
                "baseline": None,
                "target": None,
                "segments": [],
                "confidence": 1,
                "requires_manual_review": True,
            }
        ],
        "fact_matrix": [],
        "rule_checks": [],
        "warnings": [],
        "advice": {},
        "metadata": {
            "execution_mode": "RULE_BASED",
            "workflow_version": "0.3.1",
            "rules_version": "0.3.1",
            "primary_model": None,
            "model_runs": [],
        },
        "mock": False,
    }


def test_schema_v2_has_ungraded_risk_review_and_pass_contract() -> None:
    result = TaskResultData.model_validate(base_result())
    dumped = result.model_dump(mode="json")

    assert dumped["summary"]["statistics"] == {
        "risk_count": 1,
        "deletion_or_missing_count": 0,
        "addition_or_change_count": 1,
        "review_count": 0,
        "passed_check_count": 1,
        "legacy_statistics": False,
    }
    assert "severity" not in dumped["diff_items"][0]
    assert dumped["risk_items"][0]["risk_type"] == "ADDITION_OR_CHANGE"
    assert dumped["stamp_images"] == []


def test_stamp_image_schema_rejects_external_urls() -> None:
    payload = base_result()
    payload["stamp_images"] = [{"file_name": "盖章.pdf", "page": 1, "data_uri": "https://ocr.invalid/stamp.png"}]
    try:
        TaskResultData.model_validate(payload)
    except ValueError:
        return
    raise AssertionError("external stamp image URL must not pass result validation")


def test_legacy_result_is_readable_without_exposing_risk_levels() -> None:
    payload = base_result()
    payload["schema_version"] = "1.0"
    payload["summary"]["statistics"] = {
        "total": 6,
        "high": 2,
        "medium": 1,
        "low": 2,
        "info": 1,
    }
    payload["diff_items"][0]["severity"] = "HIGH"
    payload["risk_items"] = [
        {
            "risk_id": "risk_old",
            "category": "SOURCE_CONFLICT",
            "severity": "HIGH",
            "title": "旧版风险",
            "description": "旧版描述",
            "sources": [],
        }
    ]

    dumped = TaskResultData.model_validate(payload).model_dump(mode="json")

    assert dumped["schema_version"] == "1.0"
    assert dumped["summary"]["statistics"]["risk_count"] == 3
    assert dumped["summary"]["statistics"]["review_count"] == 3
    assert dumped["summary"]["statistics"]["legacy_statistics"] is True
    assert "severity" not in dumped["diff_items"][0]
    assert dumped["risk_items"][0]["module_code"] == "LEGACY_RESULT"
    assert "severity" not in dumped["risk_items"][0]
