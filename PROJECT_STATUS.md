# Project status

Updated: 2026-08-21

## Current phase

Phase 0 — engineering and governance scaffold.

## Completed

- Independent repository layout.
- FastAPI, SQLAlchemy, Alembic, PostgreSQL, pytest, ruff, mypy, and security-scan wiring.
- Docker Compose services for API, worker, PostgreSQL, and a disabled Phase 3 mail collector profile.
- Codex implementation rules and Claude independent-review rules.
- Task, handoff, review, and storage conventions.
- Hermes deployment target verified as `/srv/ai-center/ledgerbridge`, API loopback port 8650.

## Active implementation owner

Codex on `ai/chatgpt/phase-0-scaffold`.

## Review owner

Claude, read-only until explicitly assigned implementation ownership by the user.

## Next task

Phase 1: implement the frozen core schema as one reviewed Alembic migration,
including the deferred per-entry/per-currency balance trigger and its OLD/NEW
entry-id behavior.

## Blocking decisions

None. Architecture is frozen by the 2026-08-20/21 implementation baseline.
