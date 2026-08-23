# Implementation Plan: worker-owned asynchronous Connector dispatch

## Selected Baseline And Scope

This plan implements the accepted composition baseline from
[Connector execution composition](../proposals/connector-execution-composition.md):
production runner work is owned by the worker, the API does not mount
`connector-socket`, and accepted work is represented by a durable PostgreSQL
dispatch record. The existing synchronous `/v1/evidence/imports` contract stays
an explicit internal/test profile; the async profile uses a separate operation
contract so clients do not mistake `202 Accepted` for a completed import.

This is an implementation plan, not source or migration work. It does not
register a real Connector, create a signed production manifest, enable the
route, deploy Hermes, or import evidence. Signing/key custody, authentication
provider ownership and source-system ownership remain separate gates.

## Source Revision And Evidence

The plan is based on the reviewed branch content at the design decision head
`cdecbdbb429e16b0c18bb835e6afda1c9e0742cf`, with the following relevant source
facts:

- `EvidenceImporter.ingest_published()` continues synchronously through
  detection, runner calls, source-record publication and terminal `ImportJob`.
- `ImportJobStatus` has `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED` and
  `NEEDS_REVIEW`; the `(artifact, connector, version)` identity is not known
  before detection.
- `worker.py` writes a heartbeat but has no claim loop or Connector registry.
- `docker-compose.yml` mounts `connector-socket` into worker and runner only;
  API has a read-only artifact mount and no socket.
- `ArtifactStore` publishes verified content before the importer binds a
  `RawArtifact` row, so an async enqueue needs an explicit publish/bind/
  dispatch transaction and orphan reconciliation rule.
- The runtime database role is currently `ledgerbridge_app` for both API and
  worker. The existing migrations grant it bounded evidence reads/inserts and
  selected `import_job` update columns; they revoke TEMPORARY and use database
  triggers for state and audit invariants.

The source collection used by the parent hardening review remains bound by
SHA-256 `79175486a1716efbf17828e49efa809caeae5b74ba0b48d2ca0f2f80e6ee0149`.
The additional model/grant observations above are direct implementation-plan
checks and are not presented as a new evidence collection digest.

## Non-Negotiable Invariants

- API authentication and `evidence:write` authorization complete before body
  read, staging or database enqueue.
- No request field selects Connector name, version, factory or `source_system`.
- API returns `202` only after verified artifact publication, `RawArtifact`
  binding, acceptance audit event and dispatch row commit in one database
  transaction.
- A process crash cannot lose an accepted dispatch. A published file with no
  committed `RawArtifact`/dispatch is recoverable by reconciliation and is
  never reported as accepted.
- Only the worker claims dispatch rows and reaches the runner socket in the
  production profile. API has no socket volume and never constructs a
  production `RunnerConnector`.
- Claim ownership is atomic, lease-bounded and recoverable. A stale worker
  cannot overwrite a newer claim or terminal outcome.
- `ImportJob` remains the Connector-specific, audited result. The dispatch row
  is orchestration state and cannot be used to bypass `ImportJob` triggers,
  source provenance foreign keys or terminal audit binding.
- Retryable infrastructure failures are bounded and observable. Deterministic
  contract, provenance, digest and identity failures do not retry indefinitely.
- Manifest generation and digest are captured at enqueue and revalidated by
  worker before execution. Mixed or missing generations fail closed.
- Dispatch and status responses expose only bounded IDs, states and stable
  error codes. They never expose evidence bytes, parser output, signatures,
  key material, credentials or unbounded exception text.
- New SECURITY DEFINER/trigger SQL follows the Phase 2 hardening rule:
  `SET search_path = pg_catalog`, fully qualify application tables/functions
  with `public.`, and use immutable, bounded values.

## Contract Decision

### Async operation endpoint

Add a separately named internal operation endpoint rather than changing the
meaning of the existing synchronous route:

```text
POST /v1/evidence/import-requests
GET  /v1/evidence/import-requests/{operation_id}
```

The endpoint remains feature-flagged and production-forced-off until the
authentication provider, signed manifest and dispatch implementation have
separate approval. The synchronous `/v1/evidence/imports` route remains
available only to internal/test dependency overrides and continues returning
its current bounded `ImportOutcome` projection.

### POST response

On successful durable enqueue, return `202 Accepted`, a `Location` header for
the status resource and a bounded body:

```json
{
  "operation_id": "<uuid>",
  "artifact_id": "<uuid>",
  "status": "PENDING"
}
```

