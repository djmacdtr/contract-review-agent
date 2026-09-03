import asyncio
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import ORJSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.api.error_handlers import register_error_handlers
from app.api.routes import console_uploads, draft_reviews, final_comparisons, health, tasks
from app.core.config import get_settings
from app.core.ids import new_request_id
from app.core.logging import configure_logging
from app.db.session import engine
from app.services.console_uploads import ConsoleUploadStore

settings = get_settings()
configure_logging(settings.LOG_LEVEL)
logger = structlog.get_logger(__name__)
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(ConsoleUploadStore.from_settings(settings).cleanup_expired)
    logger.info("api_started", environment=settings.APP_ENV)
    yield
    await engine.dispose()
    logger.info("api_stopped")


app = FastAPI(
    title="合同智能检查 Agent API",
    description=(
        "FINAL_COMPARE 提供确定性文件版本比对；DRAFT_REVIEW 提供动态多文档起草检查。"
        "结果不构成合同审查或法律意见。"
    ),
    version="0.2.2",
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)
register_error_handlers(app)
app.include_router(health.router)
app.include_router(draft_reviews.router)
app.include_router(final_comparisons.router)
app.include_router(tasks.router)
app.include_router(console_uploads.router)


@app.middleware("http")
async def request_context(request: Request, call_next):
    clear_contextvars()
    provided = request.headers.get("X-Request-ID", "")
    req_id = provided if REQUEST_ID_PATTERN.fullmatch(provided) else new_request_id()
    request.state.request_id = req_id
    bind_contextvars(request_id=req_id)
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = req_id
    logger.info(
        "http_request",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    clear_contextvars()
    return response


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/console/")


static_dir = Path(__file__).parent / "static" / "console"
if static_dir.exists():
    app.mount("/console", StaticFiles(directory=static_dir, html=True), name="console")
