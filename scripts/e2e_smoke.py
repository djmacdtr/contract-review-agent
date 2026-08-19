"""Verify the DRAFT_REVIEW Mock API -> Worker -> result loop."""

import os
import time

import httpx

BASE_URL = os.getenv("SMOKE_BASE_URL", "http://127.0.0.1:8000")

PAYLOADS = [
    (
        "/api/v1/draft-reviews",
        {
            "client_reference_id": "docker-smoke-draft",
            "target_file": {"url": "https://files.example.com/draft.docx?token=not-logged", "file_name": "draft.docx"},
            "template_file": {"url": "https://files.example.com/template.docx?token=not-logged", "file_name": "template.docx"},
            "reference_files": [{"url": "https://files.example.com/review.pdf?token=not-logged", "file_name": "review.pdf", "reference_type": "REVIEW_OPINION"}],
        },
    ),
]


def main() -> None:
    task_ids: list[str] = []
    # Local verification must not be routed through workstation proxy settings.
    with httpx.Client(base_url=BASE_URL, timeout=10, trust_env=False) as client:
        for path, payload in PAYLOADS:
            response = client.post(path, json=payload)
            response.raise_for_status()
            assert response.status_code == 202
            task_ids.append(response.json()["data"]["task_id"])

        for task_id in task_ids:
            history: list[tuple[str, str, int]] = []
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                detail = client.get(f"/api/v1/tasks/{task_id}")
                detail.raise_for_status()
                data = detail.json()["data"]
                state = (data["status"], data["stage"], data["progress"])
                if not history or history[-1] != state:
                    history.append(state)
                if data["status"] in {"SUCCEEDED", "FAILED"}:
                    break
                time.sleep(0.2)
            assert data["status"] == "SUCCEEDED", (task_id, data)
            result = client.get(f"/api/v1/tasks/{task_id}/result")
            result.raise_for_status()
            result_data = result.json()["data"]
            assert result_data["mock"] is True
            assert result_data["metadata"]["execution_mode"] == "MOCK"
            assert result_data["warnings"][0]["code"] == "MOCK_RESULT"
            print({"task_id": task_id, "history": history, "result": "valid-mock"})


if __name__ == "__main__":
    main()
