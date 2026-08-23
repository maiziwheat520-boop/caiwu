# Internal upload route implementation evidence

Date: 2026-08-23  
Branch: `ai/chatgpt/phase-3-connector-runner`  
Implementation: `74d81eb`  
Test/mypy follow-up: `19f4c30`  
PR: [#18](https://github.com/maiziwheat520-boop/caiwu/pull/18)

## Result

The approved Slice C boundary is implemented as an internal/test-only route:

`POST /v1/evidence/imports`

The route is controlled by `LEDGERBRIDGE_ENABLE_INTERNAL_UPLOAD`, which defaults
to `false`. `require_internal_upload()` also rejects every production setting,
including a production setting that explicitly enables the flag. The default
connector manifest is empty, so an enabled non-production instance fails closed
with `CONNECTOR_REGISTRY_UNAVAILABLE` before reading the request body.

No real connector, authentication provider, OAuth flow, mailbox collector,
production route, or evidence fixture was added.

## Boundary and ordering

1. The feature flag and production guard run before authentication and body read.
2. Authentication reads only `request.state.authenticated_principal`, which is
   expected to be installed by trusted middleware. Client actor/reason headers
   are ignored; the route supplies a bounded server-owned audit reason.
3. The request body is copied into a bounded `TemporaryFile`; the declared
   `Content-Length` is an early rejection hint, while the streaming byte budget
   remains authoritative.
4. The pure multipart parser must emit `MultipartComplete` before the route
   builds `IngestMetadata` or completes the `ArtifactHandoff`.
5. `ArtifactHandoff.write/complete/abort` remains the only publication path.
   The route then calls `EvidenceImporter.ingest_published()` so importer-owned
   provenance, database state, and audit transitions are not duplicated in HTTP.
6. Responses expose only the bounded `ImportOutcome` projection and stable
   `error_code` values; exception summaries, filenames, raw fields, and
   connector internals are not returned.

## Tests added

`tests/test_upload_route.py` covers:

- successful multipart handoff and server-derived actor/reason;
- default-off and production-forced-off behavior;
- missing trusted principal and client actor-header non-authority;
- empty connector manifest fail-closed behavior before body read;
- malformed/truncated multipart and parser-completion enforcement;
- filename/media-type metadata bounds and unknown ingest-channel mapping;
- body/file/staging/published quota, storage, integrity, database, and importer
  error mappings;
- bounded request stream type/size enforcement and staging cleanup.

The route target has 23 passing tests locally; the full Windows suite is
`183 passed / 120 skipped / 1 warning`. Ruff formatting/checks, strict mypy,
Bandit, and `uv lock --offline` also passed. PostgreSQL-gated importer and
ledger tests remain Linux/CI-gated on Windows. Hosted push run `32615944190`
and pull-request run `32615946593` both passed `secrets`, `quality`, and
`compose`.

## Hermes isolated replay

The current branch was cloned into a unique disposable Compose project on
Hermes. The API image built successfully, migrations `20260821_0001` through
`20260822_0004` applied to a temporary PostgreSQL volume, and the built image
raised `404 {'error_code': 'INTERNAL_UPLOAD_DISABLED'}` when the production
settings explicitly set `enable_internal_upload=True`. The temporary project’s
containers, volumes, networks, and image were removed afterward. No production
Compose project, service, volume, or database was touched.

## Residual gates

- The principal dependency is an explicit seam for a later reviewed
  authentication middleware; it is not an authentication provider.
- The connector registry intentionally has no reviewed real connector, so the
  route cannot ingest evidence in its current default composition.
- Filesystem publication and database import are separate commits. A database
  failure after publication can leave a content-addressed blob without a
  corresponding row; the existing reconciliation/retention design must be
  exercised before any broader enablement.
- Enabling this route in production, registering a real connector, ingesting
  evidence, or merging/deploying the PR each require a separate authorization.
