FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

ARG LEDGERBRIDGE_REVISION=dev

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

LABEL org.opencontainers.image.revision="${LEDGERBRIDGE_REVISION}"

WORKDIR /app
RUN printf '%s\n' "${LEDGERBRIDGE_REVISION}" > /app/DEPLOYED_REVISION

RUN useradd --create-home --uid 10001 ledgerbridge
RUN install -d -m 0700 -o 10001 -g 10001 /var/lib/ledgerbridge/artifacts
COPY docker/uv-requirements.txt ./docker/uv-requirements.txt
RUN python -m pip install --no-cache-dir --timeout 30 --retries 2 \
    --require-hashes --only-binary=:all: -r docker/uv-requirements.txt

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts/__init__.py scripts/run_account_registry_intake.py ./scripts/
COPY --chmod=0755 docker/run-internal-reader.sh ./docker/run-internal-reader.sh

USER ledgerbridge

CMD ["uvicorn", "ledgerbridge.main:app", "--host", "0.0.0.0", "--port", "8000"]
