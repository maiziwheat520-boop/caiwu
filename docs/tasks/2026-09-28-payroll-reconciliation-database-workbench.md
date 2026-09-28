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
