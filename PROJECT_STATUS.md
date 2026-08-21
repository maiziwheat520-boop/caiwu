# Project status

Updated: 2026-08-21

## Current phase

Phase 0 blocker/high-severity remediation is implemented and verified on Hermes
and in GitHub CI. PR #1 is open and awaiting Claude independent re-review.
Phase 1 has not begun.

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
- Private GitHub source of truth: `maiziwheat520-boop/caiwu`; PR #1 targets `main`.
- Push and pull-request CI passed on `3ff2055` (`secrets`, `quality`, `compose`).
- GitHub Free does not permit branch protection on this private repository; the
  user chose to keep it private. PR/CI/one-writer rules remain mandatory but manual.

## Active implementation owner

Codex on `ai/chatgpt/phase-0-review-fixes`.

## Review owner

Claude, read-only until explicitly assigned implementation ownership by the user.

## Next task

Claude independently re-reviews PR #1 against the original report and records
a new verdict. Merge only after approval; Phase 1 remains blocked until then.

## Blocking decisions

No architecture decision is open. Known platform limitation: GitHub Free cannot
enforce branch protection on this private repository.
