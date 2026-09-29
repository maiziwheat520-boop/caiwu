# Payroll and reconciliation database workbench

- Status: in progress
- Implementation owner: Codex
- Core branch: `ai/chatgpt/payroll-database-workbench-20260928`
- Web branch: `ai/chatgpt/payroll-database-workbench-web-20260928`
- Risk: high (formal payroll facts, database migration, financial exports, deployment)

## User outcome

Run payroll and hotel reconciliation from the private VM103 website regardless of which personal
computer is in use. PostgreSQL is the authoritative business store. The only spreadsheet output
is the bank remittance workbook; payroll details, rules, history, summaries, and reconciliation
views remain in the website.

## Module seams

### Payroll

The Payroll Module owns employee pay parameters, source material interpretation, calculation,
blocking issues, immutable locked versions, and deterministic bank-file export. Its external
Interface is intentionally small:

1. read one period workspace;
2. ingest one supported material;
3. resolve a blocking issue or update a draft;
4. lock the exact reviewed version;
5. export bank files from that locked version.

The Module does not submit payments. Exporting a bank-format file records an audit receipt but
does not make the file payable or prove that a payment occurred.

### Reconciliation

The Reconciliation Module owns monthly income, expense, transfer, store, category, and exception
views over persisted financial facts. It consumes a locked payroll projection through a
versioned Interface. It never copies an editable payroll total and never writes a reconciliation
workbook.

## Frozen safety rules

- Money is signed integer minor units. Floating-point values are forbidden in stored facts and
  calculation Interfaces.
- A payroll period may have many drafts but at most one current locked version. Locked rows are
  immutable; corrections create a superseding version or a supplemental batch.
- Every material, rule version, locked batch, and export is content-addressed and auditable.
- Blocking issues prevent lock and export. Missing employee type, required location, required job
  group, required attendance, payee, account, remittance group, or reconciliation mapping fails
  closed.
- Employee types are exactly `REGULAR`, `TEMPORARY`, and `PAYEE_ONLY`. Regular employees require
  location, job group, and attendance. Temporary employees require location but not job group or
  attendance. Payee-only records do not create an independent wage calculation.
- Bank amount plus cash amount must equal locked net pay for every employee. Cash rows never enter
  a bank file.
- A bank file is generated only from a locked version. Repeating the same export produces the same
  logical rows, totals, and content digest.
- Reconciliation reads the payroll period and actual disbursement period as distinct fields. The
  current rule remains: payroll belongs to its earning period; the expense enters reconciliation
  for the actual payment month.
- Neither Module creates accounting postings or submits a bank instruction.

## Bank workbook contract

- Legacy `.xls`, sheet name `Sheet1`.
- Business columns are payee name, payee account, amount in yuan, and memo.
- Account identifiers remain text. Amounts are rendered to exactly two decimal places from minor
  units. Memos are non-empty and at most 40 characters.
- Normal MyBank exports retain the existing five controlled grouping rules and filenames.
- Zero or negative bank amounts, unknown locations, missing accounts, ambiguous payees, duplicate
  employees, and unexplained account/payee conflicts reject the complete export.
- Supplemental amounts stay inside the same locked net pay, but leave in a separate
  `YY.M月 补发代发表.xls`; they are subtracted from the normal group file. The lock
  rejects a supplemental amount above the bank-payable amount.

## Reconciliation migration contract

- The current workbook is read only as migration evidence, never as a live database.
- Import preflight records the source digest, sheet/period inventory, row counts, control totals,
  and every unmapped or conflicting row before any formal write.
- Existing normalized bank facts are reused by stable source identity. Workbook rows may provide
  reviewed classifications or legacy-only adjustments, but may not duplicate a bank transaction.
- Every imported monthly/store/category total must reconcile to its persisted detail or remain an
  explicit blocking exception.

## First vertical slice

1. Add the database-owned Payroll Module and migration.
2. Port employee-type validation, minor-unit calculation inputs, locking, and deterministic `.xls`
   export with synthetic tests.
3. Reuse the existing payroll website shell, replacing TEST_ONLY/provider language with the
   database workspace and adding lock/download actions.
4. Add a read-only reconciliation workbook preflight against the current production sample.
5. Run representative historical parity checks before importing or deploying formal data.

## Release gates

- Migration upgrade, replay, ACL, and immutable-lock checks pass on PostgreSQL 15.
  The payroll migration is forward-only; production rollback requires verified restore.
- Synthetic unit, contract, Core route, BFF, component, and `.xls` round-trip tests pass.
- A representative real payroll period matches the old program per employee, remittance group,
  and total without writing the source files or production database.
- The latest reconciliation workbook preflight closes all period/store/category totals or records
  explicit blocking exceptions.