The response does not include `job_id`, parsed counts or a guessed Connector;
those values do not exist until worker detection. Stable admission errors keep
the existing bounded mapping (`400`, `401`, `404`, `413`, `422`, `503`, `507`)
where applicable. A database/commit failure returns an error and does not
claim that the operation was accepted.

### GET response

The status projection contains:

```json
{
  "operation_id": "<uuid>",
  "artifact_id": "<uuid>",
  "status": "PENDING|RUNNING|RETRY_WAIT|SUCCEEDED|FAILED",
  "job_id": "<uuid>|null",
  "result_status": "SUCCEEDED|FAILED|NEEDS_REVIEW|null",
  "error_code": "<bounded-code>|null"
}
```

Here dispatch `SUCCEEDED` means the worker completed its durable execution;
`result_status` is the existing `ImportJobStatus` and can therefore be
`NEEDS_REVIEW` without being mislabeled as a successful financial import.

The caller must pass the same verifier-owned principal used for admission.
The query joins the immutable acceptance audit event and compares its actor;
unknown IDs and other actors receive the same non-disclosing `404` response.
No raw `diagnostic_summary`, filename, media type or source-system detail is
returned by this endpoint.

An `Idempotency-Key` is not required for the first internal/test profile, in
line with the existing Slice C decision. The database unique key still makes
repeated identical artifact/generation/channel submissions converge. A
bounded keyed idempotency contract is mandatory before any remote or
multi-client exposure.

## Dispatch Data Model

### ORM and database table

Add an `ImportDispatch` model mapped to `public.evidence_import_dispatch`.
The table is an append-once operation record with controlled state updates:

| Column | Type/constraint | Purpose |
| --- | --- | --- |
| `id` | UUID primary key, `gen_random_uuid()` | Opaque operation identifier. |
| `artifact_id` | UUID `NOT NULL`, FK `raw_artifact.id ON DELETE RESTRICT` | Content-addressed work input. |
| `ingest_channel` | `varchar(64) NOT NULL`, FK `ingest_channel.id ON DELETE RESTRICT` | Server-validated intake channel; never a Connector selector. |
| `accepted_audit_event_id` | UUID `NOT NULL`, unique FK `audit_event.id ON DELETE RESTRICT` | Immutable actor/reason and acceptance payload binding. |
| `manifest_generation` | bounded canonical string, `NOT NULL` | Manifest generation captured at admission. |
| `manifest_digest` | `bytea NOT NULL`, exactly 32 bytes | Verified manifest identity captured at admission. |
| `state` | new enum `dispatch_state` | `PENDING`, `RUNNING`, `RETRY_WAIT`, `SUCCEEDED`, `FAILED`. |
| `attempt_count` | integer `NOT NULL DEFAULT 0`, bounded `0..16` | Number of claims, not number of socket frames. |
| `available_at` | timestamptz `NOT NULL DEFAULT CURRENT_TIMESTAMP` | Earliest claim time for pending/retry work. |
| `lease_owner` | bounded string nullable | Opaque worker instance ID while `RUNNING`. |
| `lease_until` | timestamptz nullable | Claim expiry; never used as an authorization token. |
| `created_at` | timestamptz `NOT NULL DEFAULT CURRENT_TIMESTAMP` | Operation creation time. |
| `started_at` | timestamptz nullable | First successful claim time. |
| `completed_at` | timestamptz nullable | Dispatch terminal time. |
| `import_job_id` | UUID nullable, composite FK to `import_job(id, artifact_id)` | Connector-specific result once detection creates it. |
| `error_code` | bounded uppercase string nullable | Stable dispatch/infrastructure failure code. |
| `diagnostic_summary` | bounded non-blank string nullable | Internal diagnostic only; never returned by GET. |

Use a unique constraint on `(artifact_id, ingest_channel,
manifest_generation)` so a duplicate artifact in the same generation
converges while a reviewed manifest generation can intentionally reprocess the
same bytes. On conflict, compare the stored digest with the current digest; a
digest mismatch for an existing generation is a fail-closed
manifest/configuration error, not a second operation.

The `import_job_id` foreign key must include `artifact_id`, matching the
existing composite identity. A dispatch can remain `RUNNING`/`RETRY_WAIT`
with a null `import_job_id`; once a terminal `ImportOutcome` exists, the
worker binds the ID and never clears it.

### Database state constraints and trigger

