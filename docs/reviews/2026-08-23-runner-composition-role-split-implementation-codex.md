# LedgerBridge runner composition and API/worker role-split implementation

Date: 2026-08-23  
Branch: `ai/chatgpt/phase-3-connector-runner`  
Scope: implement the fail-closed runner composition root and the database-role
boundary needed by the worker-owned async dispatch profile.

## Outcome

The branch now has an explicit `VerifiedRunnerManifest` value object and a
worker-only composition function. The composition function accepts only an
already verified, immutable manifest supplied by a future signature verifier;
it reads no files, keys, provider configuration, or dynamic import paths. With
the default `None` manifest, the worker registry is empty. With an injected
manifest, each allowlisted runner entry becomes a `RunnerConnector` facade over
the worker-owned Unix socket. The API process still has no socket mount.

Migration `20260823_0006_runtime_role_split.py` adds separate
`ledgerbridge_api` and `ledgerbridge_worker` grants. API can enqueue dispatch
rows but cannot update dispatch state; worker can update the bounded dispatch
lease/result columns but cannot insert dispatch rows. Both roles can read the
shared evidence/registry projections needed by their profile, have no database
TEMPORARY privilege, and remain non-owner runtime logins. The existing
`ledgerbridge_app` compatibility role was not removed or widened.

The migration service is explicitly marked `LEDGERBRIDGE_ENV=test` because it
is an owner-only one-shot tool. API and worker use their own role URLs in
production; outside production the settings methods retain the shared URL
fallback for local/test composition.

## Files

- `src/ledgerbridge/runner_composition.py`: canonical manifest identity,
  allowlisted runner spec, and worker facade factory.
- `src/ledgerbridge/worker.py`: worker URL resolution, manifest injection,
  worker-owned runner construction, and manifest identity handoff to dispatch.
- `src/ledgerbridge/config.py`, `src/ledgerbridge/db.py`, `src/ledgerbridge/main.py`:
  explicit API/worker database URLs with production fail-closed validation.
- `alembic/versions/20260823_0006_runtime_role_split.py`:
  exact API/worker table, column, type, schema, and audit-function grants.
- `docker/postgres-init-runtime-role.sh`, `docker-compose.yml`, `.env.example`,
  and CI bootstrap: disposable/test role provisioning and service wiring.
- `tests/test_runner_composition.py`, `tests/test_config.py`,
  `tests/test_phase2_runtime_boundary.py`, and `tests/test_worker.py`:
  identity, digest, duplicate, role URL, composition, and socket-boundary tests.

The follow-up hardening commit `99f8d05` validates manifest connector text and
canonical source fields at the composition boundary, rejects mutable digest or
connector containers, and covers invalid generations, multiple facade sharing,
worker socket-path handoff, and production async fail-closed behavior. The
worker test additions are in `b453874`.

## Verification

- Windows: `uv lock --offline`, Ruff format/check, strict mypy, and the final
  full regression passed: **230 passed / 136 skipped / 1 warning**.
- Hermes disposable PostgreSQL 15: migration `0001→0006` succeeded, and an
  explicit `base→head` downgrade/upgrade round-trip succeeded.
- Hermes grant probes after the round-trip reported:
  `api_insert_dispatch=true`, `api_update_state=false`,
  `worker_insert_dispatch=false`, `worker_update_state=true`,
  `api_temp=false`, and `worker_temp=false`.
- `SET ROLE ledgerbridge_api` and `SET ROLE ledgerbridge_worker` both failed to
  create a temporary table, as required. Compose configuration validation passed.
- A fresh Linux/PostgreSQL replay of the exact CI pytest command passed **353
  tests**, with **95.44%** coverage. The replay also caught and confirmed the
  fix for the pre-existing downgrade-guard assertion, which now expects the
  transaction to remain at migration `20260823_0006` after a blocked downgrade.
- Bandit initially reported two B608 findings on the migration's controlled
  role interpolation. The final migration uses fixed deployment-contract role
  literals, so the full Bandit scan reports **no issues identified**. Ruff,
  mypy, and Windows pytest remained green after this hardening commit.
- GitHub PR #18 head `823b7de` passed both push run `32648157898` and pull
  request run `32648154595`; each run was green for `secrets`, `quality`, and
  `compose`.
- After the boundary-test hardening, PR #18 head `b453874` passed push run
  `32648931938` and pull request run `32648934569`; both again passed
  `secrets`, `quality`, and `compose`.
- Hermes production was read-only checked after cleanup: gateway active,
  `/health` returned `{"status":"ok","platform":"hermes-agent","version":"0.20.0"}`;
  API, worker, and PostgreSQL remained healthy on revision
  `e426b488b2abb02f10ef02a61aae7ebe24c3283f` and migration `20260822_0004`.

## Deliberate remaining gates

This is composition scaffolding, not real Connector enablement. Signature/key
verification, manifest loading/custody, provider/source ownership, a real
Connector, production role-password rollout, feature-flag enablement, merge,
and deployment remain separate approvals. No production database, socket,
manifest, signing material, Connector, or evidence was created.
