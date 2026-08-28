FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

ARG LEDGERBRIDGE_REVISION=dev

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

LABEL org.opencontainers.image.revision="${LEDGERBRIDGE_REVISION}"

WORKDIR /app

RUN useradd --create-home --uid 10002 ledgerbridge-ocr
COPY docker/uv-requirements.txt ./docker/uv-requirements.txt
RUN python -m pip install --no-cache-dir --timeout 30 --retries 2 \
    --require-hashes --only-binary=:all: -r docker/uv-requirements.txt

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra ocr --no-install-project

COPY src ./src
COPY scripts/__init__.py scripts/preprocess_bill_images.py ./scripts/
RUN uv sync --frozen --no-dev --extra ocr --no-editable

USER ledgerbridge-ocr

CMD ["python", "scripts/preprocess_bill_images.py", "--image-directory", "/input", "--output", "/output/bill-ocr.json"]
