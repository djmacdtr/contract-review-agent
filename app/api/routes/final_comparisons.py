from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import task_service
from app.schemas.common import ApiResponse
from app.schemas.requests import FinalComparisonCreate
from app.schemas.tasks import TaskAccepted
from app.services.task_service import TaskService

router = APIRouter(prefix="/api/v1/final-comparisons", tags=["final-comparisons"])


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ApiResponse[TaskAccepted],
    summary="创建放款阶段比对任务",
)
async def create_final_comparison(
    payload: FinalComparisonCreate,
    request: Request,
    service: Annotated[TaskService, Depends(task_service)],
) -> ApiResponse[TaskAccepted]:
    data = await service.create_final(payload, request.state.request_id)
    return ApiResponse(code="0", message="accepted", request_id=request.state.request_id, data=data)

