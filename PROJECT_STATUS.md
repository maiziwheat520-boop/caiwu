# Project status

Updated: 2026-08-21

## Current phase

Phase 0 blocker/high-severity remediation is implemented and verified on Hermes.
The private GitHub source of truth, CI/PR evidence, and Claude re-review remain
before Phase 1 can begin.

## Completed

- Independent repository layout.
- FastAPI, SQLAlchemy, Alembic, PostgreSQL, pytest, ruff, mypy, and security-scan wiring.
- Docker Compose services for API, worker, PostgreSQL, and a disabled Phase 3 mail collector profile.
- Codex implementation rules and Claude independent-review rules.
- Task, handoff, review, and storage conventions.
- Hermes deployment target verified as `/srv/ai-center/ledgerbridge`, API loopback port 8650.
- Claude review captured in `docs/reviews/2026-08-21-phase-0-scaffold-claude.md`.
- Local remediation quality gates pass: ruff, format, mypy, pytest (95% coverage),
  Bandit, sensitive-path checks, and pip-audit.
- Hermes read-only audit confirmed the pre-remediation database contained only `alembic_version`,
  and the artifact volume was empty.
- Remediation commits `43a7fbb` and `207c9f8` are deployed on Hermes.
- PostgreSQL data checksums are on; migration upgrade/downgrade/upgrade passed.
- API and worker run as UID 10001; API artifacts are read-only, worker artifacts
  are writable, and API is published only on `127.0.0.1:8650`.

## Active implementation owner

Codex on `ai/chatgpt/phase-0-review-fixes`.

## Review owner

Claude, read-only until explicitly assigned implementation ownership by the user.

## Next task

Create/connect the empty private GitHub repository, push `main` and
`ai/chatgpt/phase-0-review-fixes`, run CI, open the review PR, and obtain
Claude approval. Phase 1 remains blocked until those gates pass.

## Blocking decisions

The private GitHub repository URL is pending from the user.
