"""Validate one real text-PDF to scanned-PDF FINAL_COMPARE task using safe metrics."""

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
EXPECTED_VERSION = os.getenv("OCR_EXPECTED_WORKFLOW_VERSION", "0.3.0")
DEADLINE_SECONDS = float(os.getenv("OCR_E2E_TIMEOUT_SECONDS", "660"))
MAX_RESPONSE_BYTES = int(float(os.getenv("OCR_MAX_RESPONSE_MB", "50")) * 1024 * 1024)


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
    metadata = target.get("parser_metadata") or {}
    result_size = len(json.dumps(result, ensure_ascii=False).encode())

    assert result["mock"] is False
    assert result["metadata"]["execution_mode"] == "RULE_BASED"
    assert result["metadata"]["workflow_version"] == EXPECTED_VERSION
    assert result["metadata"]["rules_version"] == EXPECTED_VERSION
    assert baseline["parser_name"] == "pdfplumber"
    assert baseline["page_count"] == EXPECTED_PAGES
    assert target["parser_name"] == "textin-document-parser"
    assert target["page_count"] == EXPECTED_PAGES
    assert metadata.get("ocr") is True
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
    assert elapsed <= 600

    safe = {
        "task_id": task_id,
        "history": history,
        "elapsed_seconds": elapsed,
        "conclusion": result["conclusion"],
        "diff_count": len(result["diff_items"]),
        "result_size_bytes": result_size,
        "baseline_parser": baseline["parser_name"],
        "target_parser": target["parser_name"],
        "page_count": target["page_count"],
        "engine_version": metadata["engine_version"],
        "service_duration_ms": metadata["duration_ms"],
        "response_size_bytes": metadata["response_size_bytes"],
        "block_count": metadata["block_count"],
        "table_count": metadata["table_count"],
        "cell_count": metadata["cell_count"],
        "detail_page_count": metadata["detail_page_count"],
        "bbox_block_count": metadata["bbox_block_count"],
        "bbox_cell_count": metadata["bbox_cell_count"],
        "confidence_mean": metadata["confidence_mean"],
        "confidence_min": metadata["confidence_min"],
        "warning_codes": [item["code"] for item in target.get("parse_warnings", [])],
    }
    print(json.dumps(safe, ensure_ascii=False))


if __name__ == "__main__":
    main()
