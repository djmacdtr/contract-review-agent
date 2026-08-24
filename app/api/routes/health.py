from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.session import get_session
from app.schemas.common import ApiResponse, HealthData, ReadyData

router = APIRouter(tags=["health"])


@router.get("/health", response_model=ApiResponse[HealthData], summary="进程健康检查")
async def health(request: Request) -> ApiResponse[HealthData]:
    return ApiResponse(
        code="0",
        message="success",
        request_id=request.state.request_id,
        data=HealthData(status="ok"),
    )


@router.get("/ready", response_model=ApiResponse[ReadyData], summary="数据库就绪检查")
async def ready(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ApiResponse[ReadyData]:
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise AppError("SERVICE_NOT_READY", "数据库尚未就绪", status_code=503) from exc
    return ApiResponse(
        code="0",
        message="success",
        request_id=request.state.request_id,
        data=ReadyData(
            status="ready",
            database="ok",
            ocr_configured=settings.ocr_configured,
            llm_configured=settings.llm_configured,
        ),
    )
