# Task: Phase 1 Ledger Core schema

- Status: complete; PR #5 merged as `2028e3a` and the reviewed Phase 1 revision is
  deployed and verified on Hermes
- Implementation owner: Codex
- Review owner: Codex self-audit; Claude independent-audit entry point preserved
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
| F-2 | Before Phase 1 merge | **Implemented.** `uv.lock` is committed; local, CI, and Docker use frozen uv installs. The Docker/CI uv bootstrap wheel is version- and SHA-256-locked. PR #5 self-audit commit `a094b1a` passed all six push/PR jobs. |
| F-3 | With Phase 1 schema | **Complete locally.** Core ledger modules, worker, and deployment-manifest script are in the denominator; CI threshold is 95%; the latest isolated PostgreSQL run reached 99.31%. |
| F-4 | Before Phase 2 evidence ingestion | Add executable backup/restore automation and record a restore-to-empty-instance rehearsal with migration/checksum validation. |
| F-5 | During Phase 1 | **Implemented.** Deployment manifest/revision label, OpenAPI 404, strict locked dependency audit, ephemeral worker heartbeat, root-safe manifest exclusions, and full-history gitleaks checkout are present. PR #5 self-audit commit `a094b1a` passed all six push/PR jobs. |
| F-6 | Conditional blocker | Enable private branch protection or equivalent if a second human gets write access, Phase 2 real data/OAuth begins, or any direct push bypasses PR. |
| F-7 | Every Phase 1 migration | **Complete locally.** The migration drops and recreates Phase 1 tables, functions, triggers, indexes, and enum types; an isolated-database test asserts object absence and presence. |

## Implementation evidence

- Final isolated PostgreSQL 15 on Hermes: 51 tests passed; coverage 99.31% under the expanded denominator.
- Migration exercised upgrade -> Phase 1 downgrade -> upgrade in a separate temporary database,
  with table/function/trigger absence and presence assertions.
- Business assertions query posted aggregates for internal transfer, 0.10 CNY fee,
  credit-card purchase/repayment, and partial refund behavior.
- The API/worker log in directly as non-owner `ledgerbridge_app`; pool reuse and `RESET ROLE` remain unprivileged, and direct audit INSERT, trigger ALTER, and TRUNCATE fail.
- Owner-level update/delete triggers reject audit mutation; the serialized SHA-256 chain is independently recomputed, and unique indexes reject concurrent stale-snapshot forks.
- Ruff, format, mypy, Bandit, sensitive-path scan, and locked strict dependency audit pass; Linux pip-audit reports no known vulnerabilities.
- Frozen lock Docker image builds as UID 10001; revision label, worker heartbeat, API ready/live,
  OpenAPI 404, runtime `session_user/current_user`, migration head, and deployment manifest
  verification pass in isolated Hermes networks.
- Production deployment was separately authorized after merge; the validated
  results are recorded below.

## Production deployment

PR #5 merged as `2028e3a99abe5e20c842a95ec22f2931878d39ee`. Hermes now runs
`ledgerbridge-app:2028e3a`, whose OCI revision label matches the full merge SHA.
The 25-file deployment manifest verifies against the rendered runtime tree.

The existing Phase 0 owner role `ledgerbridge` was retained for migrations after
explicit user confirmation; API and worker use `ledgerbridge_app`. Migration head
is `20260821_0002`; all five business tables and the audit table contain zero rows.
API live/ready, OpenAPI 404, worker heartbeat, UID 10001, container hardening,
loopback-only port binding, role attributes/grants, and database checksums passed.

Backups are under `/srv/ai-center/backups/ledgerbridge/20260821-phase1-deploy-2028e3a`.
The old deployment tree remains at
`/srv/ai-center/ledgerbridge.previous-61ad910-before-phase1-20260821`.
No real financial evidence was introduced.

## First-review remediation

The immutable Claude report is `docs/reviews/2026-08-21-phase-1-core-schema-claude.md`
(review commit `88d4e775a42e924998173590a0d91f34830d1fbc`, verdict CHANGES REQUIRED).
The committed remediation through `3e1e6fc` resolves:

- P1-B1/P1-H1: remove `SET ROLE`; split runtime LOGIN from owner/migration credentials.
- P1-H2: one partial unique reversal per original POSTED entry.
- P1-H3: freeze Account class after POSTED use; the final self-audit further makes Account and JournalEntry entity identities immutable from creation.
- P1-H4: make the posting-move test fail when the OLD-entry check is removed.
- M1/M2/M3: unique audit successors/genesis with REPEATABLE READ concurrency,
  deterministic runtime pool-reuse coverage, and a behavioral per-currency test.
- M4-M7 and safe LOW items: external role bootstrap, guarded downgrade revokes,
  explicit lifecycle/sequence-gap documentation, expanded coverage, root-only
  manifest exclusions plus unsafe-path/symlink tests, strict shell blocks,
  hash-locked uv bootstrap, cache restoration, and ephemeral worker heartbeat.
- M8 is explicitly deferred to the workflow/API phase without weakening the
  Phase 1 creation-authorization AuditEvent binding.

## Final Codex self-audit

The final report is
`docs/reviews/2026-08-21-phase-1-core-schema-final-codex.md`.
The self-audit found and fixed:

- a HIGH cross-entity POSTED-entry path caused by mutable draft entity identities;
- a LOW deployment-manifest symlink exclusion-order bypass.

Account and JournalEntry `entity_id` values are now immutable from creation,
POSTED transition revalidates every Posting entity, and the manifest rejects a
non-excluded symlink before file exclusions. Final isolated validation passed
51 tests, 99.31% coverage, migration round-trip, static analysis, and strict
dependency audit.

The Codex Security workbench could not create a scan because of a GBK decode
failure on the Chinese Windows path, and Codex Security Access reported
`not_granted`. The report preserves this limitation and the complete manual
review evidence.

## Review gate

The user directed Codex to perform the final review while conserving Claude
capacity. The immutable Claude report and separate Claude clone remain available
for a later independent audit, but another Claude run was not required for merge.

Self-audit commit `a094b1abfd56c2eb625eae95275bb875e698c3b3` and evidence head
`e739088ad8f4d0eec91fda6e1e5ab3c268b1b2e6` passed their push/PR CI gates.
PR #5 merged only after explicit user authorization. Production deployment then
received separate authorization and passed the deployment record's acceptance gates.
