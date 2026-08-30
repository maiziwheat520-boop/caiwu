# Accounting Owner and Managed Account registry

Status: Core implementation ready for central 0022 integration and PostgreSQL replay
Branch: `ai/chatgpt/account-owner-registry`
Migration: `20260830_0023`, final predecessor `20260830_0022`

## Outcome

The Shared Financial Foundation has one explicit, owner-scoped registry contract. `Entity.id`
is the authoritative Accounting Owner identity and `Entity.entity_type` is the authoritative
PERSON/COMPANY kind. The legacy `managed_account.owner_ref` and `owner_kind` columns remain only
as migration-compatible derived fields: 0023 writes them from `Entity` and database validation
prevents them from disagreeing with it. They are not accepted from statement imports or exposed
as independent registry facts.

No Person, company, account, statement, or private sample is created by this branch. All tests
use synthetic UUIDs, labels, aliases, and facts.

## Public seams

### Controlled write

`AccountRegistryOperator.apply(plan, principal, session=None)` is the only application write
seam. A plan contains an operation UUID, owner Entity UUID, expected Entity kind, expected owner
registry revision, actor/reason, and one or more explicit actions:

- register a Managed Account with its UUID, admission Evidence UUID, account key, institution,
  suffix, account kind, and aliases;
- assign an account to a business-unit UUID over a half-open `[effective_from, effective_to)`
  date range; or
- append a new allocation revision for one bank-statement fact, with allocation items totaling
  10,000 basis points.

The principal requires both `account-registry:write` and an `EntityGrant` whose
`allow_account_registry` is true for the exact owner. The database function is executable only
by `ledgerbridge_api`; runtime roles have no direct registry-table access.

The default call owns and commits its Session. Passing an external Session performs no commit
and does not close the Session, enabling one transaction containing:

`Evidence Object → AccountRegistryOperator.apply → BankStatementImportService.import_statement`

The Evidence Object may be newly inserted earlier in that transaction and used as the Managed
Account's `admission_evidence_ref`. Any failure leaves rollback to the transaction owner.

### Versioned read

`AccountRegistryReader.get_owner_registry(...)` returns
`ledgerbridge.account-registry.v1`, bound to an exact audit sequence/hash. It returns the owner
kind from `Entity`, registry revision, Managed Accounts, masked aliases, effective-dated account
assignments, and the latest fact-allocation revision visible at that horizon. The reader requires
`account-registry:read` plus the exact owner grant. The database function is executable only by
`ledgerbridge_reader`.

Accounts without a business-unit assignment remain visible at owner scope. This is the expected
shape for a company account serving multiple stores.

## Business-unit snapshots and reporting precedence

Every account assignment and allocation item stores immutable
`business_unit_ref_snapshot` / `business_unit_label_snapshot` values. The operator plan supplies
the business-unit UUID and both snapshots; 0023 checks all three against the current directory
at write time. The projection returns the stored snapshots, so consumers must not join the live
directory to reconstruct historical meaning.

Company Reporting has frozen this precedence:

1. fact allocation;
2. transaction-date account assignment;
3. pending/unassigned.

Registry v1 preserves multi-item allocations. Company Reporting v1 accepts a single
10,000-basis-point item and treats a multi-item set as pending until split reporting is added.

## MyBank import integration

`BankStatementImportContext` now contains only `owner_entity_ref`, `managed_account_ref`,
`evidence_ref`, actor, and reason. The request contains the statement-proven institution/suffix
only for equality checking. It contains no owner ref/kind, account kind, or account key.

0023 keeps the 0021 import implementation as an ungranted internal function and replaces its
public command name with a security-definer wrapper. The wrapper rejects legacy identity fields,
requires the exact pre-registered owner/account pair, checks the registered institution/suffix
and ACTIVE lifecycle, then invokes the existing append-only statement implementation. The
hidden 0021 function has no runtime executor.

For the current MYbank importer, the registered account key retains the 0021 convention
`mybank:<personal|company>:<suffix>`; it is supplied by the explicit operator plan and is never
inferred from names.

## Idempotency and row deltas

The canonical JSON plan is SHA-256 bound to `operation_id`. Exact replay returns the stored
result with `created=false` and adds no rows or audit events. Reusing an operation ID with a
different plan fails closed. Owner-level advisory locking and expected registry revision prevent
concurrent lost updates.

One new account with one alias adds:

- one `managed_account` row;
- one revision-1 ACTIVE `managed_account_lifecycle` row;
- one `managed_account_alias` row;
- one `account_registry_operation` row; and
- four audit events: account registration, lifecycle, alias registration, and plan application.

Each optional account assignment adds one assignment row and one audit event. A fact allocation
adds one allocation-set row, one or more allocation-item rows, and one audit event.

## Database invariants

- Upgrade refuses to infer owners for any pre-existing Managed Account; the known production
  registry is empty.
- Admission Evidence must belong to the same Entity as the account.
- Alias normalization removes whitespace and hyphens and is globally unique per institution and
  alias kind, preventing the same account alias from being registered to two owners.
- Assignment owner/account/business-unit scope is enforced with composite foreign keys; date
  ranges for one account may not overlap.
- Fact allocation owner/account/fact scope is enforced with foreign keys; allocation revisions
  are append-only and every set totals exactly 10,000 basis points at commit.
- Registry facts and operations are append-only and audit-bound.
- Backup/restore metadata covers all new tables, functions, triggers, constraints, ACLs, and
  effective runtime privileges.
- Development downgrade is allowed only while both the registry operation table and Managed
  Account table are empty. Production downgrade is rejected.

## Verification state

The full local test suite passes **772 tests** with **199 skips**. Focused account-registry,
migration-source, statement-import, reconciliation, internal-read, and backup/restore suites
pass. Changed-file Ruff formatting/lint, mypy, sensitive-path, and diff checks pass.

The repository-wide Ruff format check still identifies eight unchanged files, and the
repository-wide mypy check still identifies 70 existing errors in six unchanged files. Those
baseline files are outside this branch's scope and were not reformatted or repaired here.

PostgreSQL upgrade/replay tests are present but cannot run on this branch until the centrally
owned `20260830_0022` revision and a PostgreSQL migration URL are available; 0023 deliberately
retains its final `down_revision` instead of creating a competing 0022 placeholder.

## Consumer handoff

- Personal Finance selects an owner projection whose `owner_kind` is `PERSON`.
- Company Reporting selects a `COMPANY` projection and applies the frozen attribution precedence
  using immutable snapshots.
- Original reconciliation uses `ManagedAccount.owner_entity_ref` UUID equality to distinguish
  Internal Transfer from Related-Party Transfer; the former free-text owner ref/kind pair is
  removed from the in-memory contract.
- Import runners must select an explicit owner Entity UUID, Managed Account UUID, admission
  Evidence UUID, institution, suffix, account kind, aliases, and optional business-unit snapshot.
  They must not use name matching to select any of them.
