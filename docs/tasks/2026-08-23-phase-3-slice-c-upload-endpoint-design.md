# Phase 3 Slice C: upload endpoint prerequisite design

Status: **APPROVED INTERNAL DESIGN — no HTTP upload route or production data
path is implemented.**

This document prepares the first-party upload boundary that may be implemented
after the current Slice B review. It does not register a real Connector, enable
OAuth/mail collection, expose a public endpoint, or authorize production
deployment.

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
   authority. Adapt bounded request chunks directly to `ArtifactStore.publish()`
   so staging quota, cleanup, fsync, and digest checks remain one control path.
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

The exact status-code choice for quota and asynchronous semantics is a product
decision; the storage and audit error codes are not negotiable.

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
2. Confirm the quota HTTP status mapping and authentication dependency before
   writing the route.
3. Implement a pure bounded multipart adapter and unit-test it without FastAPI
   or PostgreSQL.
4. Add an internal/test-only route behind an explicit feature flag; wire it to
   `EvidenceImporter` without adding a real Connector.
5. Run the full local/Linux/PostgreSQL/CI gates and a security-diff review.
6. Only after a separate decision may a future PR add authentication provider
   integration, a real Connector manifest, or a production deployment.

## Explicit non-goals

No OAuth, mailbox polling, provider API, parser, archive extraction, OCR,
malware scanning, automatic posting, public internet exposure, real financial
fixture, production migration, or evidence ingestion is included here.
