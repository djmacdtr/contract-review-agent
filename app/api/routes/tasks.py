from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.deps import task_service
from app.core.enums import TaskStatus, TaskType
from app.schemas.common import ApiResponse
from app.schemas.results import TaskResultData
from app.schemas.tasks import TaskAccepted, TaskDetail, TaskListData
from app.services.task_service import TaskService

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


@router.get("", response_model=ApiResponse[TaskListData], summary="查询历史任务")
async def list_tasks(
    request: Request,
    service: Annotated[TaskService, Depends(task_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    task_type: TaskType | None = None,
    status_filter: Annotated[TaskStatus | None, Query(alias="status")] = None,
    client_reference_id: Annotated[str | None, Query(max_length=128)] = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
) -> ApiResponse[TaskListData]:
    data = await service.list_tasks(
        page=page,
        page_size=page_size,
        task_type=task_type,
        status=status_filter,
        client_reference_id=client_reference_id,
        created_from=created_from,
        created_to=created_to,
    )
    return ApiResponse(code="0", message="success", request_id=request.state.request_id, data=data)


@router.get("/{task_id}", response_model=ApiResponse[TaskDetail], summary="查询任务详情")
async def get_task(
    task_id: str,
    request: Request,
    service: Annotated[TaskService, Depends(task_service)],
) -> ApiResponse[TaskDetail]:
    data = await service.get_detail(task_id)
    return ApiResponse(code="0", message="success", request_id=request.state.request_id, data=data)


@router.get(
    "/{task_id}/result", response_model=ApiResponse[TaskResultData], summary="获取任务结果"
)
async def get_task_result(
    task_id: str,
    request: Request,
    service: Annotated[TaskService, Depends(task_service)],
) -> ApiResponse[Any]:
    data = await service.get_result(task_id)
    return ApiResponse(code="0", message="success", request_id=request.state.request_id, data=data)


@router.post(
    "/{task_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ApiResponse[TaskAccepted],
    summary="重试失败任务",
)
async def retry_task(
    task_id: str,
    request: Request,
    service: Annotated[TaskService, Depends(task_service)],
) -> ApiResponse[TaskAccepted]:
    data = await service.retry(task_id, request.state.request_id)
    return ApiResponse(code="0", message="accepted", request_id=request.state.request_id, data=data)

