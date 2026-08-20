# Hermes deployment

Verified target: `aiadmin@192.168.1.39` (`ai-hub`)
Host path: `/srv/ai-center/ledgerbridge`
API binding: `127.0.0.1:8650` only

## Boundaries

- The local LedgerBridge Git repository is the source of code and reviewed migrations.
- The Hermes directory is a deployment checkout, not a second source of truth.
- PostgreSQL and artifacts use dedicated Docker named volumes.
- The production `.env` is mode 600 on Hermes and is never copied back or committed.
- No public/LAN port is opened. Hermes integrations call the local API endpoint.

## Initial deploy

```bash
cd /srv/ai-center/ledgerbridge
docker compose build api worker
docker compose run --rm api alembic upgrade head
docker compose up -d postgres api worker
curl --fail http://127.0.0.1:8650/health/live
curl --fail http://127.0.0.1:8650/health/ready
```

The `mail-collector` profile remains disabled until Phase 3 OAuth and secret-store
work is complete.

## Upgrade sequence

1. Back up PostgreSQL and the current deployment directory.
2. Stage the reviewed code revision.
3. Render Compose configuration and build images.
4. Run `alembic upgrade head` as a one-shot container.
5. Recreate API/worker and verify live/ready endpoints.
6. Keep the previous image/revision until post-deploy checks pass.

Rollback is revision-specific. Never run an Alembic downgrade against production
financial data unless the migration's review explicitly proves it safe.
