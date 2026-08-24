from app.schemas.results import TaskResultData


def test_schema_20_result_remains_readable() -> None:
    value = {
        "schema_version": "2.0",
        "task_id": "tsk_legacy",
        "task_type": "DRAFT_REVIEW",
        "conclusion": "PASS",
        "summary": {
            "title": "历史结果",
            "description": "历史结果",
            "statistics": {
                "risk_count": 0,
                "deletion_or_missing_count": 0,
                "addition_or_change_count": 0,
                "review_count": 0,
                "passed_check_count": 0,
            },
        },
        "files": [], "risk_items": [], "review_items": [], "passed_checks": [],
        "diff_items": [], "fact_matrix": [], "rule_checks": [], "warnings": [],
        "advice": {}, "metadata": {
            "execution_mode": "RULE_BASED", "workflow_version": "0.4.0",
            "rules_version": "0.3.2", "primary_model": None, "model_runs": [],
        }, "mock": False,
    }
    assert TaskResultData.model_validate(value).schema_version == "2.0"
