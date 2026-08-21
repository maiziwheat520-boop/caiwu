# Task: Phase 0 scaffold

- Status: review
- Implementation owner: Codex
- Review owner: Claude
- Branch: `ai/chatgpt/phase-0-scaffold`
- Owned files: repository scaffold, excluding future Phase 1 models/migrations

## Goal

Create an independently versioned, runnable, reviewable LedgerBridge repository
without implementing financial business logic ahead of Phase 1.

## In scope

- Python package, API liveness endpoint, worker process boundary.
- Docker Compose wiring for API, worker, PostgreSQL, and Phase 3 mail boundary.
- Alembic infrastructure with no business-schema revision yet.
- CI quality and security gates.
- Storage boundaries and Codex/Claude governance.

## Out of scope

- Ledger tables, triggers, and account initialization.
- Real connectors, mailbox OAuth, financial files, or production deployment.
- UI, dashboard, LLM classification, and Hermes integration.

## Frozen invariants

- The repository contains no real financial evidence or credentials.
- Phase 1 cannot proceed by weakening the implementation baseline.
- One implementation owner writes at a time.

## Acceptance tests

- Python tests and static checks pass in a clean Python 3.12 environment.
- Docker Compose configuration renders with `.env.example` values.
- Git reports a dedicated repository on the required Codex branch.
- Claude can review using only `CLAUDE.md`, project status, baseline, and this task.

## Implementation evidence

- Local: ruff, format, mypy, pytest, Bandit, and pip-audit passed.
- Hermes: API and PostgreSQL healthy; worker running.
- `GET /health/live` and `GET /health/ready` returned HTTP 200.
- API bound only to `127.0.0.1:8650`; dedicated runtime volumes created.
- Docker was unavailable locally; Compose/build/runtime validation completed on Hermes.

## Review findings

Claude returned `NOT APPROVED FOR PHASE 1`. The authoritative review is
`docs/reviews/2026-08-21-phase-0-scaffold-claude.md`; remediation is tracked in
`docs/tasks/2026-08-21-phase-0-review-fixes.md`.