Add a `BEFORE INSERT OR UPDATE` state trigger with the following transitions:

```text
INSERT                  -> PENDING only
PENDING                 -> RUNNING or FAILED (admission/config failure)
RUNNING                 -> SUCCEEDED, FAILED, or RETRY_WAIT
RETRY_WAIT              -> RUNNING or FAILED
SUCCEEDED/FAILED        -> immutable
```

The trigger rejects identity, accepted-audit, manifest, artifact and creation
timestamp changes. It enforces:

- `PENDING`: no lease, no started/completed time, zero attempts, no error;
- `RUNNING`: non-blank owner, future lease, started time, no completed time;
- `RETRY_WAIT`: no owner/lease, attempts below the configured maximum,
  available time present and no terminal import job;
- `SUCCEEDED`: completed time and `import_job_id` present, no error;
- `FAILED`: completed time and error code present; if the failure represents
  an import outcome, `import_job_id` and its terminal audit must be present;
- no state may mutate after terminal audit/import binding.

The trigger must be schema-qualified and hardened against `pg_temp` shadowing.
The migration must test illegal transitions, lease-owner mismatch, stale claim
updates, changed manifest digest and direct runtime attempts from both API and
worker roles.

### Indexes

Create:

- a partial claim index on `(available_at, created_at, id)` for `PENDING` and
  `RETRY_WAIT` rows;
- a partial recovery index on `(lease_until, id)` for `RUNNING` rows;
- a status lookup index on `(id, accepted_audit_event_id, artifact_id)` (the
  primary key remains authoritative);
- the unique artifact/channel/generation constraint above; the digest is
  checked explicitly on conflict.

Do not index raw actor, filenames or parser payloads. The acceptance audit
event remains the immutable actor binding and is queried only for ownership
checks.

## Transaction Boundaries

### API publish/bind/enqueue

1. Authenticate and authorize before reading the request body.
2. Parse the bounded multipart stream and complete the existing ArtifactStore
   handoff. Do not call the importer or runner from the API async profile.
3. Load the verified manifest generation/digest from the shared composition
   loader; an empty/invalid production manifest fails before enqueue.
4. Begin one database transaction. Ensure the content-addressed `RawArtifact`
   row exists using the existing audit-bound `_ensure_artifact` logic or a
   transactionally equivalent service. Generate the operation UUID.
5. Append `import.dispatch.accepted` with the actor, fixed reason, operation
   ID, artifact ID, ingest channel, manifest generation and digest. Store no
   raw body, token or signature.
6. Insert `evidence_import_dispatch` as `PENDING` referencing the acceptance
   audit event. On the unique key, verify artifact/channel/generation/digest
   equality and return the existing operation; never overwrite its actor or
   state.
7. Commit. Only after commit return `202` and `Location`.

Publication is a filesystem operation before this transaction. If the
transaction fails, the API returns an error and a reconciliation task later
compares published content-addressed files with `raw_artifact` and dispatch
references. Reconciliation may delete only an unreferenced, stale file under
the existing ArtifactStore rules; it may never delete a referenced artifact.

### Worker claim

Claim one row in a short transaction using an atomic `SELECT ... FOR UPDATE
SKIP LOCKED` or equivalent `UPDATE ... FROM` statement:

