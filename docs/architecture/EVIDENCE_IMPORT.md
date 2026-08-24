# Evidence and Import operations

Status: implementation contract plus feature-flagged worker-async dispatch
endpoint/worker composition through Phase 3 platform-controls Slice A; real
manifest, Connector and production enablement remain pending
Date: 2026-08-23

## Evidence identity and retention

The core computes SHA-256 while streaming every upload into a private staging
file. The caller never supplies the digest or storage path. Publication uses a
same-filesystem hard-link create operation, which is atomic and refuses to
overwrite an existing destination. The final key is always
`sha256/<2 hex>/<2 hex>/<64 hex>` and never contains the display filename.
Destination safety comes from create-if-absent `link()` semantics, rejection of
`EEXIST` mismatches, and `O_NOFOLLOW` plus `fstat()` on every verified read; no
`os.link(..., follow_symlinks=...)` destination guarantee is assumed.

An existing blob is accepted only after its size and digest are recomputed.
Symbolic links, non-regular destinations, size mismatches, digest mismatches,
oversized streams, interrupted reads, and failed synchronization all fail
closed. A database failure after publication may leave a verified unreferenced
blob. That blob is retained for a future explicit orphan scan; it is not deleted
automatically because a concurrent transaction may be adopting the same digest.

Phase 3 adds three independent capacity limits: 50 MiB per artifact, 10 GiB of
published bytes, and 512 MiB of aggregate staging bytes. A cross-process
filesystem lock serializes cleanup, measurement, and final hard-link admission.
Usage comes from the real filesystem, so verified orphan blobs and crash-left
staging files cannot be hidden by a database rollback. Exact duplicate bytes are
still accepted when the published limit is full because the existing destination
is verified and consumes no new published capacity.

Only regular `artifact-*` staging entries older than one hour are eligible for
cleanup. Fresh entries remain counted. Symlinks, devices, unknown names,
unexpected directories, unreadable entries, scan failures, and inode replacement
during measurement fail closed with `ARTIFACT_QUOTA_STATE`. Published and staging
pressure use distinct `ARTIFACT_TOTAL_QUOTA` and `ARTIFACT_STAGING_QUOTA` codes.
Each rejection appends `artifact.ingest_rejected` with a random intake UUID and
non-secret capacity fields, then emits the same machine fields as a structured
ERROR log. The log remains the fallback if the audit database is unavailable;
neither signal includes the original filename, raw content, source transaction
identity, or exception text.

`RawArtifact` metadata and `SourceRecord` rows are permanent and immutable. The
v0.1 retention policy keeps bytes forever. A future audited retention policy may
remove bytes, but it must not delete either database row or rewrite provenance.
The authorizing `artifact.ingest` event binds the source, media type, byte size,
storage key, digest, and a SHA-256 of the display filename. Hashing the filename
protects the binding without copying a potentially secret-bearing filename into
the general audit payload.

## Connector boundary

The Connector manifest declares one lowercase canonical `source_system`. The
core snapshots that value once, requires it to exist in the append-only registry,
and rejects every parsed record whose stored source differs. Acquisition identity
is separate: `RawArtifact.source` refers to an `ingest_channel`, while
`SourceRecord.source` refers to a financial `source_system`. Connector display
text never becomes a machine identity.

The supported Connector SDK surface receives immutable metadata, a bounded
prefix for detection, and a read-only object exposing only `read()`. It exposes
no host path, `fileno()`, or write method; the store verifies and rewinds the same
open descriptor that backs that object, so a pathname replacement cannot change
the bytes parsed after verification. The interface is limited to:

- `detect(metadata, bounded_prefix)` returning `MATCH`, `NO_MATCH`, or `AMBIGUOUS`;
- `parse(read_only_stream)` yielding typed `ParsedSourceRecord` values.

The core validates connector identity, parser provenance, JSON shape, record
limits, unique locators, and normalized money after values are returned. Money
uses signed 64-bit integer minor units and `CNY`; normalized floats are rejected.
JSON objects are capped at 64 nested levels and 1,000,000 serialized UTF-8 bytes.
A connector cannot choose the database transaction, artifact destination, audit
chain, or ledger posting behavior through the SDK.

This object boundary is capability minimization, not a Python security sandbox.
The in-process Connector implementation remains available only to synthetic
tests. Every future real first-party or third-party Connector must use the
out-of-process runner from Phase 3 Slice B before it may be registered. Slice A
does not add, enable, or execute any real Connector.

Exactly one deterministic match proceeds. Zero matches, multiple matches, or an
explicit ambiguous result create an observable router job in `NEEDS_REVIEW`.
Detection exceptions and connector contract violations become bounded error
codes and generic summaries; raw rows, display filenames, and exception messages
are not copied to logs or job diagnostics.
Evidence-integrity and evidence-I/O failures use distinct error codes. If durable
publication fails before a trustworthy RawArtifact can exist, the public method
raises the controlled `EvidenceIngestionError` with a bounded error code instead
of fabricating an ImportJob that cannot satisfy its artifact foreign key.
Database persistence failures that prevent a trustworthy job/audit terminal state
use the same controlled exception boundary with `IMPORT_DATABASE`.