- Production release uses the shared release lock, encrypted backup and isolated restore, exact
  Core/Web revisions, health checks, audit/idempotency checks, and rollback proof.

## 2026-09-28 implementation checkpoint

- Core payroll schema, immutable-lock rules, read-only masked workbench route, legacy workbook
  adapter, bank `.xls` export, and reconciliation workbook preflight are under development.
- The 2026-08 real payroll sample was compared read-only against the five normal bank files,
  the independent supplemental file, and the fixed `其他` file. Every payee/account/amount row
  matched after the 1,000-yuan partial-cash split was sourced from the legacy cash sign-off file.
  No source document or production database was modified.
- Focused tests: 39 passed. A live PostgreSQL migration/restore test, write/upload/lock/export
  API, Web workbench, full historical reconciliation import, and production deployment remain
  incomplete. Do not retire the old service or import formal data yet.

## 2026-09-28 PostgreSQL 15 isolated probe

- On VM103, a temporary `postgres:15-alpine` container was run with no network, no exposed ports,
  and memory-backed data storage. It used synthetic prerequisite roles and references; it did not
  connect to or modify the production database or encrypted volume. The 0051 upgrade SQL applied
  successfully.
- `tests/sql/payroll_migration_contract.psql` passed: a synthetic version locks, the database
  rejects locked-line and payee-account changes, the API role cannot read raw payroll accounts,
  the masked view is readable, and a locked export receipt inserts. The probe rolled back its
  synthetic transaction. The temporary container and transfer files were removed.
- This is a migration-level probe, not an end-to-end release gate. A full Alembic chain,
  database-backed service test, encrypted backup/isolated restore, complete browser write path,
  and production verification remain required before release.

## 2026-09-29 full-workbook inventory

- Added a read-only, source-digested inventory over every monthly sheet. It treats any failed
  period or duplicate month as a block on importing the entire workbook; it does not import a
  subset as though the migration were complete.
- On the current hotel reconciliation workbook, 42 monthly sheets were found. Only 2026-08
  passed the existing detailed parser; the other 41 use historical layouts that the parser does
  not yet recognize. This is a code/contract coverage gap, not evidence that those months are
  financially wrong. No workbook or production database was changed.
- The replacement remains unfit for formal data migration or release. Historical sheet layouts,
  row-level source identity, reconciliation mapping, and the payroll write/calculation workflow
  must be completed and verified before the high-risk release gate can start.

## 2026-09-29 revised historical-data contract (D-043)

- The user explicitly changed the historical migration scope: save original workbook data for
  website lookup; do not require every old period to be recalculated or re-matched before import.
  This supersedes the all-period interpreted preflight gate for the archival path only. Existing
  normalized reconciliation remains a separate, verified view.
- Migration 0052 adds append-only source bytes and per-month typed cell snapshots. The adapter
  stores formula text and saved cached result separately and rejects duplicate periods. Reimport
  with the same digest is idempotent; the importer verifies a caller-provided source digest and
  commits all months in one transaction. The Core API sees only the month view, never source bytes.
- Website reads require the dedicated `reconciliation:read` identity to cover every
  entity-and-business-unit pair in the archived workbook's declared scope. The BFF uses its dedicated reconciliation client and
  the page labels historical values as original data, not new reconciled results.
- Current source read-only extraction: 42 monthly sheets, 6,414 nonempty cells, 191,073 original
  bytes. The isolated PostgreSQL 15 ACL/immutability probe for 0052 passed and was removed. This
  is not a production import or full Alembic upgrade/restore rehearsal. Payroll calculations and
  the write/lock/download path remain incomplete; do not claim full D-042 completion.

## 2026-09-29 source replacement and release hold (D-044)

- The user identified Claude's local LedgerBridge ledger as the new finance data source and
  authorized a fresh VM103 database on ordinary unencrypted storage. The old LUKS volume is no
  longer a dependency; it is **not yet deleted**, and no production database has been changed.
  The local encrypted backup from 2026-09-13 has a matching SHA-256, a prior passed isolated
  restore report, and a usable local GPG secret key. Live restore on VM103 is still pending.
- The local-mode Core branch has distinct migrations `20260906_0051` and `20260913_0052`, which
  collide numerically with this payroll branch's draft `0051` and archive draft `0052`. An
  integration owner must rebase/renumber the schema chain before any deployment.
- Independent review found that the first archive draft checked entity grants but ignored
  business-unit grants. It now records scoped pairs, checks referenced units at import, and
  fail-closes website reads without every pair. Concurrent reimport uses conflict-and-readback.
  These repairs require fresh PostgreSQL and restore tests.
- The 0052 archive revision is deliberately **excluded** from backup_restore's release allowlist
  until its source/sheet rows, privileges and immutability triggers are included in a verified
  restore inventory. Do not deploy or import it in production before that gate is complete.
