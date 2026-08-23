# Implementation Plan: ArtifactStore-owned transactional handoff session

## Selected Design And Constraints

The selected design is Option 2 from
`proposals/artifact-handoff-publication-boundary.md`. We will add a bounded
session owned by `ArtifactStore`; a future route will write validated file
chunks to that session and may commit only after the multipart parser has
consumed its explicit completion signal.

Non-negotiable constraints:

- `ArtifactStore` remains the only component that derives the storage key,
  verifies SHA-256/size, enforces published and staging quotas, applies file
  permissions, fsyncs directories, deduplicates, and commits a durable path.
- The existing 50 MiB per-file, 512 MiB aggregate staging and 10 GiB published
  limits remain the defaults. Every in-flight session is counted in the same
  staging budget exactly once.
- `MultipartFileEnd` is not completion. The adapter must consume the closing
  boundary and produce a typed `Complete` condition before `complete()` can
  succeed.
- No `RawArtifact`, `ImportJob`, `SourceRecord`, or audit event is created
  before a handoff returns a committed `PublishedArtifact`.
- No route, authentication integration, Connector registration, real evidence,
  or production deployment is included in this implementation-plan task.

The proposed lifecycle is:

```text
OPEN --write--> OPEN --complete(parser_complete)--> COMMITTED
  |                 |
  +----abort--------+----abort------------------> ABORTED
```

`SEALED` may be represented internally between the final fsync/verification
and destination adoption, but callers never receive a path or a mutable file
handle. Writes after `SEALED`, `COMMITTED`, or `ABORTED` fail closed.

## Source Revision And Drift Check

I refreshed the source before writing this plan. The worktree is clean at
`a8856197aa29ad1b45beec3729b8f67a6c6fc8d1`. The relevant source hashes are
unchanged from the design evidence:

| Path | SHA-256 |
| --- | --- |
| `src/ledgerbridge/artifacts.py` | `82d15ee8d386ecf282ffdd9161d8dcb957139277d3769dae440d9dfb259e714b` |
| `src/ledgerbridge/upload.py` | `eefc0d81b4aff384125e8f88a61fe6bc71f3a5d9ec324ac6a3fd5464ceeb07f3` |
| `tests/test_artifacts.py` | `cfad171499ac4ae97fbd3407e2e6bc7dea62ee8d261647c308bb05905df00ed4` |
| `tests/test_upload.py` | `f1488cc2e960542b953bf68660fe805d18242529299ae43beb900d3dde8ff009` |

The source evidence collection remains bound to
`443933d9019784b0927617d985c3b1348bc9ae86f8b32e92ee95c67e246f490b`; source
drift is `none`. The current revision adds only design documents relative to
the code snapshot, so implementation must re-check these hashes before the
first source edit.

## Affected Components

- `src/ledgerbridge/artifacts.py`: session type, bounded writes, quota
  admission, descriptor sealing, commit/adoption, abort and stale cleanup.
- `src/ledgerbridge/upload.py`: a parser-completion contract that cannot be
  confused with `MultipartFileEnd`; keep the pure parser independent of the
  store.
- `tests/test_artifacts.py`: session state, quota, descriptor, crash and
  concurrency behavior.
- `tests/test_upload.py`: completion event and adapter behavior, including
  malformed suffixes after file bytes.
- A future internal route module: only after the handoff API is proven, behind
  an explicit feature flag and existing server-side actor/authentication
  decisions.
- Observability/configuration: bounded counters and ages for active handoffs,
  aborts, stale cleanup, lock wait and commit failures; no raw body, filename,
  token or exception text.

No database migration is expected for the handoff itself.

## Ordered Work Packages

### 1. Freeze the public contract and state machine

Define a small store-owned interface, for example
`begin_handoff() -> ArtifactHandoff`, `write(bytes)`, `complete()`, and
`abort()`. Keep the temporary path and descriptor private. Use typed errors for
limit rejection, quota state ambiguity, invalid transition, digest/integrity
failure and cancellation. Make `complete()` require a boolean or token that
proves parser completion; it must not infer completion from a byte count or
`FileEnd`.

Acceptance criteria:

- Invalid transitions are deterministic and do not mutate the destination.
- The interface exposes no filesystem path, raw descriptor, or caller-selected
  storage key.
- Existing `ArtifactStore.publish()` callers continue to pass their current
  tests unchanged.

### 2. Create one ArtifactStore-owned staging inode

Create a private regular file under `.staging` with an `artifact-handoff-`
prefix so existing quota scans count it. Set owner-only permissions where the
platform supports them. Keep the file descriptor open in the session and use
descriptor I/O; never reopen by path for ordinary writes.

Acceptance criteria:

- Root, `.staging`, and lock entries retain current real-directory/regular-file
  and symlink rejection behavior.
