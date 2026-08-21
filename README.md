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

Phase 0 is deployed. Phase 1 Ledger Core implementation is in progress, including
database-enforced balance, immutability, and audit-chain invariants.
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

See [docs/architecture/STORAGE.md](docs/architecture/STORAGE.md) for the full
layout and retention rules, [docs/architecture/LEDGER_CORE_OPERATIONS.md](docs/architecture/LEDGER_CORE_OPERATIONS.md)
for the Phase 1 lifecycle and audit contract, and
[docs/architecture/DEPLOYMENT_HERMES.md](docs/architecture/DEPLOYMENT_HERMES.md)
for the split runtime/migration database identities.
