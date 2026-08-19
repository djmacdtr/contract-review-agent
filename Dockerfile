# syntax=docker/dockerfile:1.7
FROM node:22.19.0-alpine AS frontend-builder
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12.11-slim-bookworm AS python-builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml ./
COPY app/ ./app/
RUN --mount=type=cache,target=/root/.cache/pip python -m pip wheel --wheel-dir /wheels .

FROM python:3.12.11-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai
WORKDIR /opt/app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app
COPY --from=python-builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
COPY --chown=app:app app/ ./app/
COPY --chown=app:app alembic/ ./alembic/
COPY --chown=app:app alembic.ini pyproject.toml ./
COPY --chown=app:app scripts/ ./scripts/
COPY --from=frontend-builder --chown=app:app /frontend/dist ./app/static/console/
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test
USER root
COPY --chown=app:app tests/ ./tests/
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install "pytest==8.4.2" "pytest-asyncio==1.1.0" "pytest-cov==6.2.1" "reportlab==4.4.3"
USER app
CMD ["pytest"]
