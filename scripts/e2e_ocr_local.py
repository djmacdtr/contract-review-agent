"""Run one real OCR-backed FINAL_COMPARE task against locally served synthetic scans."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

API = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
BASELINE_URL = os.getenv("OCR_BASELINE_URL", "http://127.0.0.1:18080/ocr_scan_fixture.pdf")
TARGET_URL = os.getenv("OCR_TARGET_URL", BASELINE_URL)


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


def main() -> None:
    created = call(
        "POST",
        "/api/v1/final-comparisons",
        {
            "client_reference_id": "ocr-local-real-e2e",
            "baseline_file": {"url": BASELINE_URL, "file_name": "baseline-scan.pdf"},
            "target_file": {"url": TARGET_URL, "file_name": "target-scan.pdf"},
        },
    )["data"]
    task_id = created["task_id"]
    history: list[tuple[str, str, int]] = []
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        detail = call("GET", f"/api/v1/tasks/{task_id}")["data"]
        point = (detail["status"], detail["stage"], detail["progress"])
        if not history or point != history[-1]:
            history.append(point)
        if detail["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            break
        time.sleep(0.5)
    if detail["status"] != "SUCCEEDED":
        error = detail.get("error") or {}
        raise RuntimeError(f"OCR task failed safely: {error.get('code', detail['status'])}")
    result = call("GET", f"/api/v1/tasks/{task_id}/result")["data"]
    assert result["mock"] is False
    assert result["metadata"]["execution_mode"] == "RULE_BASED"
    assert all(item["parser_name"] == "textin-document-parser" for item in result["files"])
    assert all(item.get("parser_metadata", {}).get("ocr") is True for item in result["files"])
    safe = {
        "task_id": task_id,
        "history": history,
        "conclusion": result["conclusion"],
        "diff_count": len(result["diff_items"]),
        "parsers": [item["parser_name"] for item in result["files"]],
        "page_counts": [item["page_count"] for item in result["files"]],
        "confidence_min": [
            item.get("parser_metadata", {}).get("confidence_min") for item in result["files"]
        ],
    }
    print(safe)


if __name__ == "__main__":
    main()
