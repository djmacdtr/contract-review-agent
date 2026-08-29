import hashlib
from pathlib import Path

import pytest

from app.core.errors import WorkflowError
from app.workflows.report_regeneration import (
    LocalRegenerationDownloader,
    remap_file_references,
    validate_file_reference_remap,
)


def test_remap_changes_file_references_but_preserves_business_ids() -> None:
    source = {
        "task_id": "tsk_source",
        "files": [{"file_id": "fil_source", "file_ids": ["fil_source"]}],
        "risk_items": [
            {
                "risk_id": "risk_diff_000001",
                "related_diff_ids": ["diff_000001"],
                "source_evidence": [
                    {
                        "file_id": "fil_source",
                        "location": {"structure_id": "fil_source_p000001"},
                    }
                ],
            }
        ],
    }

    value = remap_file_references(
        source,
        {"fil_source": "fil_current"},
        task_id="tsk_new",
    )

    assert value["task_id"] == "tsk_new"
    assert value["files"][0]["file_id"] == "fil_current"
    assert value["files"][0]["file_ids"] == ["fil_current"]
    assert value["risk_items"][0]["risk_id"] == "risk_diff_000001"
    assert value["risk_items"][0]["related_diff_ids"] == ["diff_000001"]
    assert value["risk_items"][0]["source_evidence"][0]["file_id"] == "fil_current"


def test_file_reference_preflight_rejects_old_and_unknown_ids() -> None:
    with pytest.raises(WorkflowError) as old_error:
        validate_file_reference_remap(
            {"file_id": "fil_old"},
            old_file_ids={"fil_old"},
            new_file_ids={"fil_new"},
        )
    assert old_error.value.details["failure_code"] == "OLD_FILE_ID_REMAINED"

    with pytest.raises(WorkflowError) as unknown_error:
        validate_file_reference_remap(
            {"source_file_id": "fil_unknown"},
            old_file_ids={"fil_old"},
            new_file_ids={"fil_new"},
        )
    assert unknown_error.value.details["failure_code"] == "UNKNOWN_FILE_ID"


@pytest.mark.asyncio
async def test_local_regeneration_downloader_never_uses_url(tmp_path: Path) -> None:
    path = tmp_path / "fixture.docx"
    path.write_bytes(b"PK\x03\x04fixture")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    downloader = LocalRegenerationDownloader(
        {"fil_new": path},
        {"fil_new": digest},
    )

    result = await downloader.prepare(
        [{"file_id": "fil_new", "role": "REFERENCE", "file_name": "fixture.docx"}],
        None,
    )

    assert result[0].path == path
    assert result[0].safe_url == ""
