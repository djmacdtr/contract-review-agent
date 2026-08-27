import json
from pathlib import Path

import httpx
import pytest

from app.adapters.document_parser.textin_client import TextInDocumentParserClient
from app.core.config import Settings
from app.core.errors import WorkflowError
from app.services.downloader import PDF_MIME, LocalFile


def make_file(tmp_path: Path) -> LocalFile:
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.7\nsynthetic")
    return LocalFile(
        file_id="fil_test",
        role="TARGET",
        file_name="scan.pdf",
        safe_url="http://fixture/scan.pdf",
        path=path,
        file_size=path.stat().st_size,
        sha256="a" * 64,
        detected_mime_type=PDF_MIME,
    )


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "OCR_ENABLED": True,
        "OCR_BASE_URL": "https://ocr.invalid",
        "OCR_API_KEY": "unit-test-secret",
        "OCR_AUTH_HEADER": "x-api-key",
        "OCR_HTTP_RETRY_ATTEMPTS": 0,
    }
    values.update(overrides)
    return Settings(**values)


def success_payload() -> dict[str, object]:
    path = Path(__file__).parents[1] / "fixtures" / "ocr" / "textin_success.json"
    return json.loads(path.read_text(encoding="utf-8"))


async def test_client_sends_binary_auth_and_fixed_parameters(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["auth"] = request.headers.get("x-api-key")
        observed["content_type"] = request.headers.get("content-type")
        observed["query"] = dict(request.url.params)
        observed["body"] = await request.aread()
        return httpx.Response(200, json=success_payload())

    client = TextInDocumentParserClient(settings(), transport=httpx.MockTransport(handler))
    response = await client.parse(make_file(tmp_path), mode="auto")

    assert response.code == 200
    assert response._response_size_bytes is not None
    assert response._response_size_bytes > 0
    assert observed["auth"] == "unit-test-secret"
    assert observed["content_type"] == "application/octet-stream"
    assert observed["body"] == b"%PDF-1.7\nsynthetic"
    query = observed["query"]
    assert query["parse_mode"] == "auto"
    assert query["get_image"] == "none"
    assert query["raw_ocr"] == "1"
    assert query["char_details"] == "0"


async def test_client_requests_stamp_images_only_when_explicitly_enabled(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["query"] = dict(request.url.params)
        return httpx.Response(200, json=success_payload())

    client = TextInDocumentParserClient(settings(), transport=httpx.MockTransport(handler))
    await client.parse(make_file(tmp_path), mode="scan", include_stamp_images=True)

    query = observed["query"]
    assert query["get_image"] == "objects"
    assert query["image_output_type"] == "base64str"


@pytest.mark.parametrize(
    ("status", "business_code", "expected"),
    [
        (401, 200, "OCR_AUTH_FAILED"),
        (400, 200, "OCR_REQUEST_INVALID"),
        (200, 40429, "OCR_QUOTA_EXCEEDED"),
        (200, 50207, "OCR_PARTIAL_FAILURE"),
        (200, 40423, "OCR_PASSWORD_REQUIRED"),
        (200, 10703, "OCR_PARSE_FAILED"),
    ],
)
async def test_client_maps_safe_errors(
    tmp_path: Path, status: int, business_code: int, expected: str
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"code": business_code, "msg": "vendor detail"})

    client = TextInDocumentParserClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(WorkflowError) as caught:
        await client.parse(make_file(tmp_path), mode="scan")
    assert caught.value.code == expected
    assert "unit-test-secret" not in str(caught.value)


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_client_retries_only_transient_gateway_errors(tmp_path: Path, status: int) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(status, content=b"")
        return httpx.Response(200, json=success_payload())

    client = TextInDocumentParserClient(
        settings(OCR_HTTP_RETRY_ATTEMPTS=1, OCR_RETRY_BACKOFF_SECONDS=0),
        transport=httpx.MockTransport(handler),
    )
    assert (await client.parse(make_file(tmp_path), mode="scan")).code == 200
    assert calls == 2


@pytest.mark.parametrize(
    ("status", "failure_kind"),
    [(502, "UPSTREAM_502"), (503, "UPSTREAM_503"), (504, "UPSTREAM_504")],
)
async def test_client_exposes_safe_gateway_failure_details_after_retries(
    tmp_path: Path, status: int, failure_kind: str
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, content=b"vendor detail must not escape")

    client = TextInDocumentParserClient(
        settings(OCR_HTTP_RETRY_ATTEMPTS=1, OCR_RETRY_BACKOFF_SECONDS=0),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(WorkflowError) as caught:
        await client.parse(make_file(tmp_path), mode="auto")

    assert caught.value.code == "OCR_SERVICE_UNAVAILABLE"
    assert caught.value.details == {
        "component": "EXTERNAL_DOCUMENT_PARSER",
        "failure_kind": failure_kind,
        "attempts": 2,
        "elapsed_ms": caught.value.details["elapsed_ms"],
    }
    assert isinstance(caught.value.details["elapsed_ms"], int)
    assert caught.value.details["elapsed_ms"] >= 0
    assert calls == 2
    assert "vendor detail" not in str(caught.value.details)


async def test_client_applies_response_limit_before_transient_retry(tmp_path: Path) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, content=b"x" * 2048)

    client = TextInDocumentParserClient(
        settings(
            OCR_MAX_RESPONSE_MB=0.001,
            OCR_HTTP_RETRY_ATTEMPTS=1,
            OCR_RETRY_BACKOFF_SECONDS=0,
        ),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(WorkflowError) as caught:
        await client.parse(make_file(tmp_path), mode="scan")
    assert caught.value.code == "OCR_RESPONSE_INVALID"
    assert calls == 1


async def test_client_rejects_oversized_or_invalid_response(tmp_path: Path) -> None:
    async def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2048)

    client = TextInDocumentParserClient(
        settings(OCR_MAX_RESPONSE_MB=0.001), transport=httpx.MockTransport(oversized)
    )
    with pytest.raises(WorkflowError) as caught:
        await client.parse(make_file(tmp_path), mode="scan")
    assert caught.value.code == "OCR_RESPONSE_INVALID"

    async def invalid_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    client = TextInDocumentParserClient(settings(), transport=httpx.MockTransport(invalid_json))
    with pytest.raises(WorkflowError) as caught:
        await client.parse(make_file(tmp_path), mode="scan")
    assert caught.value.code == "OCR_RESPONSE_INVALID"


async def test_client_maps_timeout_without_leaking_configuration(tmp_path: Path) -> None:
    async def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    client = TextInDocumentParserClient(settings(), transport=httpx.MockTransport(timeout))
    with pytest.raises(WorkflowError) as caught:
        await client.parse(make_file(tmp_path), mode="scan")
    assert caught.value.code == "OCR_SERVICE_UNAVAILABLE"
    assert "ocr.invalid" not in str(caught.value)
    assert "unit-test-secret" not in str(caught.value)


@pytest.mark.parametrize(
    ("error_type", "failure_kind"),
    [
        (httpx.ConnectTimeout, "CONNECT_TIMEOUT"),
        (httpx.ReadTimeout, "READ_TIMEOUT"),
        (httpx.WriteTimeout, "WRITE_TIMEOUT"),
        (httpx.ConnectError, "NETWORK_ERROR"),
    ],
)
async def test_client_exposes_safe_network_failure_details(
    tmp_path: Path,
    error_type: type[httpx.HTTPError],
    failure_kind: str,
) -> None:
    async def fail(request: httpx.Request) -> httpx.Response:
        raise error_type("sensitive transport detail", request=request)

    client = TextInDocumentParserClient(
        settings(OCR_HTTP_RETRY_ATTEMPTS=0), transport=httpx.MockTransport(fail)
    )
    with pytest.raises(WorkflowError) as caught:
        await client.parse(make_file(tmp_path), mode="auto")

    assert caught.value.code == "OCR_SERVICE_UNAVAILABLE"
    assert caught.value.details["component"] == "EXTERNAL_DOCUMENT_PARSER"
    assert caught.value.details["failure_kind"] == failure_kind
    assert caught.value.details["attempts"] == 1
    assert "sensitive transport detail" not in str(caught.value.details)


async def test_enabled_client_requires_complete_runtime_configuration(tmp_path: Path) -> None:
    client = TextInDocumentParserClient(settings(OCR_BASE_URL=""))
    with pytest.raises(WorkflowError) as caught:
        await client.parse(make_file(tmp_path), mode="scan")
    assert caught.value.code == "OCR_NOT_CONFIGURED"
