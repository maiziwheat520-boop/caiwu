# Codex implementation rules

This file governs coding work in the LedgerBridge repository.

## Before changing code

1. Read `PROJECT_STATUS.md`.
2. Read `docs/architecture/IMPLEMENTATION_BASELINE.md` and the relevant task file.
3. Inspect the actual working tree and tests; never assume a previous agent finished.
4. Confirm that no other model currently owns writes to the same files.

## Ownership

- Codex implementation branches use `ai/chatgpt/<task>`.
- Claude implementation branches use `ai/claude/<task>` only when the user explicitly transfers write ownership.
- One writer owns a file or shared interface at a time. Independent modules, adapters, pages, and tests may be implemented in parallel in separate worktrees when the task message assigns disjoint ownership.
- The primary integrator alone owns shared data interfaces, Alembic migrations, integration commits, and production release manifests. Parallel branches must start from the same current integration baseline and sync before they drift into long-lived forks.
- Do not silently alter frozen architecture. Record a proposed change as an open issue in the task document and wait for user approval.
- Codex writes only from `D:\repos\LedgerBridge-Codex` or an explicitly assigned worktree under `D:\repos\_worktrees`.
- Claude writes only from `D:\repos\LedgerBridge-Claude` or an explicitly assigned worktree under `D:\repos\_worktrees` when authorized.
- `D:\repos\LedgerBridge` is a retired clone; never write or resume work there. Never create or restore a Git repository under `G:\我的云端硬盘`.
- Before any write, verify the repository root, branch, identity, clean/staged state,
  and the ownership checkpoint in `PROJECT_STATUS.md`.

## Financial safety invariants

- Prefer Review or Suspense over a guessed financial result.
- Money uses signed integer minor units; never float.
- Asset/expense normal balances are positive; liability/income/equity normal balances are negative.
- Every journal entry balances to zero per currency, enforced in PostgreSQL.
- Posted entries are immutable; corrections use reversal or adjustment entries.
- LLMs may suggest tags only. They never write postings.
- Source records are permanent. Raw artifact retention is a separate policy.
- Database migrations and tests must prove invariants; application convention is insufficient.

## Security and data handling

- Never commit financial statements, raw artifacts, email files, passwords, tokens, private keys, or OAuth refresh tokens.
- Use `.env` only for local non-committed configuration and an external secret store in production.
- Avoid logging raw financial fields or secrets.
- Tests use synthetic fixtures only.

## Definition of done

- Classify the change before implementation. Fast presentation/copy changes run affected component tests and the relevant build check. Functional changes run affected unit, contract, and integration tests during development, then one complete relevant suite and build at the integration gate. Formal data, identity, authorization, encryption, migrations, and infrastructure are high risk and retain the complete quality, security, backup, rollback, and restore gates.
- Every tier preserves integer-money, idempotency, authorization, audit, sensitive-path, and no-auto-posting invariants. Risk tiering changes when broad verification runs; it never weakens these invariants.
- High-risk and integration gates run `ruff check .`, `ruff format --check .`, `mypy src alembic tests scripts`, Bandit, dependency audit (strict from F-5), sensitive-path checks, and the relevant full test suite. Pull-request CI remains authoritative before merge.
- Migration upgrade and downgrade behavior is reviewed; destructive downgrade limitations are documented. A released, replay-safe importer may ingest additional files with bounded preflight, transaction, receipt, count, and idempotent replay checks; a new migration or importer capability still requires encrypted backup and isolated restore evidence.
- `PROJECT_STATUS.md` and the active task handoff are updated.
- One independent review is required at the integrated release gate; high-risk changes require no unresolved BLOCKER or HIGH finding before merge.
