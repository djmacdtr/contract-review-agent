"""Run a real FINAL_COMPARE task using URLs reachable from the Worker container."""

import os
import time

import httpx

BASE_URL = os.getenv("SMOKE_BASE_URL", "http://127.0.0.1:8000")
BASELINE_URL = os.getenv("FINAL_BASELINE_URL", "http://fixture-server:8080/保证合同1.docx")
TARGET_URL = os.getenv("FINAL_TARGET_URL", "http://fixture-server:8080/保证合同3.docx")


def main() -> None:
    payload = {
        "client_reference_id": "docker-real-final-compare",
        "baseline_file": {"url": BASELINE_URL, "file_name": "保证合同1.docx"},
        "target_file": {"url": TARGET_URL, "file_name": "保证合同3.docx"},
    }
    with httpx.Client(base_url=BASE_URL, timeout=20, trust_env=False) as client:
        created = client.post("/api/v1/final-comparisons", json=payload)
        created.raise_for_status()
        task_id = created.json()["data"]["task_id"]
        deadline = time.monotonic() + 180
        history = []
        while time.monotonic() < deadline:
            detail = client.get(f"/api/v1/tasks/{task_id}")
            detail.raise_for_status()
            task = detail.json()["data"]
            state = (task["status"], task["stage"], task["progress"])
            if not history or history[-1] != state:
                history.append(state)
            if task["status"] in {"SUCCEEDED", "FAILED"}:
                break
            time.sleep(0.5)
        assert task["status"] == "SUCCEEDED", {"task_id": task_id, "error": task.get("error")}
        response = client.get(f"/api/v1/tasks/{task_id}/result")
        response.raise_for_status()
        result = response.json()["data"]
        assert result["mock"] is False
        assert result["metadata"]["execution_mode"] == "RULE_BASED"
        assert result["diff_items"]
        assert all(file.get("sha256") for file in result["files"])
        assert any(
            (item.get("baseline") or {}).get("location")
            or (item.get("target") or {}).get("location")
            for item in result["diff_items"]
        )
        print({"task_id": task_id, "history": history, "diff_count": len(result["diff_items"]), "execution_mode": "RULE_BASED"})


if __name__ == "__main__":
    main()
