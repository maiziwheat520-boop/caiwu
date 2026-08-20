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

Phase 0 governance and engineering scaffolding is in place. Phase 1 ledger
models and the first real schema migration are the next implementation task.
See [PROJECT_STATUS.md](PROJECT_STATUS.md) and
[docs/architecture/IMPLEMENTATION_BASELINE.md](docs/architecture/IMPLEMENTATION_BASELINE.md).

## Local development

Requirements: Python 3.12+, Docker Compose, and PostgreSQL 15+.

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
docker compose up -d postgres
alembic upgrade head
uvicorn ledgerbridge.main:app --reload
```

Quality gate:

```bash
ruff check .
ruff format --check .
mypy src
pytest
bandit -c pyproject.toml -r src
```

## Storage boundary

- Source code and design decisions: this repository.
- Runtime artifacts and database data: Docker volumes / `var/`, ignored by Git.
- Credentials and OAuth tokens: external secret store, never this repository.
- Historical design reviews: the parent workspace's `outputs/` directory.

See [docs/architecture/STORAGE.md](docs/architecture/STORAGE.md) for the full
layout and retention rules.
