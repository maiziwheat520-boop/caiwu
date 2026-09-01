# Account registry intake

This operator path registers one authoritative Accounting Owner, one active
Business Unit, one encrypted admission Evidence Object, and one Managed Account.
It is intentionally a small interface: every identity and owner mapping is
explicit in a private plan, while encryption, exact-idempotence, audit binding,
transactionality, and failure cleanup remain inside the module.

The plan is private runtime data. Copy
`docs/templates/account-registry-intake-plan.example.json` outside the repository,
replace every example value from statement-internal evidence, restrict the file to
the operator on POSIX (`0600`), and never commit it. The source, key file,
artifact root, plan, and receipt paths must be absolute.

## Contract

- One plan covers exactly one Entity, one Business Unit, one local source file,
  and one Managed Account. Use a separate plan for each account.
- Entity identity is exact by explicit UUID, entity type, and name. A semantic
  match with another UUID is a conflict, not an inferred merge.
- Business Unit identity is exact by explicit UUID, owner UUID, ref, label, and
  active state.
- Evidence replay is exact by `evidence_ref`. The same plaintext digest may be
  admitted under a different, explicitly reviewed owner using a new
  `evidence_ref`; this is required to repair historical owner misclassification.
- The source is checked before encryption and the encrypted artifact is opened
  and authenticated before any database commit.
- The Account Registry command runs in the same outer transaction as Entity,
  Business Unit, Evidence, and encrypted-blob rows.
- `initial_lifecycle=CLOSED` does not update or bypass append-only facts. The
  registry creates revision 1 `ACTIVE`, then the intake appends an audit-bound
  revision 2 `CLOSED` in the same transaction.
- A replay must match the plan digest, registry operation request, Evidence
  identity, encrypted blob, and initial lifecycle prefix. Later valid lifecycle
  facts are preserved. Conflicts fail closed.
- Any exception or rollback aborts a newly staged encrypted artifact.
- The runner identity and account-registry capability are fixed in code; a
  private plan cannot grant itself a workload identity or capability.

For multiple accounts owned by one Entity, run plans serially and increment
`expected_registry_revision` from the prior successful receipt. Do not preflight
two plans for the same owner at the same expected revision.

## Production rollback preflight

First take the normal encrypted database backup and complete its restore rehearsal.
The preflight then executes the complete path against the real production database
inside one rollback-only transaction, performs a second exact replay, verifies zero
target-inventory and audit delta, forces all deferred constraints immediate, rolls
back, aborts any newly staged artifact, and writes a private receipt bound to the
canonical SHA-256 digest of the exact plan.

Run from the deployed release root. The command reads the fixed
`DEPLOYED_REVISION` file there; revision identity is not accepted from an
environment variable or private plan alone.

```powershell
$env:LEDGERBRIDGE_ENV = 'production'
$env:LEDGERBRIDGE_ACCOUNT_INTAKE_DATABASE_TARGET = 'production-rollback-only'
$env:LEDGERBRIDGE_ACCOUNT_INTAKE_DATABASE_URL = '<production owner database URL>'
$env:LEDGERBRIDGE_ACCOUNT_INTAKE_PRIVATE_PLAN = '<absolute private plan path>'
$env:LEDGERBRIDGE_ACCOUNT_INTAKE_PREFLIGHT_RECEIPT = '<new absolute receipt path>'
.\.venv\Scripts\python.exe scripts\run_account_registry_intake.py --preflight-only
```

Success prints only a bounded status line. It never prints owner names, account
aliases, source paths, database URLs, or evidence values.

## Production execution

Production requires all three independent gates: production environment,
production database target, and the explicit reviewed-execution token. It also
requires the preflight receipt for the unchanged plan and exact deployed
revision.

Both modes additionally assert in one database query that `current_user` and
`session_user` are `ledgerbridge`, the database is `ledgerbridge`, the schema is
`20260831_0026`, and the transaction is writable. Any mismatch fails before the
first write.

```powershell
$env:LEDGERBRIDGE_ENV = 'production'
$env:LEDGERBRIDGE_ACCOUNT_INTAKE_DATABASE_TARGET = 'production'
$env:LEDGERBRIDGE_ACCOUNT_INTAKE_DATABASE_URL = '<production database URL>'
$env:LEDGERBRIDGE_ACCOUNT_INTAKE_PRODUCTION_EXECUTION = 'execute-reviewed-account-intake-v1'
.\.venv\Scripts\python.exe scripts\run_account_registry_intake.py --execute-production
```

After each successful plan, verify the returned registry revision before
preparing the next account for the same owner. Keep the plan and receipt with the
private operational evidence package; neither belongs in Git.
