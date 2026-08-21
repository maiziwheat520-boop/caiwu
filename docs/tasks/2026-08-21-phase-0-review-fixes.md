# Task: Phase 0 review fixes

- Status: implementation
- Implementation owner: Codex
- Review owner: Claude
- Branch: `ai/chatgpt/phase-0-review-fixes`
- Base commit: `e504a42`
- Owned files: Phase 0 scaffold, CI, migrations, container hardening, and governance status

## Goal

Close every blocker and high-severity finding in Claude's 2026-08-21 Phase 0
review without entering Phase 1 business-schema work.

## In scope

- Require an explicit database URL and make Alembic environment-driven.
- Add the model registry, baseline migration, and PostgreSQL migration tests.
- Harden sensitive-path rules, secret scanning, CI coverage, and dependency checks.
- Correct artifact ownership and separate API read-only from worker read-write access.
- Harden containers and initialize PostgreSQL with data checksums.
- Establish a private GitHub remote and auditable branch/PR workflow.
- Rebuild the empty Hermes PostgreSQL volume and validate migrations there.

## Out of scope

- Ledger business tables, balance triggers, connectors, UI, and financial data.
- Editing Claude's signed review report.
- Weakening frozen architecture or security rules to make checks pass.

## Acceptance tests

- Ruff, formatting, mypy, pytest with at least 80% coverage, Bandit, pip-audit,
  and sensitive-path checks pass.
- CI runs secret scanning and PostgreSQL migration upgrade/downgrade/upgrade.
- Hermes PostgreSQL reports data checksums on after a backed-up volume rebuild.
- API and worker run as UID/GID 10001; API artifact mount is read-only and worker
  artifact mount is writable.
- Private GitHub is the source of truth and deployment identifies an immutable revision.
- Claude re-review returns approval before Phase 1 begins.

## Evidence collected

- Local quality gates pass; 7 tests pass with 95% coverage.
- Pip-audit reports no known vulnerabilities; the local package itself is not on PyPI.
- Hermes services are healthy on the pre-remediation deployment.
- Hermes database contains only the `alembic_version` table.
- Hermes artifact volume contains zero files.
- Hermes PostgreSQL currently reports `data_checksums=off`.

## Remaining work

- Obtain explicit authorization to commit and publish the remediation.
- Create/configure the private GitHub repository and protected workflow.
- Back up and rebuild the empty Hermes PostgreSQL volume with checksums enabled.
- Deploy the reviewed revision, run PostgreSQL migrations, and verify service hardening.
- Request Claude re-review and record the final verdict.