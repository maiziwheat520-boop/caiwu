# Phase 3 Slice C: upload endpoint prerequisite design

Status: **IMPLEMENTED INTERNAL/TEST-ONLY ROUTE — feature flag defaults off;
production composition remains closed.**

This document records the first-party upload boundary and its gated internal
implementation. It does not register a real Connector, enable OAuth/mail
collection, expose a public endpoint, or authorize production deployment.

## Existing boundaries to preserve

- `ArtifactStore.publish()` is the only artifact publication authority. It
  streams into private staging, enforces the 50 MiB per-file and aggregate
  quotas, verifies SHA-256, and publishes content-addressed bytes under the
  quota lock.
- `EvidenceImporter.ingest_and_import()` owns provenance, idempotent artifact
  state, Connector matching, runner error mapping, terminal ImportJob state,
  and `import.complete` auditing. The HTTP layer must not duplicate those
  transitions or write database rows directly.
- `IngestMetadata.source` is an append-only `ingest_channel` identity, not a
  provider-controlled free-text source. `original_filename` and `media_type`
  are untrusted text and must use the shared storable-text and length checks.
- `actor` must come from an authenticated server-side principal and `reason`
  must be a bounded server-generated audit reason. Neither may be accepted as
  arbitrary client text.
- Production currently has no real Connector registry. A route must fail closed
  or remain disabled until a separately reviewed manifest exists.

## Proposed HTTP contract (pending approval)

### Request

`POST /v1/evidence/imports` with an authenticated `multipart/form-data` request
containing exactly one binary part named `file` and one canonical
`ingest_channel` field. The server derives the filename and media type from the
multipart metadata, treats both as hostile input, and never treats a filename
as a filesystem path.

Required boundary controls:

1. Authenticate before reading the body; derive `actor` from the principal.
2. The first internal/test-only version does **not** require an
   `Idempotency-Key` (user decision). Duplicate bytes still converge through
   the existing content-addressed artifact path. A bounded idempotency key is
   required before any remote or multi-client exposure; it must be stored only
   as a keyed digest or opaque bounded identifier and never logged raw.
3. Reject an advertised `Content-Length` above the configured per-artifact cap,
   but never rely on it for enforcement. The streaming reader remains the
   authority and aborts at `MAX_ARTIFACT_BYTES + 1`.
4. Do not use Starlette's unbounded default upload spool as the artifact
   authority. The bounded adapter must reach a valid final boundary before the
   route hands bytes to `ArtifactStore.publish()`; a bounded temporary handoff
   (or equivalent two-phase staging) is required so a malformed trailing
   boundary cannot publish a verified orphan. `ArtifactStore` remains the only
   durable publication authority.
5. Validate canonical `ingest_channel`, filename/media type length, NUL/lone
   surrogate rejection, and supported multipart shape before creating import
   state. No request value selects a Connector or `source_system`.

The synchronous model and deferred idempotency-key requirement are explicit
Slice C decisions. The actor remains server-derived even for the internal
route; there is no client-supplied actor override.

### Response

The initial implementation should be synchronous only in the internal/test
profile and return a bounded JSON projection of `ImportOutcome`:

```json
{
  "artifact_id": "uuid",
  "job_id": "uuid",
  "status": "SUCCEEDED|FAILED|NEEDS_REVIEW",
  "parsed_count": 0,
  "created_count": 0,
  "duplicate_count": 0,
  "error_code": null
}
```

It must omit filenames, raw fields, exception strings, provider tokens, and
connector internals. A later asynchronous API may expose a read-only job
status resource, but that is not part of this Slice C prerequisite.

### Error mapping

| Condition | HTTP result | Body rule |
| --- | --- | --- |
| Missing/invalid auth | 401/403 | generic, no storage detail |
| Invalid multipart shape or metadata | 400/422 | field name only, bounded |
| Unsupported media type policy | 415 | generic policy code |
| File or staging quota exceeded | 413/507 | stable machine code, no filename |
| Duplicate/provenance review outcome | 202 or 200 by final sync policy | outcome projection only |
| Connector registry unavailable/empty | 503 | no internal registry detail |
| Runner/ingest terminal failure | 422/500 by error class | existing bounded `error_code` and summary mapping |

The internal implementation uses the documented stable status-code mapping; the
storage and audit error codes are not negotiable. A real authentication provider
and Connector manifest remain separate gates.

## Transaction and failure sequence

1. Authenticate and establish a request deadline/body byte budget.
2. Parse only bounded multipart headers and fields; reject duplicate control
   fields and unsafe text before opening a publication stream.
3. Pass a chunked binary adapter to `ArtifactStore.publish()`; cancellation,
   disconnect, size overflow, quota rejection, read error, or digest failure
   must clean staging and create no `RawArtifact`/`ImportJob` row.
4. Build `IngestMetadata` from validated server values.
5. Invoke `EvidenceImporter` with the server-owned Connector manifest. The
   importer alone creates/updates `RawArtifact`, `ImportJob`, `SourceRecord`,
   and audit events.
6. Map the bounded `ImportOutcome` to the response. Never expose raw exception
   text or return a successful response before the importer commits its
   terminal state.

## Acceptance matrix before route enablement

- Uploads one byte below, at, and above the 50 MiB limit with arbitrary chunk
  boundaries; above-limit requests leave no row, orphan, or staging residue.
- Rejects NUL/lone-surrogate filename and media-type values before publication;
  confirms no artifact, job, or audit side effect.
- Rejects path separators, absolute paths, control-field duplicates, unknown
  ingest channels, and connector/source-system selection attempts.
