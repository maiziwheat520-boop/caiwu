FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --uid 10001 ledgerbridge
RUN install -d -o 10001 -g 10001 /var/lib/ledgerbridge/artifacts

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

RUN python -m pip install --no-cache-dir .

USER ledgerbridge

CMD ["uvicorn", "ledgerbridge.main:app", "--host", "0.0.0.0", "--port", "8000"]