```sql
-- Pseudocode; final SQL must use public-qualified identifiers.
SELECT id
FROM public.evidence_import_dispatch
WHERE state IN ('PENDING', 'RETRY_WAIT')
  AND available_at <= CURRENT_TIMESTAMP
ORDER BY available_at, created_at, id
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

Then set `state='RUNNING'`, increment `attempt_count`, set a fresh bounded
`lease_owner` and `lease_until`, and set `started_at` only on the first claim.
Commit before opening the artifact or socket. Every later update includes
`id`, `state='RUNNING'`, `lease_owner` and the expected attempt number, so a
stale worker receives zero updated rows and cannot overwrite a newer claim.

The worker renews a lease only while the importer is active and never extends
it beyond a global operation deadline. Graceful shutdown stops new claims,
allows the current bounded runner call to finish, and leaves an uncompleted
row recoverable after lease expiry.

### Worker execution and terminalization

1. Read the dispatch and acceptance audit actor/reason after claim.
2. Verify the current manifest generation/digest equals the captured values.
   On mismatch, stop and move the dispatch to bounded retry or operator
   failure according to the rollout policy; do not execute a different parser.
3. Construct the worker-owned `EvidenceImporter` with the shared verified
   runner registry and execute `ingest_published()`.
4. The importer creates or converges on the existing `ImportJob`, writes
   `SourceRecord` rows and appends the existing `import.complete` audit in its
   own transaction. No dispatch code writes source records directly.
5. In a short transaction, bind `import_job_id` and move dispatch to
   `SUCCEEDED` or terminal `FAILED`, clear the lease, set `completed_at` and
   store only a bounded error code/summary. If the terminal update races with
   a stale worker, zero-row update is success for the already-terminal row
   after re-read; it is never a reason to duplicate source records.

If the worker crashes after the importer commits but before dispatch update,
the next claim reruns the same artifact. Existing importer job uniqueness and
terminal checks return the prior outcome; reconciliation then binds the
dispatch to that job. This crash point must have a behavior-sensitive test.

## Retry And Failure Policy

The initial bounded policy is:

| Class | Examples | Dispatch action |
| --- | --- | --- |
| Retryable infrastructure | `RUNNER_UNAVAILABLE`, transient `IMPORT_DATABASE`, transient artifact read/storage I/O | `RETRY_WAIT` with bounded exponential backoff and jitter; lease cleared. |
| Deterministic runner/protocol | `RUNNER_PROTOCOL`, `STALE_RESPONSE`, `RESPONSE_LIMIT`, `RECORD_LIMIT`, digest/size mismatch | Terminal `FAILED` after the importer records the corresponding audited outcome; no blind retry. |
| Connector contract | `CONNECTOR_CONTRACT`, `PARSE_ERROR` | Terminal `FAILED`; request/manifest/connector review required. |
| Routing/review | `NO_CONNECTOR`, `AMBIGUOUS_CONNECTOR`, `PROVENANCE_CONFLICT`, `IDENTITY_CONFLICT` | Existing `ImportJob` becomes `NEEDS_REVIEW`; dispatch becomes `SUCCEEDED` and status exposes `result_status=NEEDS_REVIEW`, so execution completion is not presented as a posted/imported result. |
| Manifest/configuration | missing/invalid generation, unsigned or mismatched digest | Do not claim new work; keep route not-ready and alert. Existing claimed row returns to bounded retry or operator-held failure without executing. |

At most 5 attempts are allowed for the initial implementation. Backoff and
lease durations are configuration with strict upper bounds, not unbounded
environment strings. When attempts are exhausted, the worker must first
create a durable audited `ImportJob` terminal outcome if one does not already
exist; only then may dispatch become `FAILED`. If the database is unavailable
for that operation, keep the row recoverable and alert rather than silently
dropping accepted evidence.

## Database Roles And Grants

The current shared `ledgerbridge_app` role is sufficient for a test-only first
slice but does not distinguish API enqueue authority from worker claim
authority. Before production async enablement, split the login URLs into two
deploy-time roles with passwords supplied outside the repository:

| Capability | API role | Worker role | Migration/owner role |
| --- | --- | --- | --- |
| `raw_artifact` SELECT/INSERT and registry SELECT | yes | yes | yes |
| Dispatch SELECT by operation and actor | yes | yes | yes |
| Dispatch INSERT (enqueue) | yes | no | yes |
| Dispatch claim/lease/status UPDATE | no | yes | yes |
| `import_job`/`source_record` INSERT and importer UPDATE columns | no for async API | existing bounded worker grants | yes |
| `append_audit_event` EXECUTE | accepted/rejection events only | terminal/retry events as designed | yes |
| DDL, trigger/function ownership, grants | no | no | yes |

The migration must revoke PUBLIC and role-inherited table privileges before
granting exact columns. API and worker roles must both be `LOGIN NOSUPERUSER
NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, have database TEMPORARY
denied, and have no schema `CREATE`. Existing `ledgerbridge_app` remains as a
compatibility role only until the split is deployed; no role alias or
`SET ROLE` shortcut may let API regain worker update authority.

CI and Hermes disposable tests may use non-production fixture passwords in
environment variables, but no password or URL value is committed to this
repository or `work.md`.

## Affected Components

- `src/ledgerbridge/models/evidence.py`: add `ImportDispatch` and a separate
  `DispatchState` enum; keep `ImportJob` constraints intact.
- New dispatch service module: transactionally bind an artifact, append the
  acceptance event, enqueue, claim, lease, reconcile and expose a bounded
  status projection.
- `src/ledgerbridge/main.py`: add the async operation endpoint and status
  endpoint; do not mount a socket or call the importer in async API mode.