- A session is charged from its actual descriptor size, and a staging scan
  cannot double-count it.
- A failed create or permission change leaves no unexpected entry.

### 3. Add bounded per-write quota admission

For each `write(chunk)`, obtain the existing cross-process quota lock with a
bounded wait tied to the request deadline. Re-scan staging usage under the lock,
compare `current_usage + len(chunk)` with the 512 MiB limit, then write through
the owned descriptor and verify the descriptor size advanced by exactly the
accepted bytes. Update the in-memory SHA-256 and byte count only after the
write succeeds. The existing blocking lock behavior for legacy `publish()` may
remain until the new route proves bounded lock waiting.

Acceptance criteria:

- Two processes cannot oversubscribe staging, even when both write near the
  limit.
- A short write, non-bytes input, quota-state ambiguity or lock timeout aborts
  the session and reports a bounded error.
- Accepted bytes are charged once; a failed chunk does not advance the digest
  or logical size.

### 4. Seal and verify without reopening by path

`complete()` first requires parser completion and `OPEN` state. Flush and fsync
the descriptor, apply the same private read-only mode used by publication, and
verify size and digest from the owned descriptor. Rewind the descriptor and
mark the session sealed. A second caller cannot complete or write the session.

Acceptance criteria:

- Digest and size are verified against the bytes actually held by the session
  descriptor.
- Descriptor replacement, path replacement, symlink insertion and size drift
  fail closed without touching an existing destination.
- `complete()` is not callable after parser error, abort or disconnect.

### 5. Commit/adopt under the publication authority

Under the quota lock, derive the content-addressed destination from the sealed
digest, validate the destination parent, and check published quota only for a
new digest. If the destination is absent, adopt the sealed inode with the
existing safe hard-link/fsync pattern; if it already exists, verify the
existing bytes and treat the session as a deduplicated commit. Never overwrite
a mismatched destination. Only after the destination is durable may the
session transition to `COMMITTED` and return `PublishedArtifact`.

Acceptance criteria:

- Commit is idempotent for identical bytes and rejects a mismatched existing
  destination.
- Published quota is charged once for a newly created digest and not charged
  again for deduplication.
- Directory fsync and cleanup behavior matches current `publish()` semantics on
  supported platforms.
- A crash before adoption leaves only bounded staging; a crash after adoption
  leaves a valid destination and at most harmless stale handoff residue.

### 6. Abort and stale-session recovery

`abort()` must be idempotent for `OPEN` and `SEALED`, close the descriptor,
unlink only the session's private inode, fsync `.staging`, and transition to
`ABORTED`. Startup or quota-snapshot cleanup may reap only stale regular files
with the handoff prefix after identity/age checks. It must never infer a
successful commit or delete published content.

Acceptance criteria:

- Parser error, client disconnect, cancellation, deadline, quota rejection,
  digest failure and route exception leave no active session descriptor.
- Stale cleanup is bounded, observable and cannot traverse symlinks or delete
  an unknown staging entry.
- Re-running cleanup is safe and does not change a valid published artifact.

### 7. Connect the parser without collapsing completion states

Add an explicit parser completion result/event after final-boundary validation.
The route adapter writes only `MultipartFileChunk` data to the session, records
validated metadata, and calls `complete()` only after the completion event.
`MultipartFileEnd` must be treated as “stop file bytes”, never as EOF for
publication. Parser and store remain independently testable.

Acceptance criteria:

- A malformed suffix after valid file bytes causes `abort()` and no published
  artifact.
- Fragmented final boundaries, trailing bytes, disconnects and slow delivery
  are all bounded and deterministic.
- Unknown fields, unsafe metadata and duplicate control fields fail before
  import state is created.

### 8. Add route/import integration behind a feature flag

Only after packages 1–7 pass, add the internal/test-only route. Authenticate
before reading the body, derive actor server-side, enforce request deadline and
advertised-length early rejection, and map bounded errors to the approved
status-code policy. Invoke `EvidenceImporter` only after a committed artifact;
the route must not write database rows or audit events directly. Keep the route
disabled in production composition until a separate authorization.

Acceptance criteria:

- No artifact/job/audit side effect occurs on parser, handoff, quota, storage,
  database or runner failure.
- Responses contain only the bounded `ImportOutcome` projection and stable
  machine error codes.
- The feature flag fails closed when no reviewed Connector manifest exists.

### 9. Reproduce, benchmark and review before rollout

Run local Windows checks, Linux/PostgreSQL CI, and a disposable Hermes replay.
Perform a security-diff review against the original evidence and add the
resulting report to the PR. Do not use production data or the production
Compose project for the replay.

## Compatibility And Migration

