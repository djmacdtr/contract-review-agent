from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import task_service
from app.schemas.common import ApiResponse
from app.schemas.requests import DraftReviewCreate
from app.schemas.tasks import TaskAccepted
from app.services.task_service import TaskService

router = APIRouter(prefix="/api/v1/draft-reviews", tags=["draft-reviews"])


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ApiResponse[TaskAccepted],
    summary="创建合同起草检查任务",
)
async def create_draft_review(
    payload: DraftReviewCreate,
    request: Request,
    service: Annotated[TaskService, Depends(task_service)],
) -> ApiResponse[TaskAccepted]:
    data = await service.create_draft(payload, request.state.request_id)
    return ApiResponse(code="0", message="accepted", request_id=request.state.request_id, data=data)