## Job lifecycle and idempotency

The database enforces these transitions:

```text
PENDING -> RUNNING -> SUCCEEDED | FAILED | NEEDS_REVIEW
       \------------> FAILED | NEEDS_REVIEW
```

Terminal jobs are immutable. One effective attempt exists for each
`(artifact_id, connector_name, connector_version)`. A repeated import of the
same bytes and connector version returns the existing terminal outcome. A new
connector version receives a distinct job but cannot overwrite an existing
`(artifact_id, record_locator)` SourceRecord. Provenance disagreement becomes
`NEEDS_REVIEW` and publishes no partial batch.

Re-importing the same bytes with a different source or media type creates a
separate provenance review job with `PROVENANCE_CONFLICT`; it never rewrites the
first RawArtifact row. A different display filename alone is allowed because the
frozen identity rule deliberately converges duplicate bytes under different
filenames onto one artifact. A new parser version creates a distinct ImportJob,
but an existing locator remains immutable; approved re-parsing therefore needs a
human review workflow rather than automatic overwrite.

The core validates the complete batch before starting its publication
transaction. SourceRecords and the terminal success state commit together under
a row lock on the job. Unique locator races converge as duplicates; other
identity conflicts roll back the entire batch. Phase 2 never creates a
JournalEntry from imported evidence.

## Worker-owned asynchronous dispatch (implemented, feature-flagged and disabled)

The Codex branch implements the durable dispatch foundation in migration
`20260823_0005` and `src/ledgerbridge/dispatch.py`. `ImportDispatch` captures the
artifact, ingest channel, verified manifest generation/digest, acceptance audit,
bounded attempt state, lease owner/deadline, and the eventual `ImportJob`.
The unique `(artifact_id, ingest_channel, manifest_generation)` key makes
repeated admissions converge; a digest disagreement is rejected rather than
silently reusing a different manifest. The database trigger pins
`search_path=pg_catalog`, schema-qualifies business references, and enforces
the legal PENDING/RUNNING/RETRY_WAIT/SUCCEEDED/FAILED transitions.

`DispatchService` provides transactionally audited enqueue, principal-scoped
status reads, SKIP-LOCKED claims, lease renewal, expiry recovery, bounded retry,
and terminal completion/failure. A `NEEDS_REVIEW` import is an execution
success (`dispatch=SUCCEEDED`) whose status projection exposes the review
result; a failed import is terminal `dispatch=FAILED`. The migration revokes
database TEMPORARY and PUBLIC privileges and grants only the currently tested
compatibility-role columns. Migration `20260823_0006` adds separate
`ledgerbridge_api` and `ledgerbridge_worker` runtime roles; migration
`20260823_0007` retires the legacy `ledgerbridge_app` login and runtime grants
in production. Migration `20260823_0008` makes dispatch acceptance a
security-definer enqueue operation and binds each row to an exact
`import.dispatch.accepted` payload created in the same transaction. API can
call the enqueue function but cannot insert dispatch rows or update dispatch
state; worker can update bounded dispatch lease/result columns but cannot insert
dispatch rows. Both roles are non-owner logins without TEMPORARY privilege. The
owner-only migrate service uses an explicit non-production profile in CI, while
production API and worker settings require distinct dedicated role URLs.
Forward migration `20260824_0009` repairs the historical Phase 1/2 function
definitions by fixing `search_path=pg_catalog` and schema-qualifying all
business-table references; its downgrade to `0008` intentionally preserves
those hardened definitions.

The Codex branch now also contains the separately named async operation
profile: `POST /v1/evidence/import-requests` returns `202` only after the
published artifact, audit binding and dispatch row commit, and the principal-
scoped `GET` status projection exposes only bounded dispatch/result fields.
`worker.py` contains the claim, lease-renewal, retry and terminalization loop;
the API never calls the importer in this profile. Both are guarded by the
internal async flag and by production fail-closed checks. The default manifest
loader returns no generation and the default worker Connector registry is empty,
so the endpoint and loop cannot execute real import work until a separately
reviewed manifest and real Connector are supplied. The worker composition root
now accepts only an injected `VerifiedRunnerManifest`; it performs canonical
digest/identity checks and constructs worker-owned `RunnerConnector` facades,
but does not load files, keys, or providers. The final local regression is
`217 passed / 136 skipped`; the exact hosted CI coverage command passed in the
prior disposable Hermes run at `348 passed` and `95.26%`. Production Hermes
remains on `20260822_0004`; no dispatch row, endpoint request, evidence bytes or
real Connector was used in production.

## Database permissions

Migration `20260822_0004` creates `ingest_channel` and `source_system` with
canonical IDs matching `^[a-z][a-z0-9_]{0,63}$`. It seeds only
`manual_upload`, `synthetic_upload`, and `synthetic`. Runtime receives SELECT
only. UPDATE and DELETE are blocked even for the owner by a trigger function with
`search_path=pg_catalog`; new identities require a reviewed migration. Existing
unregistered provenance makes upgrade roll back, and any dependent data makes
downgrade refuse rather than erase provenance.

