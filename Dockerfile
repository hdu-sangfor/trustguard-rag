# syntax=docker/dockerfile:1.7
ARG DOCKERHUB_REGISTRY=docker.io
ARG UV_INDEX_URL
ARG UV_EXTRA_INDEX_URL
ARG UV_DEFAULT_INDEX
ARG PIP_INDEX_URL
ARG PIP_EXTRA_INDEX_URL

FROM ${DOCKERHUB_REGISTRY}/astral/uv:0.11.7 AS uv
FROM ${DOCKERHUB_REGISTRY}/library/python:3.11-slim
ARG UV_INDEX_URL
ARG UV_EXTRA_INDEX_URL
ARG UV_DEFAULT_INDEX
ARG PIP_INDEX_URL
ARG PIP_EXTRA_INDEX_URL

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -n "${UV_DEFAULT_INDEX}" ]; then export UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX}"; fi; \
    if [ -n "${UV_INDEX_URL}" ]; then export UV_INDEX_URL="${UV_INDEX_URL}"; fi; \
    if [ -n "${UV_EXTRA_INDEX_URL}" ]; then export UV_EXTRA_INDEX_URL="${UV_EXTRA_INDEX_URL}"; fi; \
    if [ -n "${PIP_INDEX_URL}" ]; then export PIP_INDEX_URL="${PIP_INDEX_URL}"; fi; \
    if [ -n "${PIP_EXTRA_INDEX_URL}" ]; then export PIP_EXTRA_INDEX_URL="${PIP_EXTRA_INDEX_URL}"; fi; \
    uv sync --frozen --no-dev --no-install-project --extra local-embedding

COPY app ./app
COPY frontend ./frontend

ENV RAG_API_HOST=0.0.0.0 \
    RAG_API_PORT=18200 \
    RAG_MODE=ingest \
    RAG_LOCAL_STORAGE_DIR=/data/storage \
    HF_HOME=/models/huggingface

RUN mkdir -p /data/storage /models/huggingface

EXPOSE 18200

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18200"]
