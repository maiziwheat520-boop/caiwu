# Sol Max audit remediation

Date: 2026-08-24  
Branch: `ai/chatgpt/phase-3-connector-runner`  
Scope: remediate the independent Sol Max audit findings before any merge or deployment.

## Findings and fixes

### HIGH — runtime role credential crossover

The audit found that API and worker inherited the compatibility database URL and
their passwords could fall back to the shared `ledgerbridge_app` password.

Implemented controls:

- Compose no longer injects `LEDGERBRIDGE_DATABASE_URL` into API or worker.
- API and worker URLs and passwords are mandatory in Compose.
- Bootstrap rejects equal app/API/worker passwords.
- Production settings require URL usernames `ledgerbridge_api` and
  `ledgerbridge_worker`, and require distinct URLs.
- Migration `20260823_0007` retires `ledgerbridge_app` with `NOLOGIN` and revokes
  its table, sequence, function, type, and schema privileges in production.
- The compatibility URL remains only for local/test tooling.

### MEDIUM — dispatch acceptance was not semantically bound

The audit found that API could directly insert a PENDING dispatch row and point
it at a stale or unrelated audit event.

Implemented controls:

- Migration `20260823_0008` adds a `SECURITY DEFINER` enqueue function with a
  fixed `pg_catalog` search path.
- The function creates the exact `import.dispatch.accepted` audit payload and
  dispatch row in one transaction.
- A BEFORE INSERT trigger verifies the action, exact payload, and that the audit
  event `xmin` matches the current transaction.
- Direct API INSERT is revoked; API receives only EXECUTE on the enqueue
  function. Compatibility-role direct inserts are covered by a negative trigger
  test in non-production CI.
- `DispatchService` uses the enqueue function for both normal and published
  artifact paths.

## Verification

- Windows: full pytest `231 passed / 138 skipped / 1 warning`; Ruff format/check,
  strict mypy, and Bandit all pass.
- Alembic static SQL generation succeeds for both non-production and production
  paths.
- Hermes disposable PostgreSQL 15 replay succeeds through migrations `0001` to
  `0008`. Production replay confirms `ledgerbridge_app` is `NOLOGIN`, API and
  worker remain login roles, the API enqueue function succeeds, and direct API
  dispatch INSERT is denied.
- Production Hermes was not modified; current deployed revision remains
  `e426b488b2abb02f10ef02a61aae7ebe24c3283f` with migration `20260822_0004`.

## Remaining gate

This branch is ready for a fresh independent review and Hosted CI. Merge,
production migration, password rollout, feature-flag enablement, Connector
registration, and real evidence ingestion remain separately authorized actions.