The compatibility role receives the legacy SELECT/INSERT grants only in
non-production test profiles. Production API enqueue uses the dedicated
security-definer function and cannot directly insert dispatch rows; the worker
can update only explicit lifecycle columns. Database triggers reject RawArtifact
or SourceRecord UPDATE/DELETE even from the migration owner and reject illegal
job transitions. The SourceRecord and external transaction identities use
unique constraints, and every evidence relationship uses `ON DELETE RESTRICT`.

The runtime role has no database `TEMPORARY` privilege (including through
`PUBLIC`). Every security-relevant trigger function pins
`search_path=pg_catalog`, and every business-table reference is schema-qualified
as `public.*`. Revoking temporary tables is the least-privilege boundary; schema
qualification is the defense in depth if that privilege is accidentally restored.

The API keeps the artifact volume read-only. The worker is the only service with
a read-write artifact mount. Both continue to use a read-only root filesystem,
dropped capabilities, no-new-privileges, the unprivileged UID, and the
least-privileged database login. The async operation profile is an internal,
default-disabled orchestration endpoint; it performs no production ingestion.

## POSTED audit binding

Creation has its own semantic binding: every new JournalEntry must reference a
fresh `journal.create` event from the same transaction whose
`payload.journal_entry_id` targets that exact preallocated UUID. An unrelated or
stale event cannot authorize creation.

`journal_entry.posted_audit_event_id` is nullable for DRAFT/manual lifecycle
work and mandatory exactly when status is POSTED. A valid DRAFT-to-POSTED
transition references a newly appended event in the same PostgreSQL transaction.
The event must:

- be distinct from every creation event;
- have action `journal.post`;
- target the exact JournalEntry UUID in `payload.journal_entry_id`;
- be used by only one JournalEntry.

The database checks the event transaction ID in addition to its action and
target. A stale pre-created event cannot be attached later. The application
helper appends the event and updates the entry without committing; the caller's
commit evaluates the existing deferred balance, completeness, and entity
constraints. Any failure rolls back both the status transition and audit append.
The audit chain therefore reconstructs the entry, actor, time, reason, and target
for every POSTED transition.

## Recovery and deployment gate

New encrypted backups use `ledgerbridge-encrypted-backup-v2`. They record exact
revision-derived table counts, every application security function and its
`proconfig`, every public trigger and enabled state, runtime table/sequence/
function grants, TEMP and schema-CREATE denial, artifact quota configuration,
published/staging usage, and archive safety. New restore reports use
`ledgerbridge-restore-rehearsal-v2` and compare every v2 field exactly.

The reader still accepts v1 encrypted bundles. A v1 rehearsal compares only the
legacy fields actually present in the source backup and lists the richer restored
observations separately; it does not invent Phase 2 or Phase 3 source-side
evidence. Unsupported future formats fail closed.

No production migration is implied by merging Phase 2 or implementing the
dispatch foundation. Before an authorized
deployment, create a fresh encrypted backup and pass an isolated restore
rehearsal. After deployment, create another encrypted backup and repeat the
restore verification. Real financial evidence and real connectors remain out of
scope until separately approved.

The Phase 2 and Phase 3 downgrades are intentionally non-destructive: if any
dispatch, RawArtifact, ImportJob, or SourceRecord exists, the relevant downgrade
fails closed rather than deleting provenance. The dispatch migration first
downgrades only when `evidence_import_dispatch` is empty; the underlying Phase 2
objects still refuse downgrade to `20260821_0002` while evidence exists.
Operators must export and explicitly dispose of evidence through a separately
approved procedure before removing Phase 2 objects. The Phase 1 function
hardening and database-wide `PUBLIC` temporary-privilege revocation remain in
place after an empty downgrade. Restore validation must also assert that every
security trigger is present and `tgenabled = 'O'`; a table owner can otherwise
disable PostgreSQL triggers by design.

## Deferred request and runner availability controls

The internal multipart routes remain disabled by default and fail closed in
production. When a separately approved test profile enables them, request body
reads are bounded by `LEDGERBRIDGE_UPLOAD_READ_TIMEOUT_SECONDS` (default 120
seconds) and `LEDGERBRIDGE_UPLOAD_CONCURRENCY` (default two). Admission is
independent of the asyncio event loop and is held until the temporary body is
closed; timeouts return `EVIDENCE_READ_TIMEOUT` (HTTP 408) and saturation
returns `EVIDENCE_UPLOAD_BUSY` (HTTP 429).

The isolated runner uses a dedicated executor capped at eight synchronous
Connector calls (default four). A cancelled asyncio wait does not release a
slot until the underlying call actually returns, and saturated work fails
closed with `TIMEOUT`. This bounds thread growth; hostile real Connectors still
require killable process isolation and a reviewed signed manifest before
enablement.
