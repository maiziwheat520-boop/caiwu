FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

ARG UV_VERSION=0.12.5
ARG LEDGERBRIDGE_REVISION=dev

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

LABEL org.opencontainers.image.revision="${LEDGERBRIDGE_REVISION}"

WORKDIR /app

RUN useradd --create-home --uid 10001 ledgerbridge
RUN install -d -o 10001 -g 10001 /var/lib/ledgerbridge/artifacts
RUN python -m pip install --no-cache-dir --timeout 30 --retries 2 "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

COPY alembic.ini ./
COPY alembic ./alembic

USER ledgerbridge

CMD ["uvicorn", "ledgerbridge.main:app", "--host", "0.0.0.0", "--port", "8000"]
