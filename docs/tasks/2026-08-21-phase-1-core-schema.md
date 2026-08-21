# Task: Phase 1 Ledger Core schema

- Status: implementation complete; CI and independent review pending
- Implementation owner: Codex
- Review owner: Claude
- Base commit: `55f88dd9f8125d34a8952e5af56844c0033d7b27`
- Preflight branch: `ai/chatgpt/phase-1-prep`
- Implementation branch: `ai/chatgpt/phase-1-core-schema`
- Owned files: core models, one Phase 1 Alembic revision, ledger invariants, focused tests,
  dependency lock/CI, and the F-5 operational hardening files

## Goal

Implement the frozen double-entry Ledger Core with database-enforced balance,
auditability, and POSTED immutability. The preflight card and Claude report were merged in PR #4 before implementation began.

## In scope

- `Entity` with PERSON and COMPANY types.
- `Account` with ASSET, LIABILITY, INCOME, EXPENSE, EQUITY, and SUSPENSE classes.
- Entity-safe, explicit account-identifier uniqueness.
- `JournalEntry` with DRAFT, POSTED, and REVERSED status; source, adjustment,
  reversal, primary-account, and authorizing-audit references required by the baseline.
- `Posting` with signed integer minor units and currency (CNY in v0.1).
- `AuditEvent` append-only hash chain written through one database function;
  applications may not insert audit rows directly.
- One genuinely reversible Phase 1 Alembic migration for the Ledger Core.
- Deferred per-entry/per-currency balance constraint trigger that checks both
  OLD and NEW entry IDs when a posting moves.
- Database enforcement that POSTED entries/postings are immutable and corrections
  use explicit reversal or adjustment relationships.
- Actual-balance query logic that includes POSTED entries only.
- Focused model, migration, trigger, immutability, audit-chain, and balance tests.

## Out of scope

- RawArtifact, SourceRecord, ImportJob, connector SDK, parser implementations.
- Deduplication, reconciliation groups, tags, classification rules, review queue.
- Mail/OAuth, Hermes business endpoints, UI, dashboards, and LLM integration.
- Real financial evidence, credentials, or production data migration.

## Deferred baseline requirements

The following frozen requirements are not waived. They remain assigned to the
first phase that introduces the required objects:

- Baseline requirement 3, preventing RawArtifact deletion from cascading to
  SourceRecord, is mandatory in the Phase 2 evidence/import task.
- Baseline requirement 6, structured reconciliation evidence JSON, is mandatory
  in the Phase 5 reconciliation task.
- Baseline requirement 7, a schema version on every rule action, is mandatory in
  the Phase 5 classification/rule-engine task.

Each later task must copy its deferred requirement into scope and acceptance tests
before implementation begins.

## Frozen invariants

- Every journal entry balances to zero per currency at transaction commit.
- The deferred trigger validates affected OLD and NEW entries, not only the final row target.
- Assets/expenses are normally positive; liabilities/income/equity normally negative.
- POSTED entries are immutable; corrections never rewrite posted history.
- Journal creation references the audit event that authorized it without creating
  an unresolvable journal-entry/audit-event circular dependency.
- Audit events are append-only and hash-linked through one database function.
- Actual balances exclude DRAFT and REVERSED entries.
- No ambiguous `settlement_status` is introduced.

## Acceptance tests

- A balanced multi-posting CNY entry commits successfully.
- An unbalanced entry fails at transaction commit, including after a posting is moved
  from one entry to another; both the OLD and NEW entry IDs are checked.
- Currency buckets are checked independently.
- Attempts to update/delete a POSTED entry or its postings fail at the database layer.
- Reversal and adjustment relationships preserve rather than rewrite history.
- Direct application insertion/update/deletion of audit events is rejected; the
  database function produces a valid serialized hash chain.
- Actual-balance queries include POSTED entries only.
- An equal ASSET-to-ASSET internal transfer changes actual income and expense by
  exactly zero; assertions query the posted aggregates, not only entry balance.
- A transfer fee of 0.10 CNY increases actual expense by exactly 10 minor units.
- A credit-card purchase followed by repayment counts the purchase exactly once
  as expense and counts the repayment as zero expense.
- A partial refund reduces actual expense and leaves actual income unchanged.
- Migration upgrade creates the tables/functions/triggers; downgrade removes them;
  upgrade after downgrade recreates them. CI asserts object absence/presence, not
  only the Alembic version number.
- Ruff, formatting, mypy, Bandit, pip-audit strict mode, secret scan, coverage,
  PostgreSQL migration round-trip, and Compose gates pass.

## Claude follow-ups F-1 through F-7

| ID | Trigger | Status and required evidence |
|---|---|---|
| F-1 | Before Phase 1 implementation | **Complete.** Separate `LedgerBridge-Codex` and `LedgerBridge-Claude` clones; identities configured; ownership HEAD and paths recorded in `PROJECT_STATUS.md`. The retired shared clone is read-only. |
| F-2 | Before Phase 1 merge | **Implemented; CI confirmation pending.** `uv.lock` is committed with the implementation; local, CI, and Docker use frozen uv installs. |
| F-3 | With Phase 1 schema | **Complete locally.** Core ledger modules remain in the denominator; CI threshold is 95%; isolated PostgreSQL coverage is 97.53%. |
| F-4 | Before Phase 2 evidence ingestion | Add executable backup/restore automation and record a restore-to-empty-instance rehearsal with migration/checksum validation. |
| F-5 | During Phase 1 | **Implemented; CI confirmation pending.** Deployment manifest/revision label, OpenAPI 404, strict locked dependency audit, real worker heartbeat, and full-history gitleaks checkout are present and locally/Hermes validated where applicable. |
| F-6 | Conditional blocker | Enable private branch protection or equivalent if a second human gets write access, Phase 2 real data/OAuth begins, or any direct push bypasses PR. |
| F-7 | Every Phase 1 migration | **Complete locally.** The migration drops and recreates Phase 1 tables, functions, triggers, indexes, and enum types; an isolated-database test asserts object absence and presence. |

## Implementation evidence

- Isolated PostgreSQL 15 on Hermes: 31 tests passed; coverage 97.53%.
- Migration exercised upgrade -> Phase 1 downgrade -> upgrade in a separate temporary database,
  with table/function/trigger absence and presence assertions.
- Business assertions query posted aggregates for internal transfer, 0.10 CNY fee,
  credit-card purchase/repayment, and partial refund behavior.
- Runtime-role ACL rejects direct AuditEvent inserts; owner-level update/delete triggers reject
  mutation; the serialized SHA-256 chain is independently recomputed in tests.
- Ruff, format, mypy, Bandit, sensitive-path scan, and locked strict dependency audit pass.
- Frozen lock Docker image builds as UID 10001; revision label, worker heartbeat, API ready,
  OpenAPI 404, and deployment manifest verification pass in an isolated Hermes network.
- No production Hermes code, schema, credentials, database volume, or artifact volume changed.

## Review gate

Claude reviews the implementation diff from its separate clone and writes only the
explicitly authorized review report. Phase 1 cannot merge on test output alone:
Claude must inspect trigger SQL, audit function permissions/hash serialization,
POSTED immutability, downgrade behavior, and whether tests can fail for the intended defects.