- Authenticated actor is the only audit actor; client actor/reason fields are
  ignored or rejected. Logs contain no filename, raw body, idempotency key, or
  exception string.
- Duplicate bytes converge on the existing content-addressed artifact and do
  not consume a second published quota reservation.
- Quota, storage, disconnect, cancellation, database, and runner failures
  produce the documented bounded error and preserve the no-row-to-missing-blob
  invariant.
- Production/test composition proves the route is disabled or returns a stable
  `CONNECTOR_REGISTRY_UNAVAILABLE` response while no real manifest is present.
- Property/fuzz tests cover multipart boundary fragmentation, duplicate keys,
  malformed headers, misleading `Content-Length`, slow body delivery, and
  client disconnect.

## Delivery sequence

1. **Decided:** use `/v1/evidence/imports` as a synchronous internal/test-only
   first version; do not require `Idempotency-Key` yet; keep actor server-derived.
2. **Implemented:** the quota HTTP status mapping and trusted server-side
   authentication dependency are explicit in `src/ledgerbridge/main.py`.
3. **Implemented:** the pure bounded multipart adapter is unit-tested without
   FastAPI or PostgreSQL and is reused by the route.
4. **Implemented:** `/v1/evidence/imports` is behind
   `LEDGERBRIDGE_ENABLE_INTERNAL_UPLOAD`, wires the bounded handoff to
   `EvidenceImporter.ingest_published()`, and fails closed while the connector
   manifest is empty.
5. **Validated:** the current route head passed the full local suite and hosted
   Linux/PostgreSQL CI; a separate narrow security audit remains a review task.
6. Only after a separate decision may a future PR add authentication provider
   integration, a real Connector manifest, or a production deployment.

## Adapter implementation evidence

`src/ledgerbridge/upload.py` now provides a pure event parser with no FastAPI,
database, filesystem, or Connector dependencies. It enforces a bounded ASCII
boundary, 16 KiB per-part headers, 512-byte `ingest_channel`, configurable file
and body limits, UTF-8/control-text rejection, exactly one channel before one
file part, filename/path safety, duplicate/unknown field rejection, fragmented
boundary handling, and a successful closing boundary before completion.

`tests/test_upload.py` contains 46 focused tests covering arbitrary chunk
fragmentation, binary content containing boundary-like bytes, unknown and
duplicate fields, file-before-channel ordering, header/field/file/body limits,
malformed headers and parameters, unsafe filenames, invalid UTF-8/control text,
file overflow, declared body overflow, and invalid content types. The adapter
is now connected to FastAPI through the bounded `ArtifactHandoff`; route tests
also prove default-off behavior, trusted server actor derivation, parser
completion, quota/storage/database mappings, and staging cleanup. The route and
importer continuation implementation is in `74d81eb`, with the mypy follow-up
at `19f4c30`; PR #18 CI is the authoritative Linux/PostgreSQL gate.

## ArtifactStore handoff design gate

The next boundary is documented in the derived hardening portfolio
[`docs/security-hardening/2026-08-23-artifact-handoff/hardening.md`](../security-hardening/2026-08-23-artifact-handoff/hardening.md)
and the detailed proposal
[`artifact-handoff-publication-boundary.md`](../security-hardening/2026-08-23-artifact-handoff/proposals/artifact-handoff-publication-boundary.md).
It compares a bounded request spool, an ArtifactStore-owned transactional
handoff session, and a completion-aware stream. The selected transactional
session keeps completion, staging quota, digest verification and publication
under one authority without holding the global quota lock across network pauses.

Option 2 was subsequently selected and implemented at `6300bf5`. Its
implementation handoff is
[`transactional-handoff-session.md`](../security-hardening/2026-08-23-artifact-handoff/implementation/transactional-handoff-session.md),
and the evidence report is
[`2026-08-23-artifact-handoff-implementation-codex.md`](../reviews/2026-08-23-artifact-handoff-implementation-codex.md).
The handoff source and tests are complete. The internal route is implemented but
the production composition remains closed; no production database state or
evidence has been changed.

## Worker-owned async operation profile (implemented 2026-08-23)

The accepted worker-owned composition is now implemented on the Codex branch at
`389a02d`. The separately named `POST /v1/evidence/import-requests` endpoint
returns `202` only after the verified handoff, audit-bound `RawArtifact` and
`evidence_import_dispatch` row commit. Its `GET` operation status is principal-
scoped and exposes only bounded dispatch/result fields. The API never invokes
the importer or mounts the runner socket in this profile.

`worker.py` now provides one-claim processing with `FOR UPDATE SKIP LOCKED`,
lease renewal, expired-lease recovery, bounded retry classification and
terminal success/failure mapping. The internal flag is disabled by default and
production-forced off. The default manifest loader and worker Connector
registry are empty, so endpoint/worker code is testable but cannot execute a
real Connector until signed-manifest/key custody and production enablement are
separately approved. The runner composition root now accepts only an injected
verified manifest and the API/worker database-role migration is implemented;
neither is enabled in production. Windows target tests and a disposable Hermes
PostgreSQL replay passed, including migration downgrade/upgrade and privilege
probes. The final local regression is `217 passed / 136 skipped`; the exact CI
coverage command passed in the prior disposable Hermes run with `348 passed`
and `95.26%` coverage. Production Hermes was not modified.

## Explicit non-goals

No OAuth, mailbox polling, provider API, parser, archive extraction, OCR,
malware scanning, automatic posting, public internet exposure, real financial
fixture, production migration, or evidence ingestion is included here.
