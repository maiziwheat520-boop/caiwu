# Worker-Async Dispatch Foundation — Codex Implementation Evidence

Date: 2026-08-23  
Implementation branch: `ai/chatgpt/phase-3-connector-runner`  
Implementation head: `5fbb5fb`  
Protected review: PR #18 (review-only; not merged)

## Scope

This report covers only the user-authorized schema/grants and dispatch-service
slice of the worker-owned asynchronous import plan. It does not authorize or
claim an async HTTP endpoint, a production worker loop, a real Connector, a
signed production manifest, a database-role split, a merge, or a deployment.

## Delivered

- `ImportDispatch` and `DispatchState` in `src/ledgerbridge/models/evidence.py`.
- Alembic migration `20260823_0005_async_dispatch.py` with the dispatch table,
  composite foreign keys, identity and lifecycle constraints, indexes,
  non-destructive downgrade guard, trigger transition enforcement, pinned
  `search_path=pg_catalog`, and compatibility-role column grants.
- `src/ledgerbridge/dispatch.py` with audited idempotent enqueue,
  principal-scoped status, SKIP-LOCKED claim, lease renewal, expiry recovery,
  bounded retry, and terminal success/failure handling.
- PostgreSQL-backed tests for idempotency/digest conflicts, concurrent enqueue
  convergence, claim races, lease renewal and expiry, retry/exhaustion,
  terminal immutability, validation, permissions, and failed outcome mapping.

The implementation intentionally maps an importer `NEEDS_REVIEW` result to a
completed dispatch with a review result, while an importer `FAILED` result
terminalizes the dispatch as `FAILED`. This keeps execution completion distinct
from business import success without leaving a durable dispatch in the wrong
state.

## Corrections found during validation

The disposable Hermes replay caught two behavior issues before this report was
written:

1. The transition trigger initially rejected `RUNNING -> RUNNING`, preventing
   legitimate lease renewal. The migration now allows that self-transition
   while retaining owner/deadline checks.
2. `DispatchService.complete(result_status=FAILED)` initially left the dispatch
   in `SUCCEEDED`. The service now persists `FAILED`; `NEEDS_REVIEW` remains a
   successful execution with an explicit review projection.

Both corrections have regression coverage in the branch history.

## Evidence

Windows local validation:

- `uv lock --offline` passed.
- Ruff format/check and strict mypy passed for changed source/tests.
- Full local pytest: **183 passed, 128 skipped**.
- Sensitive-path scan and `git diff --check` passed.

Disposable Hermes Linux/PostgreSQL validation:

- Full suite: **313 passed**.
- Coverage: **95.03%**, meeting the unchanged 95% gate.
- Alembic downgrade-to-base and upgrade-to-head round-trip passed; final head
  was `20260823_0005`.
- Runtime TEMP creation and public shadow-table creation were rejected.
- Dispatch trigger configuration reported `search_path=pg_catalog`.
- Runtime table/column grants matched the intended compatibility boundary.
- Five-way dispatch attack/recovery probes, including claim races and
  exhaustion, passed.
- All temporary Compose projects, volumes, images, and test directories were
  removed after validation.

Production Hermes was checked after cleanup and remained healthy on
`e426b488b2abb02f10ef02a61aae7ebe24c3283f` with migration `20260822_0004`.
No production dispatch row, route, Connector, signing material, or evidence
bytes were created.

## Remaining gates

The following are deliberately not part of this implementation slice:

- async `POST`/`GET` operation endpoints and `202` response contract;
- worker claim loop, runner composition, graceful drain, and readiness;
- separate API/worker runtime database roles and deploy-time secret injection;
- signed manifest generation and real Connector registration;
- protected-PR review/merge, production migration, or production enablement.

The next review can audit the schema/service at the fixed head above before any
endpoint or worker wiring is authorized.
