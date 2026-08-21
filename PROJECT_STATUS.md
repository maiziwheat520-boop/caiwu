# Project status

Updated: 2026-08-21

## Current phase

Phase 0 scaffold is implemented and running on Hermes. Claude's independent
review returned `NOT APPROVED FOR PHASE 1`; the blocker/high-severity remediation
is active on a dedicated Codex branch.

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
- Hermes read-only audit confirmed the database contains only `alembic_version`,
  the artifact volume is empty, and PostgreSQL data checksums are currently off.

## Active implementation owner

Codex on `ai/chatgpt/phase-0-review-fixes`.

## Review owner

Claude, read-only until explicitly assigned implementation ownership by the user.

## Next task

Close Phase 0 review findings: establish the private GitHub source of truth,
commit and publish the reviewed remediation, rebuild the empty Hermes PostgreSQL
volume with checksums, validate migration upgrade/downgrade/upgrade on PostgreSQL,
and obtain Claude re-review approval. Phase 1 remains blocked until then.

## Blocking decisions

No architecture decision is open. GitHub publication and the destructive-but-
recoverable Hermes volume rebuild require explicit user authorization.
