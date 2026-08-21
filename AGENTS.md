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
- One model writes at a time. The other model reviews only.
- Do not silently alter frozen architecture. Record a proposed change as an open issue in the task document and wait for user approval.
- Codex writes only from `G:\我的云端硬盘\AI\LedgerBridge-Codex`.
- Claude writes only from `G:\我的云端硬盘\AI\LedgerBridge-Claude` when explicitly authorized.
- `G:\我的云端硬盘\AI\LedgerBridge` is a retired clone; never write or resume work there.
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

- Relevant unit/integration/property tests pass.
- `ruff check .`, `ruff format --check .`, `mypy src alembic tests scripts`, Bandit,
  dependency audit (strict from F-5), and sensitive-path checks pass.
- Migration upgrade and downgrade behavior is reviewed; destructive downgrade limitations are documented.
- `PROJECT_STATUS.md` and the active task handoff are updated.
- Claude review has no unresolved BLOCKER or HIGH findings before merge.
