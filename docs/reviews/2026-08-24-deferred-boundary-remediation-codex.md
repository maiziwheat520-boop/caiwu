# Deferred runner and upload boundary remediation

Date: 2026-08-24  
Branch: `ai/chatgpt/phase-3-connector-runner`  
Implementation commit: `bb3eee4`

## Outcome

The four enablement-time controls left by the independent security audit are
implemented and locally verified. This closes the bounded availability and
legacy-role-drift gaps at the foundation layer; it does not authorize enabling
the internal routes, registering a real Connector, merging the branch, or
deploying to Hermes production.

## Changes

- `RunnerConnector` stores the detect/parse handoff in a `ContextVar`, so a
  shared facade cannot overwrite another concurrent import's pending request.
- `ConnectorSupervisor` uses a dedicated, hard-capped executor (default four,
  maximum eight workers). A cancelled asyncio wrapper does not release its
  execution slot until the underlying synchronous Connector future completes;
  saturated requests fail closed with the bounded `TIMEOUT` error instead of
  growing the default executor queue. Artifact spool ownership remains with
  the worker until that future finishes.
- Multipart request ingestion has a configurable read deadline
  (`LEDGERBRIDGE_UPLOAD_READ_TIMEOUT_SECONDS`, default 120 seconds) and a
  loop-independent admission limit (`LEDGERBRIDGE_UPLOAD_CONCURRENCY`, default
  two). Slots are held until the temporary body closes, with idempotent release;
  timeout maps to HTTP 408 and saturation to HTTP 429.
- Migration `20260823_0006` reasserts API/worker `NOSUPERUSER`, `NOCREATEDB`,
  `NOCREATEROLE`, `NOINHERIT`, `NOREPLICATION`, and `NOBYPASSRLS`, and revokes
  inheritance of `ledgerbridge_app`. The bootstrap script applies the same
  least-privilege attributes to newly created roles.

## Verification

- Windows: `241 passed, 139 skipped, 1 warning`.
- Ruff format/check, strict mypy, Bandit, `uv lock --offline`, and
  `git diff --check` passed.
- Hermes disposable PostgreSQL 15 replay: migrations `0001` through `0008`
  completed in production mode. The resulting roles reported API/worker
  `rolinherit = false`, all runtime roles unprivileged, no API/worker role
  memberships, `TEMP = false` for compatibility and API roles, and API enqueue
  EXECUTE = true. The temporary image, container, network, archive, and
  generated passwords were removed by the replay trap.
- Hermes production was not migrated, restarted, rebuilt, or written. It
  remains at revision `e426b488b2abb02f10ef02a61aae7ebe24c3283f` and migration
  `20260822_0004`.

## Remaining gates

The branch remains review-only. A real Connector still requires a reviewed
signed manifest, key custody, trusted authentication, hostile Linux/IPC
acceptance, and explicit authorization for merge, role-password rollout,
feature enablement, migration, and production evidence import.
