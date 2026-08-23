FROM node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM python:3.12-slim

ENV LEDGERBRIDGE_MODE=synthetic-preview \
    PORT=8080 \
    BIND_ADDRESS=0.0.0.0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SESSION_COOKIE_SECURE=0 \
    SITE_ROOT=/site

WORKDIR /app
COPY deploy/server.py /app/run_preview.py
COPY server /app/server
COPY --from=build /app/dist /site

USER 65534:65534

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()"

CMD ["python", "/app/run_preview.py"]
