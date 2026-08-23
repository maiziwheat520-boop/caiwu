# Worker-Async Dispatch Foundation — Codex Implementation Evidence

Date: 2026-08-23
Implementation branch: `ai/chatgpt/phase-3-connector-runner`
Implementation head: `d47c6f5`
Protected review: PR #18 (review-only; not merged)

## Scope

This report covers the user-authorized schema/grants, dispatch service, internal
async operation/status contract and worker claim-loop composition. It does not
authorize or claim a real Connector, a signed production manifest, a
production database-role split, a merge, or a deployment.

## Delivered

- `ImportDispatch` and `DispatchState` in `src/ledgerbridge/models/evidence.py`.
- Alembic migration `20260823_0005_async_dispatch.py` with the dispatch table,
  composite foreign keys, identity and lifecycle constraints, indexes,
  non-destructive downgrade guard, trigger transition enforcement, pinned
  `search_path=pg_catalog`, and compatibility-role column grants.
- `src/ledgerbridge/dispatch.py` with audited idempotent enqueue,
  principal-scoped status, SKIP-LOCKED claim, lease renewal, expiry recovery,
  bounded retry, and terminal success/failure handling.
- Feature-flagged `POST /v1/evidence/import-requests` and principal-scoped
  `GET` status route in `src/ledgerbridge/main.py`; the API publishes and binds
  the artifact before returning `202` and never invokes the importer in this
  profile.
- Worker claim/lease/retry/terminalization composition in `src/ledgerbridge/worker.py`.
  It is guarded by the internal flag, a non-production environment and a
  verified manifest/Connector registry; both defaults are empty, so no real
  import can execute until separately reviewed wiring is supplied.
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
3. Runtime replay showed that the artifact audit trigger requires the audit
   event and `raw_artifact` insert in one top-level transaction. Published
   enqueue now retries the whole transaction on a content-hash race, while the
   dispatch claim query locks only `evidence_import_dispatch` and reads the
   artifact with a plain SELECT under the compatibility role.

Both corrections have regression coverage in the branch history.

## Evidence

Windows local validation:

- `uv lock --offline` passed.
- Ruff format/check and strict mypy passed for changed source/tests.
- Full local pytest: **212 passed, 136 skipped, 1 warning**.
- Sensitive-path scan and `git diff --check` passed.

Disposable Hermes Linux/PostgreSQL validation:

- Exact CI coverage command: **348 passed, 7 warnings**, total coverage
  **95.26%** (the unchanged `--cov-fail-under=95` gate).
- The complete replay also passed the migration downgrade-to-base and
  upgrade-to-head round-trip; the final head was `20260823_0005`.
- Runtime TEMP creation and public shadow-table creation were rejected.
- Dispatch trigger configuration reported `search_path=pg_catalog`.
- Runtime table/column grants matched the intended compatibility boundary.
- Five-way dispatch attack/recovery probes, including claim races and
  exhaustion, passed.
- The test used a unique Compose project and disposable database/volumes; all
  temporary resources were removed after the final production health/tag check.

Production Hermes was checked after cleanup and remained healthy on
`e426b488b2abb02f10ef02a61aae7ebe24c3283f` with migration `20260822_0004`.
No production dispatch row, route, Connector, signing material, or evidence
bytes were created.

## Remaining gates

The following are deliberately not part of this implementation slice:

- separate API/worker runtime database roles and deploy-time secret injection;
- signed manifest generation and real Connector registration;
- production runner composition, readiness/drain rollout and real Connector
  execution;
- protected-PR review/merge, production migration, or production enablement.

The next review can audit the schema, endpoint and worker composition at the
fixed head above before any manifest, role split or production wiring is
authorized.