- `src/ledgerbridge/worker.py`: add manifest/runner composition, claim loop,
  lease renewal, retry classification, graceful drain and heartbeat/readiness.
- `src/ledgerbridge/config.py`: bounded dispatch polling, lease, backoff,
  attempt and manifest-generation settings; keep safe defaults and production
  route false.
- `alembic/versions/20260823_0005_async_dispatch.py` (name chosen at
  implementation time): enum/table/indexes, state trigger, composite FKs and
  exact grants/role migration with downgrade guard.
- `docker/postgres-init-runtime-role.sh`, `docker-compose.yml` and CI
  fixtures: separate deploy-time API/worker URLs without committed secrets;
  keep socket volume worker-only.
- `tests/test_dispatch.py`, `tests/test_upload_route.py`,
  `tests/test_evidence_import.py`, `tests/test_worker.py`, migration/grant
  integration tests and Linux Compose/Hermes replay.
- `PROJECT_STATUS.md`, Slice C task documentation and Claude audit prompt.

## Ordered Work Packages

1. **Freeze contract and enum.** Add typed request/response models, error
   mapping and the dispatch state transition table; retain the synchronous
   endpoint unchanged.
2. **Add model and migration.** Create the table, constraints, indexes,
   triggers, composite FKs and downgrade guard. Apply `SET search_path =
   pg_catalog` plus public-qualified names to every new trigger/function.
3. **Add role/grant tests before wiring code.** Prove API cannot claim or
   terminalize, worker cannot enqueue arbitrary Connector/source-system values,
   TEMP/schema/DLL privileges remain denied, and owner-only migration can
   upgrade/downgrade only when the dispatch table is empty.
4. **Implement publish/bind/enqueue.** Reuse ArtifactStore handoff and
   importer artifact identity logic without running detection; make duplicate
   artifact/generation/channel requests converge and preserve the first
   acceptance actor/audit binding.
5. **Implement claim/lease/retry worker.** Use short transactions, stale-lease
   recovery, monotonic deadlines, bounded backoff and graceful drain. The
   worker alone constructs `RunnerConnector` for production.
6. **Add status read path.** Enforce principal ownership, return bounded
   projections, avoid operation enumeration and keep terminal state immutable.
7. **Add manifest generation gate.** API captures a verified generation;
   worker and runner require the same digest and fail closed on drift.
8. **Run failure-injection and concurrency tests.** Cover every crash point,
   two workers, duplicate requests, runner restart, DB outage, stale claims,
   orphan reconciliation and terminal audit ordering.
9. **Only after all gates:** create an empty signed generation in a disposable
   profile, request Claude narrow audit, and separately seek approval for a
   synthetic Connector. No production flag or real data follows automatically.

## Test And Acceptance Matrix

### Database and state machine

- Every legal/illegal dispatch transition and invariant check is exercised
  through PostgreSQL, not only ORM mocks.
- Two concurrent claimers produce one owner; stale owner updates affect zero
  rows; lease expiry returns exactly one row to `RETRY_WAIT`/`PENDING`.
- Terminal dispatch rows, acceptance audit binding, manifest identity and
  `import_job_id` are immutable.
- API role cannot `UPDATE` dispatch claim/status columns; worker role cannot
  `INSERT` arbitrary dispatches; neither role can create TEMP/shadow objects.
- Migration upgrade→downgrade→upgrade is clean on an empty disposable DB;
  downgrade refuses when dispatch or referenced evidence exists.

### API and artifact boundary

- Auth failures occur before body read/staging.
- Incomplete multipart, quota failure, descriptor replacement and digest
  mismatch create no acceptance audit or dispatch row.
- `202` is returned only after the DB transaction commits; an injected commit
  failure returns a stable 503 and leaves an observable orphan for cleanup.
- Duplicate bytes converge on one operation for the same generation/channel;
  provenance conflicts remain reviewable.
- GET status never returns another principal's operation, raw filename, actor,
  diagnostic text or parser records.

### Worker and runner

- Worker claims only verified manifest generations and never mounts a socket in
  API Compose tests.
- Runner unavailable retries with a bounded total deadline; malformed protocol,
  hostile records, output limits and digest mismatch follow the terminal policy.
- Crash after `ImportJob` commit but before dispatch update replays to the same
  outcome and one terminal audit event; no duplicate `SourceRecord` rows.
