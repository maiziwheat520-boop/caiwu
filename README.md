# LedgerBridge

LedgerBridge is a self-hosted financial ledger gateway for importing personal
financial evidence, normalizing source records, and building a traceable
double-entry ledger for trusted queries through Hermes.

The implementation priority is deliberately conservative:

1. ledger correctness;
2. evidence preservation;
3. deterministic imports;
4. reconciliation;
5. API and AI integration.

When the system is uncertain, it creates a review item or uses a suspense
account. It must not invent financial facts.

## Repository status

Phase 3 Slice A is deployed on Hermes at revision
`e426b488b2abb02f10ef02a61aae7ebe24c3283f` with migration `20260822_0004`.
The review-only branch `ai/chatgpt/phase-3-connector-runner` contains the
subsequent async dispatch, isolated runner, bounded upload adapter, role split,
and release-readiness hardening through `e2c31be`, including forward migration
`20260824_0009`. Those changes are not deployed, and no real evidence or
Connector is registered. Phase 4 framework commit `bbe776f` adds a default-disabled,
fail-closed Microsoft Graph provider adapter and explicit Connector factory
registry; it still has no OAuth client, manifest, real parser, or production
switch. The current Phase 5 framework adds side-effect-free deduplication,
zero-sum reconciliation proposals, and explicit Suspense resolution contracts;
it also has no persistence or production switch. The deployed service remains
on Slice A with no real evidence imported.
See [PROJECT_STATUS.md](PROJECT_STATUS.md) and
[docs/architecture/IMPLEMENTATION_BASELINE.md](docs/architecture/IMPLEMENTATION_BASELINE.md).

## Local development

Requirements: Python 3.12+, Docker Compose, and PostgreSQL 15+.

```bash
cp .env.example .env
# Replace both example database passwords in .env before continuing.
uv sync --frozen --extra dev
docker compose up -d postgres
docker compose exec -T postgres sh /docker-entrypoint-initdb.d/10-ledgerbridge-runtime-role.sh
docker compose --profile tools run --rm migrate
docker compose up -d api worker
```

Quality gate:

```bash
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev ruff format --check .
uv run --frozen --extra dev mypy src alembic tests scripts
uv run --frozen --extra dev pytest
uv run --frozen --extra dev bandit -c pyproject.toml -r src alembic scripts
uv export --quiet --frozen --extra dev --no-emit-project --format requirements.txt --output-file /tmp/ledgerbridge-audit-requirements.txt
uv run --frozen --extra dev pip-audit --strict --requirement /tmp/ledgerbridge-audit-requirements.txt
```

## Storage boundary

- Source code and design decisions: this repository.
- Runtime artifacts and database data: Docker volumes / `var/`, ignored by Git.
- Credentials and OAuth tokens: external secret store, never this repository.
- Historical design reviews: the parent workspace's `outputs/` directory.
- Artifact defaults: 50 MiB per file, 10 GiB published, 512 MiB staging, with
  no automatic deletion under quota pressure.

See [docs/architecture/STORAGE.md](docs/architecture/STORAGE.md) for the full
layout and retention rules, [docs/architecture/LEDGER_CORE_OPERATIONS.md](docs/architecture/LEDGER_CORE_OPERATIONS.md)
for the Phase 1 lifecycle and audit contract, and
[docs/architecture/DEPLOYMENT_HERMES.md](docs/architecture/DEPLOYMENT_HERMES.md)
for the split runtime/migration database identities.
