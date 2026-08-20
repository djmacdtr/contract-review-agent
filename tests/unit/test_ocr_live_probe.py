from pathlib import Path

import orjson

from app.core.errors import WorkflowError
from app.documents.models import DocumentBlock, DocumentLocation, ParsedDocument
from scripts import ocr_live_probe as probe_module
from scripts.ocr_live_probe import probe, safe_failure


class RecordingParser:
    def __init__(self) -> None:
        self.mode = None

    async def parse(self, file, *, mode):
        self.mode = mode
        return ParsedDocument(
            file_id=file.file_id,
            role=file.role,
            file_name=file.file_name,
            sha256=file.sha256,
            page_count=1,
            blocks=[
                DocumentBlock(
                    block_id="block_1",
                    type="PARAGRAPH",
                    order=0,
                    raw_text="synthetic",
                    normalized_text="synthetic",
                    location=DocumentLocation(page=1, source="OCR", confidence=0.99),
                )
            ],
            parser_name="textin-document-parser",
            parser_metadata={"parse_mode": "auto", "duration_ms": 10},
        )


async def test_probe_passes_explicit_mode_and_returns_safe_metrics(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(b"%PDF-1.7\nsynthetic")
    parser = RecordingParser()

    result = await probe(path, mode="auto", document_parser=parser)

    assert parser.mode == "auto"
    assert result["page_count"] == 1
    assert result["block_count"] == 1
    assert result["parser_metadata"]["parse_mode"] == "auto"
    assert "file_name" not in result
    assert "sha256" not in result


def test_probe_failure_output_contains_only_safe_workflow_fields() -> None:
    error = WorkflowError(
        "OCR_SERVICE_UNAVAILABLE",
        "OCR 服务连接失败或超时",
        details={
            "component": "EXTERNAL_DOCUMENT_PARSER",
            "failure_kind": "CONNECT_TIMEOUT",
            "attempts": 1,
            "elapsed_ms": 20,
        },
    )

    result = safe_failure(error)

    assert result == {
        "ok": False,
        "code": "OCR_SERVICE_UNAVAILABLE",
        "message": "OCR 服务连接失败或超时",
        "details": error.details,
    }


def test_probe_cli_defaults_to_auto_and_prints_safe_json(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(b"%PDF-1.7\nsynthetic")
    observed = {}

    async def fake_probe(path, *, mode):
        observed["mode"] = mode
        return {"page_count": 1, "parser_name": "test"}

    monkeypatch.setattr(probe_module, "probe", fake_probe)
    monkeypatch.setattr("sys.argv", ["ocr_live_probe.py", str(path)])

    assert probe_module.main() == 0
    assert observed["mode"] == "auto"
    assert orjson.loads(capsys.readouterr().out) == {
        "page_count": 1,
        "parser_name": "test",
    }


def test_probe_cli_catches_workflow_error_without_traceback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(b"%PDF-1.7\nsynthetic")

    async def fail(path, *, mode):
        raise WorkflowError(
            "OCR_SERVICE_UNAVAILABLE",
            "OCR 服务连接失败或超时",
            details={
                "component": "EXTERNAL_DOCUMENT_PARSER",
                "failure_kind": "READ_TIMEOUT",
                "attempts": 1,
                "elapsed_ms": 30,
            },
        )

    monkeypatch.setattr(probe_module, "probe", fail)
    monkeypatch.setattr("sys.argv", ["ocr_live_probe.py", str(path), "--mode", "scan"])

    assert probe_module.main() == 1
    captured = capsys.readouterr()
    failure = orjson.loads(captured.err)
    assert failure["details"]["failure_kind"] == "READ_TIMEOUT"
    assert "Traceback" not in captured.err
    assert captured.out == ""
