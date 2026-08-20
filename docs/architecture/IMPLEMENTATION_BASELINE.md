# LedgerBridge v0.1 implementation baseline

Status: frozen for implementation
Date: 2026-08-21

## Purpose

LedgerBridge is a self-hosted financial ledger gateway. It receives Chinese
personal-finance exports, preserves evidence, normalizes records, builds a
double-entry ledger, identifies duplicates and transfers, and exposes a narrow
query/review API to Hermes.

It is not a bank-login tool, credential vault, autonomous AI bookkeeper, or
investment platform. Uncertain results go to Review or Suspense.

## Stack and deployment

- Python 3.12+, FastAPI, SQLAlchemy 2.x, Alembic, Pydantic.
- PostgreSQL 15+.
- pytest, pytest-asyncio, Hypothesis, ruff, mypy, security scanning.
- Docker Compose on a Debian VM/LXC under PVE.
- Target services: API, worker, PostgreSQL, and Phase 3 mail collector.

## Frozen ledger semantics

- Core chain: `Entity -> Account <- Posting -> JournalEntry`.
- Account classes: ASSET, LIABILITY, INCOME, EXPENSE, EQUITY, SUSPENSE.
- Signed integer minor units: assets/expenses normally positive;
  liabilities/income/equity normally negative.
- CNY only in v0.1, but balance checks are per currency.
- Every entry must have postings summing to zero per currency.
- Journal status: DRAFT, POSTED, REVERSED.
- POSTED means immutable. Corrections are explicit REVERSAL or ADJUSTMENT entries.
- Period close is account/period external verification and is independent of POSTED.
- Late postings supersede and re-check the relevant close; they do not rewrite time.

## Evidence and import chain

`RawArtifact -> SourceRecord -> Parse -> Normalize -> Validate -> Draft Entry -> Dedup -> Reconcile -> Posted Entry`

- Raw artifacts are immutable and keyed by SHA-256; retention is configurable.
- Source records are permanent even if an artifact is later removed by policy.
- Source identity is `(artifact_id, record_locator)`.
- External identity is unique by `(account_id, source, external_transaction_id)` when present.
- Fingerprints are heuristic evidence, never an unconditional delete key.
- Connectors implement only `detect()` and `parse()`; classification stays in the versioned rule engine.

## Suspense and reconciliation

Per entity, create four suspense accounts:

- `Suspense:Unclassified`
- `Suspense:PendingTransfer`
- `Suspense:LoanBreakdown`
- `Suspense:BalanceGap`

`journal_entry.primary_account_id` provides per-source-account attribution without
splitting suspense accounts further. Reconciliation groups are metadata and may
represent 1:1, 1:N, or N:1 relationships. They never rewrite source evidence.

## Audit and AI boundary

- Audit events are append-only and linked by a serialized hash chain through one
  database function. Applications may not insert them directly.
- Every journal entry references the audit event that authorized its creation.
- Classification rules are versioned data.
- Tags are analytical dimensions and may be suggested by an LLM.
- LLM/Hermes cannot create or alter postings.
- Hermes uses the API only: accounts, transactions, balances, coverage, reviews,
  and review confirmation.

## Database requirements for Phase 1

1. Deferred balance constraint trigger checks both OLD and NEW entry IDs on moves.
2. Audit creation avoids the journal-entry/audit-event circular dependency.
3. Raw-artifact deletion policy cannot cascade-delete source records.
4. Actual balances include POSTED entries only.
5. Account-identifier uniqueness must be explicit and entity-safe.
6. Reconciliation keeps structured evidence JSON.
7. Rule actions carry a schema version.
8. Do not add an ambiguous `settlement_status`; model authorization/posted source
   state only when a concrete source requires it.

## Required acceptance scenarios

- Re-importing identical evidence adds zero transactions.
- Equal internal transfer changes neither income nor expense.
- Transfer with 0.10 fee records exactly 0.10 expense.
- Credit-card purchase and repayment reports only the purchase as spending.
- Partial refund reduces expense and does not create income.