There is no data migration. Keep the current `ArtifactStore.publish()` API and
its tests as a compatibility path while the session implementation is proven.
The first internal route should be feature-flagged and disabled by default.
Existing artifacts and staging entries retain their current layout; new
handoff files use a distinct prefix accepted by the same staging scanner.

If the session API changes the staging scanner or lock protocol, run a full
upgrade/downgrade-free application test rather than altering the database
migrations. Any future config knob must preserve the D-011 defaults when
unset.

## Tactical Protections During Migration

- Keep the pure parser limits, UTF-8/control-text checks, filename/path checks
  and final-boundary requirement unchanged.
- Do not pass a request path, actor, reason, Connector name or `source_system`
  from the client into the store or importer.
- Do not let the route call `publish()` from a stream that returns EOF at
  `MultipartFileEnd`.
- Keep the existing store's descriptor verification, symlink defenses,
  private modes, quota lock, deduplication and fsync checks in the active path.
- Keep production route composition disabled and do not register a real
  Connector or import evidence during implementation testing.

## Tests And Security Validation

Required behavior tests include:

- state transitions: write/complete/abort, double calls, writes after terminal
  state, missing parser completion and cancellation;
- one byte below, at and above 50 MiB, plus aggregate staging at and above
  512 MiB, with arbitrary chunk boundaries;
- malformed trailing boundary after valid bytes, parser error after `FileEnd`,
  trailing bytes, incomplete request and client disconnect;
- concurrent threads/processes near both quota limits and identical-byte
  deduplication;
- short writes, non-bytes streams, digest/size mismatch, fsync failure,
  descriptor/path replacement, symlink and unexpected-entry attacks;
- crash injection before fsync, after seal, before hard-link, after hard-link
  and before stale-temp cleanup;
- route/import no-row/no-audit behavior for every failure class;
- platform-specific lock, `O_NOFOLLOW`, permission and directory-fsync cases.

The existing `tests/test_artifacts.py` and `tests/test_upload.py` remain
mandatory regression suites. The Linux CI quality job must keep the 95%
coverage gate; new handoff branches should have behavior-sensitive tests rather
than coverage-only calls.

## Performance And Resource Benchmarks

No thresholds are invented in this plan. Before implementation review, agree
on an internal-only budget for:

- wall time and p50/p95 latency for 1 MiB and 50 MiB uploads;
- bytes written and digest passes compared with current `publish()`;
- peak RSS and active staging bytes under the configured concurrent-upload
  count;
- quota-lock wait and hold time with fast, fragmented and slow clients;
- stale cleanup latency and maximum residue after an injected crash.

Run each workload on Windows and Linux CI-compatible environments, with cold
and warm filesystem caches where practical. The selected implementation should
be rejected or revised if it exceeds the agreed lock-wait, staging, or latency
budget; do not silently trade availability for a simpler API.

## Rollout And Rollback

1. Land the session and tests with the route feature flag disabled.
2. Run local and hosted quality/security gates plus disposable Hermes replay.
3. Enable only an internal/test profile with bounded concurrency and explicit
   observability; review metrics and failure residue.
4. Keep production disabled until authentication, quota status mapping,
   Connector manifest and a separate deployment authorization exist.

Rollback is the route flag off-switch. If a session defect is found, stop new
handoffs, run the bounded stale-session reaper, preserve published artifacts,
and route existing importer callers through the unchanged `publish()` path.
Revert the session adapter only after confirming no active session descriptors
remain. No database rollback is required for this design.

## Acceptance Criteria

The implementation is ready for internal route review only when all of the
following are true:

- `complete()` cannot succeed before parser closing-boundary completion.
- Every malformed, over-limit, cancelled, disconnected or failed upload leaves
  no durable artifact or import state.
- Staging and published quotas remain correct under concurrent process tests;
  identical bytes deduplicate safely.
- Commit never overwrites a mismatched destination and all descriptor/path/
  symlink/fsync checks remain effective.
- Crash/restart cleanup is bounded, private, idempotent and observable.
- Existing publish/import behavior remains green, and hosted CI passes secrets,
  quality, compose, Bandit, dependency audit and migration checks.
- Performance measurements meet the explicitly approved internal budget.
- A security-diff review confirms the original H001–H005 evidence is addressed
  or has a documented residual risk.

## Open Decisions

- Confirm whether D-011's 512 MiB budget includes all in-flight handoff bytes
  (recommended) and whether the lock wait has a fixed or request-relative
  deadline.
- Choose the public shape of parser completion: event, callback, or a sealed
  handoff adapter hidden behind the route boundary.
- Set internal concurrency and latency thresholds before benchmarking.
- Decide whether `ArtifactStore.publish()` should be refactored onto the new
  primitives in the first implementation or retained as a compatibility path
  until a second review.
