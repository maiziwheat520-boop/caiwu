# Hermes deployment

Verified target: `aiadmin@192.168.1.39` (`ai-hub`)
Host path: `/srv/ai-center/ledgerbridge`
API binding: `127.0.0.1:8650` only

## Boundaries

- The private GitHub repository is the source of reviewed code and migrations.
- The Hermes directory is a rendered deployment tree, not a Git checkout or a
  second source of truth.
- `DEPLOYED_REVISION` contains the full reviewed Git SHA. `MANIFEST.sha256`
  binds that revision to every non-secret file in the rendered tree.
- PostgreSQL and artifacts use dedicated Docker named volumes.
- The production `.env` is mode 600 on Hermes and is excluded from the manifest,
  never copied back, and never committed.
- No public/LAN port is opened. Hermes integrations call the local API endpoint.

## Build a deployment bundle

Run these commands only from a clean checkout of the reviewed merge commit:

```bash
test -z "$(git status --porcelain)"
revision="$(git rev-parse HEAD)"
python scripts/deployment_manifest.py create \
  --root . \
  --output MANIFEST.sha256 \
  --revision "$revision"
printf '%s\n' "$revision" > DEPLOYED_REVISION
```

Transfer the rendered files, `MANIFEST.sha256`, and `DEPLOYED_REVISION` to
Hermes. Do not transfer `.git`, a development virtual environment, tests,
financial fixtures, or a local `.env`.

## Initial deploy

Before building, set `LEDGERBRIDGE_REVISION` to the short Git SHA and
`DEPLOYED_REVISION` to the full Git SHA in the protected Hermes `.env`. Then:

```bash
cd /srv/ai-center/ledgerbridge
revision="$(cat DEPLOYED_REVISION)"
python scripts/deployment_manifest.py verify \
  --root . \
  --manifest MANIFEST.sha256 \
  --expected-revision "$revision"
docker compose config --quiet
docker compose build api worker
image_revision="$(docker image inspect "ledgerbridge-app:${LEDGERBRIDGE_REVISION}" \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
test "$image_revision" = "$revision"
docker compose run --rm api alembic upgrade head
docker compose up -d postgres api worker
curl --fail http://127.0.0.1:8650/health/live
curl --fail http://127.0.0.1:8650/health/ready
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  http://127.0.0.1:8650/openapi.json)" = "404"
```

The `mail-collector` profile remains disabled until Phase 3 OAuth and secret-store
work is complete.

## Upgrade sequence

1. Back up PostgreSQL and the current deployment directory.
2. Render the reviewed code revision, manifest, and `DEPLOYED_REVISION`.
3. Verify the manifest before rendering Compose or building images.
4. Render Compose configuration and build revision-labeled images.
5. Verify the image revision label equals the full reviewed SHA.
6. Run `alembic upgrade head` as a one-shot container.
7. Recreate API/worker and verify live/ready, OpenAPI 404, worker heartbeat,
   migration head, and the loopback-only port.
8. Keep the previous image/revision until post-deploy checks pass.

Rollback is revision-specific. Never run an Alembic downgrade against production
financial data unless the migration's review explicitly proves it safe.