- Worker shutdown leaves a recoverable lease; restart drains/reclaims without
  two concurrent runner calls for one operation.
- Networkless/read-only/UID/cap-drop/no-secret runner tests remain green.

### End-to-end release gate

- Windows local: Ruff, strict mypy, full pytest and sensitive-path scan.
- Hosted Linux/PostgreSQL: coverage remains at least 95%, Bandit, frozen lock,
  migration round-trip and grant probes.
- Hermes disposable Compose: unique project name, isolated volumes, empty
  signed generation, two-worker claim race, runner restart and orphan cleanup.
- No real Connector, OAuth provider, signing secret, evidence bytes or
  production deployment is used in any pre-authorization test.

## Rollout And Rollback

1. Ship the schema/model behind an unused feature flag with empty dispatch and
   route disabled. Verify role/grant and migration probes.
2. Enable the async endpoint only in a disposable/test profile with a static
   synthetic manifest and worker socket. Keep synchronous route tests intact.
3. Run crash/concurrency/runner-restart tests and a Claude narrow audit. Fix
   findings before any production manifest is considered.
4. Introduce separate API/worker runtime roles and digest/readiness gates. A
   role split is a prerequisite for production async enablement.
5. Deploy an empty signed generation only after a fresh backup and isolated
   restore rehearsal. A real Connector is a separate approval.

Rollback stops new async admissions, lets current leases expire or drains them,
selects the empty/previous manifest, and keeps both route profiles disabled.
Do not delete dispatch rows or artifacts during rollback. Reconcile rows and
files first; downgrade the migration only after dispatch, import jobs, source
records and referenced audit/artifact data are empty and the owner gate passes.

## Observability And Runbook Requirements

Emit bounded metrics and structured logs for dispatch accepted, claim latency,
attempt count, lease expiry, retry code, terminal state, manifest digest prefix,
orphan reconciliation count and worker readiness. Never log actor tokens,
signatures, raw filenames, evidence bytes or exception strings.

The runbook must document: how to pause admissions, inspect a stuck lease,
restart the worker/runner pair, verify generation agreement, reconcile orphan
files, restore from backup, and resume without duplicating `ImportJob` audit
events. Every operator action must retain the existing audit-chain discipline.

## Open Decisions Before Code

- Confirm the separate endpoint name/version and whether `NEEDS_REVIEW` maps to
  dispatch `SUCCEEDED` or a distinct public review state.
- Approve the API/worker role split and deploy-time URL/secret injection method;
  no credential values belong in the repository.
- Set measured lease duration, maximum attempts, backoff and worker drain time
  after runner throughput tests; defaults above are safety bounds, not claims.
- Assign ownership for orphan reconciliation, source-system registration,
  manifest generation signing and status endpoint authorization.
- Decide whether a dedicated broker is necessary only after PostgreSQL queue
  latency/concurrency evidence; do not add one speculatively.

## Implementation evidence (Codex, 2026-08-23)

The authorized schema/grants and service slice is implemented on
`ai/chatgpt/phase-3-connector-runner` through `5fbb5fb` (with the dispatch
foundation in `b1c1480`, lease-renewal correction in `e544d22`, fixture and
state-machine coverage in `1f92c1d`/`6968ba3`, failed-outcome mapping in
`5a4af52`, and race/exhaustion coverage in `5fbb5fb`). The implementation adds
`ImportDispatch` and `DispatchState`, migration `20260823_0005`, exact
compatibility-role grants, transition enforcement, and `DispatchService` for
audited enqueue, status, claim, lease, retry, recovery, and terminalization.

The async HTTP `202`/status endpoints, worker claim loop, runner composition,
signed manifest, separate API/worker database roles, and real Connector are not
implemented or enabled. The existing synchronous route remains an internal/test
profile. Production Hermes remains on `e426b488b2abb02f10ef02a61aae7ebe24c3283f`
and migration `20260822_0004`.

Validation completed on the disposable Hermes environment: the full Linux/
PostgreSQL suite passed 313 tests with 95.03% coverage at the unchanged 95%
threshold; migration downgrade/upgrade round-trip passed; runtime TEMP and
public-shadow creation were rejected; dispatch trigger configuration and
column-level grants matched the intended boundary; all temporary Compose
projects, volumes, images, and test directories were removed. Windows local
validation also passed 183 tests with 128 skips, strict mypy, Ruff, offline
locking, and the sensitive-path scan. No production route, connector, or
evidence bytes were touched.
