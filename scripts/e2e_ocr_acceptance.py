"""Validate one paired 46-page external-parser FINAL_COMPARE task using safe metrics."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
FIXTURE_BASE = os.getenv("OCR_FIXTURE_BASE_URL", "http://127.0.0.1:18080")
BASELINE_NAME = os.getenv("OCR_BASELINE_FILE_NAME", "融资租赁合同_电子印章示例_原版46页.pdf")
TARGET_NAME = os.getenv("OCR_TARGET_FILE_NAME", "融资租赁合同_电子印章示例_原版46页_扫描版.pdf")
EXPECTED_PAGES = int(os.getenv("OCR_EXPECTED_PAGES", "46"))
EXPECTED_VERSION = os.getenv("OCR_EXPECTED_WORKFLOW_VERSION", "0.4.2")
DEADLINE_SECONDS = float(os.getenv("OCR_E2E_TIMEOUT_SECONDS", "660"))
MAX_RESPONSE_BYTES = int(float(os.getenv("OCR_MAX_RESPONSE_MB", "50")) * 1024 * 1024)
MAX_FINAL_DIFFS = int(os.getenv("OCR_MAX_FINAL_DIFFS", "3"))
MIN_ALIGNMENT_COVERAGE = float(os.getenv("OCR_MIN_ALIGNMENT_COVERAGE", "0.90"))
PREVIOUS_DIFF_COUNT = int(os.getenv("OCR_PREVIOUS_DIFF_COUNT", "2099"))


def fixture_url(file_name: str) -> str:
    return f"{FIXTURE_BASE.rstrip('/')}/{urllib.parse.quote(file_name)}"


def call(method: str, path: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from project API") from exc


def required_metric(metadata: dict, name: str) -> int | float | str:
    value = metadata.get(name)
    if value is None:
        raise AssertionError(f"OCR parser metadata is missing {name}")
    return value


def validate_external_file(file_result: dict) -> dict:
    metadata = file_result.get("parser_metadata") or {}
    assert file_result["parser_name"] == "textin-document-parser"
    assert file_result["page_count"] == EXPECTED_PAGES
    assert metadata.get("ocr") is True
    assert metadata.get("parse_mode") == "auto"
    assert required_metric(metadata, "engine_version")
    assert required_metric(metadata, "duration_ms") >= 0
    assert required_metric(metadata, "response_size_bytes") <= MAX_RESPONSE_BYTES
    assert required_metric(metadata, "block_count") > 0
    assert required_metric(metadata, "table_count") > 0
    assert required_metric(metadata, "cell_count") > 0
    assert required_metric(metadata, "detail_page_count") == EXPECTED_PAGES
    assert required_metric(metadata, "bbox_block_count") > 0
    assert required_metric(metadata, "bbox_cell_count") > 0
    assert required_metric(metadata, "confidence_mean") > 0
    assert required_metric(metadata, "confidence_min") > 0
    return metadata


def has_traceable_location(diff: dict) -> bool:
    for side_name in ("baseline", "target"):
        side = diff.get(side_name)
        if not side:
            continue
        if side.get("locations") or side.get("location"):
            return True
    return False


def main() -> None:
    started = time.monotonic()
    created = call(
        "POST",
        "/api/v1/final-comparisons",
        {
            "client_reference_id": "ocr-46-page-host-acceptance",
            "baseline_file": {"url": fixture_url(BASELINE_NAME), "file_name": BASELINE_NAME},
            "target_file": {"url": fixture_url(TARGET_NAME), "file_name": TARGET_NAME},
        },
    )["data"]
    task_id = created["task_id"]
    history: list[tuple[str, str, int]] = []
    deadline = started + DEADLINE_SECONDS
    detail: dict = {}
    while time.monotonic() < deadline:
        detail = call("GET", f"/api/v1/tasks/{task_id}")["data"]
        point = (detail["status"], detail["stage"], detail["progress"])
        if not history or point != history[-1]:
            history.append(point)
        if detail["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            break
        time.sleep(1)
    elapsed = round(time.monotonic() - started, 3)
    if detail.get("status") != "SUCCEEDED":
        error = detail.get("error") or {}
        raise RuntimeError(f"OCR acceptance failed safely: {error.get('code', 'TIMEOUT')}")

    result = call("GET", f"/api/v1/tasks/{task_id}/result")["data"]
    files = {item["role"]: item for item in result["files"]}
    baseline = files["BASELINE"]
    target = files["TARGET"]
    baseline_metadata = validate_external_file(baseline)
    target_metadata = validate_external_file(target)
    diagnostics = result["metadata"]["comparison_diagnostics"]
    statistics = result["summary"]["statistics"]
    diff_items = result["diff_items"]
    result_size = len(json.dumps(result, ensure_ascii=False).encode())

    assert result["mock"] is False
    assert result["metadata"]["execution_mode"] == "RULE_BASED"
    assert result["metadata"]["workflow_version"] == EXPECTED_VERSION
    assert result["metadata"]["rules_version"] == EXPECTED_VERSION
    assert result["conclusion"] == "REVIEW_REQUIRED"
    assert statistics["risk_count"] == 0
    assert statistics["review_count"] <= MAX_FINAL_DIFFS
    assert not any(item["diff_type"] == "NUMERIC_CHANGED" for item in diff_items)
    assert len(diff_items) <= MAX_FINAL_DIFFS
    assert all(item.get("review_reason") for item in diff_items)
    assert not any("severity" in item for item in diff_items)
    assert diagnostics["reliable"] is True
    assert diagnostics["alignment_coverage_baseline"] >= MIN_ALIGNMENT_COVERAGE
    assert diagnostics["alignment_coverage_target"] >= MIN_ALIGNMENT_COVERAGE
    assert diagnostics["emitted_diff_count"] == len(diff_items)
    assert all(has_traceable_location(item) for item in diff_items)
    reduction = 1 - (len(diff_items) / PREVIOUS_DIFF_COUNT)
    assert reduction >= 0.97
    assert elapsed <= 600

    safe = {
        "task_id": task_id,
        "history": history,
        "elapsed_seconds": elapsed,
        "conclusion": result["conclusion"],
        "diff_count": len(diff_items),
        "diff_reduction_ratio": round(reduction, 6),
        "result_size_bytes": result_size,
        "baseline_parser": baseline["parser_name"],
        "target_parser": target["parser_name"],
        "baseline_metrics": baseline_metadata,
        "target_metrics": target_metadata,
        "comparison_diagnostics": diagnostics,
        "statistics": statistics,
        "warning_codes": [item["code"] for item in result.get("warnings", [])],
    }
    print(json.dumps(safe, ensure_ascii=False))


if __name__ == "__main__":
    main()
