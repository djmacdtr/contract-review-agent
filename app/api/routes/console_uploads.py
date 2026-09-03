from typing import Annotated

from fastapi import APIRouter, File, Request, UploadFile, status
from fastapi.responses import FileResponse

from app.core.config import get_settings
from app.core.errors import AppError
from app.schemas.common import ApiResponse
from app.schemas.files import ConsoleUploadResponse
from app.services.console_uploads import ConsoleUploadError, ConsoleUploadStore

router = APIRouter(prefix="/api/v1/console/uploads", tags=["console-internal"])


def _store() -> ConsoleUploadStore:
    return ConsoleUploadStore.from_settings(get_settings())


@router.post(
    "",
    include_in_schema=False,
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[ConsoleUploadResponse],
)
async def upload_console_file(
    request: Request,
    file: Annotated[UploadFile, File(description="DOCX 或 PDF 文件")],
) -> ApiResponse[ConsoleUploadResponse]:
    try:
        stored = await _store().save(file)
    except ConsoleUploadError as exc:
        raise AppError(exc.code, exc.message, status_code=400) from exc
    data = ConsoleUploadResponse(
        upload_id=stored.upload_id,
        url=stored.url,
        file_name=stored.file_name,
        mime_type=stored.mime_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
    )
    return ApiResponse(
        code="0", message="uploaded", request_id=request.state.request_id, data=data
    )


@router.get("/{upload_id}", include_in_schema=False)
async def download_console_file(upload_id: str) -> FileResponse:
    try:
        path, metadata = _store().resolve(upload_id)
    except ConsoleUploadError as exc:
        raise AppError(exc.code, exc.message, status_code=404) from exc
    return FileResponse(
        path,
        media_type=metadata["mime_type"],
        filename=metadata["file_name"],
        headers={"Cache-Control": "no-store", "X-Content-SHA256": metadata["sha256"]},
    )
