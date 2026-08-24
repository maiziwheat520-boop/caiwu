# Project status

Updated: 2026-08-24

## R1 database Core read adapter checkpoint (2026-08-24)

The default-disabled Core route now has an explicit database reader backend on
`ai/chatgpt/r1-db-schema-grants-design`. It requires a separate
`LEDGERBRIDGE_READER_DATABASE_URL` and uses only Migration C's scoped
`internal_read` functions for candidate and reconciliation projections. The
synthetic backend remains the default; production enablement is still rejected.
Evidence decryption and LedgerSummary remain fixed 503 gates until the reviewed
S1 decryptor and scoped aggregate function exist. No reader credential, real
data, Hermes production change, merge, or deployment was performed. See
`docs/tasks/2026-08-24-r1-database-core-read-adapter.md`.
The adapter also now has a compressed, HMAC-signed keyset cursor bound to the
principal/grant digest, normalized filters, and immutable audit horizon; the
synthetic backend continues to reject cursors.

## Current phase

R1 synthetic Core read API code foundation and the next audit slice are implemented on
`ai/chatgpt/r1-synthetic-core-read-api`. The six frozen `/internal/v1` GET
routes use only integrity-checked packaged fixtures, an exact typed mTLS
principal injection seam, per-capability and entity/business-unit authorization,
explicit unassigned-candidate grants, strict query parsing, fixed error bodies,
and an injected append-only evidence-audit seam. The optional
`DatabaseInternalReadAuditSink` now maps the allowlisted event into the existing
append-only `audit_event` hash chain with an explicit transaction commit and
fail-closed error handling; it is disabled by default and production enablement
is rejected. No new database schema or grants, production verifier, real
evidence, deployment, Hermes, Outlook, OneDrive, or Web adapter was added, so
the operational R1 gate remains open. See
`docs/tasks/2026-08-24-r1-synthetic-core-read-api.md` and
`docs/tasks/2026-08-24-r1-persistent-audit-sink.md`.
R1 Migrations A/B (`20260824_0012`/`20260824_0013`) now add the
Candidate/evidence and ledger/reconciliation fact foundations on
`ai/chatgpt/r1-db-schema-grants-design`: immutable dimensions, evidence and
encrypted blob metadata, Candidate revisions/events, attribution/scope fields,
local immutable snapshots, deferred scope checks, and owner-only append-only
tables. They create no reader role, views, grants, or production read path. The
R1 branch CI coverage floor is temporarily 90% while
the synthetic contract grows its database-backed implementation tests; the
latest Linux/PostgreSQL replay passed 615 tests, 1 skipped, with 91.53% coverage. This is a
test-policy detail only and does not authorize production enablement. See
`docs/tasks/2026-08-24-r1-migration-a-candidate-evidence.md` and
`docs/tasks/2026-08-24-r1-migration-b-ledger-reconciliation.md`.

S1 synthetic online-encryption application foundation is complete on
`ai/chatgpt/s1-online-encryption`. The branch adds test-only external-key
contracts, XChaCha20-Poly1305 secretstream envelopes, encrypted artifact/state/
transient-spool primitives, and a fail-closed host-storage attestation parser.
Independent review found no code-level blocker/high finding and the two spool
medium findings were fixed. Secure-state anti-rollback and host directory/lock
hardening remain explicit real-data gates. Hermes still uses unencrypted
ext4-backed Docker volumes, no production KeyProvider exists, and backup/restore
has not been adapted to the encrypted artifact format.
`LEDGERBRIDGE_ENABLE_REAL_INGEST=true` is rejected unconditionally; no real
source, migration, deployment, or production key was enabled. See
`docs/tasks/2026-08-24-s1-online-encryption.md`.

R0 synthetic Core contract is implemented on the Codex task branch
`ai/chatgpt/r0-synthetic-contract-v2`. It freezes a versioned CandidateProjection,
append-only candidate state graph, deny-by-default test authorization matrix,
six-GET internal OpenAPI, and fixed synthetic fixtures. The contract is not
wired into FastAPI, PostgreSQL, ArtifactStore, Connector registration, Web, or
deployment. No production feature flag, migration, real evidence, OAuth, mail,
Hermes, OneDrive, or workbook behavior was added. See
`docs/tasks/2026-08-24-r0-synthetic-contract.md`.


Phase 3 Platform Security Slice A is merged into protected `main` and deployed
on Hermes at merge commit `e426b488b2abb02f10ef02a61aae7ebe24c3283f` as
`ledgerbridge-app:e426b48`, with Alembic `20260822_0004` at head. The deployment
followed the pre-deploy backup/rehearsal gate and a protected restore-grant
hotfix PR #16. The final post-hotfix encrypted backup and isolated restore
rehearsal passed; API, worker, and PostgreSQL are healthy and no real financial
evidence has been imported.

The deployed Slice A controls provide fail-closed aggregate artifact/staging
quotas, separate append-only ingest-channel/source-system registries, backward-
compatible v2 restore evidence, and the foundation for the no-network Unix-
socket Connector runner. The restore hotfix records column-level runtime grants
that are not covered by table grants and keeps `import_job` updates limited to
the state-machine columns. Same-UID open-inode identity separation remains an
explicit Slice B boundary. Real parsers, OAuth, mailbox collection, real
evidence, and ledger automation remain out of scope. Slice B is implemented and
the Slice C bounded multipart adapter, ArtifactStore-owned transactional handoff,
and default-disabled internal upload route are pushed on the Codex branch
`ai/chatgpt/phase-3-connector-runner` at implementation head `e2c31be` (the
runner composition and API/worker role split are included in the current head;
the current design/plan head is `cdecbdb`; the prior runner audit baseline remains
`bd2ba4a2513597e83764a56215c72b61c99a8c1e`. The isolated runner
image and the handoff replay were tested only as disposable Hermes workloads and
are not deployed.

The current release-readiness hardening head is `e2c31be`; it includes the
deferred-boundary controls, role-membership cleanup, runner global
connection/spool admission and retry classification, heartbeat exclusive-temp
write, and forward migration `20260824_0009` for permanent function
`search_path` hardening. The deferred-boundary remediation report is
`docs/reviews/2026-08-24-deferred-boundary-remediation-codex.md`.
The prior independent security audit report is
`docs/reviews/2026-08-24-independent-security-audit-codex.md`; its canonical
contract artifacts are retained in the terminal scan directory. The subsequent
release-readiness audit report is
`docs/reviews/2026-08-24-release-readiness-independent-audit-codex.md`. Its
HIGH, MEDIUM, and LOW findings are closed by `54b0f2e`, `5019964`, `c7cbeea`,
`989df3b`, `43433dc`, and `e2c31be`; the complete response and evidence are in
`docs/reviews/2026-08-24-release-audit-final-remediation-codex.md`. Real
Connector registration, hostile-process isolation, trusted authentication,
signed manifest/key custody, and production enablement remain separately
gated.
Hosted CI for the prior documentation heads was green across `secrets`,
`quality`, and `compose`; those run IDs remain historical evidence.

Phase 4 framework work is now implemented on the Codex review branch at
`bbe776f` (Hosted CI push `32680886553`, PR `32680884286`). The
mailbox provider is an injected, bounded Microsoft Graph adapter with no token
storage or network client; the Connector registry accepts only an explicit
factory tuple and remains empty by default. Settings keep the provider disabled
and production rejects the provider until authentication and signed-manifest
gates are separately approved. Windows verification is `260 passed / 147
skipped`; Bandit, mypy, ruff, sensitive-path checks, and both Hosted CI workflows
are green. No real OAuth, mailbox, parser, financial evidence, or production
behavior was added. The task card and contract evidence are in
`docs/tasks/2026-08-24-phase-4-mail-connector-framework.md`.

Phase 5's first framework slice is implemented in commit `6b8fb19`, with the
persistence boundary in `540e44e`:
`src/ledgerbridge/reconciliation.py` defines side-effect-free external-ID and
fingerprint dedup decisions, explicit zero-sum 1:1/1:N/N:1 reconciliation
proposals, and an auditable Suspense open/resolve contract. Migration
`20260824_0010` persists review items, reconciliation groups/legs and Suspense
items with deferred zero-sum checks, terminal state triggers, fixed search paths
and no DELETE grants. Automatic deletion, automatic posting and production
switches remain closed. The Review API/worker boundary is implemented but
default-disabled; see
`docs/tasks/2026-08-24-phase-5-review-api-worker.md`. Service and real parser
integration remain separately gated.

The signed-manifest gate now has a fail-closed implementation in commit
`0bf5fea`: `src/ledgerbridge/signed_manifest.py` verifies canonical Ed25519
envelopes, external key ids, generation pins, connector allowlists, and stable
file reads before producing `VerifiedRunnerManifest`. Worker startup loads it
only from explicitly mounted deployment paths; missing or invalid inputs leave
the registry empty. Five signature/tamper/canonicalization tests and the
worker/composition regression pass. No signing key, real manifest, Connector,
OAuth, or production deployment was added. Trusted principal middleware,
key rotation/custody, and a real signed generation remain separate gates; see
`docs/tasks/2026-08-24-signed-manifest-verifier.md`.

The trusted-principal admission seam is now implemented in the working branch:
`src/ledgerbridge/auth.py` defines bounded immutable principals, capability and
policy-generation checks, and a resolver-only ASGI middleware that never trusts
client actor/reason headers. The upload/async route dependency accepts the typed
principal before body reads and rejects raw state when `trusted_gateway` is
configured. It remains disabled by default; certificate/token verification,
gateway deployment, rotation, and production enablement are not included. See
`docs/tasks/2026-08-24-trusted-principal-middleware.md`.

The preceding implementation head `b453874` passed push run `32648931938` and
pull-request run `32648934569` across `secrets`, `quality`, and `compose`; the
Phase 5 persistence commit has also passed the local and isolated Hermes gates
recorded in the task card.
The migration's two controlled-role B608 reports were removed by using fixed
deployment-contract role literals; the final Bandit scan reports no issues.

The deployment report is
`docs/reviews/2026-08-22-phase-3-hermes-deployment-codex.md`. Rollback trees and
the prior `c56b6ff`/`e73e718` images remain on Hermes. The public GitHub repository
and protected main branch remain the source of truth; Claude's independent clone
and narrow-audit entry point are preserved.

Sol Max's follow-up audit found one HIGH and one MEDIUM. The Codex remediation
adds mandatory distinct API/worker credentials and role-specific production URL
validation, retires the legacy `ledgerbridge_app` role in production through
migration `20260823_0007`, and moves dispatch creation to the security-definer
function/acceptance-binding trigger in migration `20260823_0008`. The full
remediation report is
`docs/reviews/2026-08-24-sol-max-audit-remediation-codex.md`. Windows regression
is `232 passed / 138 skipped / 1 warning`; Hermes disposable PostgreSQL replay
through `0008` passed, including API enqueue success and cross-channel rejection.
Production remains unchanged and the branch is still review-only.

The subsequent recheck also identified and fixed service-scoped settings: API,
worker, and migrate now declare explicit runtime roles and only require their
own database URL. The enqueue function additionally enforces artifact/channel
provenance. A second security recheck also closed the compatibility-role grant
and production fallback gaps: 0008 grants enqueue only to API in production,
and production API/worker resolution requires an explicit service role. These
changes are included through head `1d816a2`; Hosted CI push run `32653235633` and
pull-request run `32653238132` are green across `secrets`, `quality`, and
`compose`.

## Release-readiness closure (2026-08-24)

The release-readiness findings are closed on the review branch through
`e2c31be18ce77cbcecc2dec7be3aea2f195367b8`. The final response is
`docs/reviews/2026-08-24-release-audit-final-remediation-codex.md`.

- Windows: `244 passed / 147 skipped / 1 warning`; Ruff, strict mypy, Bandit,
  offline lock, sensitive-path scan, and diff check passed.
- Hermes disposable Linux/PostgreSQL: `391 passed` at `95.23%` coverage;
  migration `upgrade head → downgrade base → upgrade head`, runner capacity /
  retry regressions, and the historical `pg_temp` exploit control all passed.
- Hosted CI push `32679541438` and pull-request `32679543455` for `e2c31be`
  passed `secrets`, `quality`, and `compose`.
- Production Hermes remains read-only verified at revision
  `e426b488b2abb02f10ef02a61aae7ebe24c3283f` / migration `20260822_0004`;
  no async dispatch, Connector, or real financial evidence was created.

The branch remains review-only. Killable hostile-Connector process isolation,
trusted authentication, signed manifest/key custody, production password
rollout, protected merge/deployment, and real parser/provider credentials are
separate enablement gates, not silently inferred from this code closure.

## Completed

- Phase 0 scaffold and Claude blocker/high remediation.
- Public GitHub source of truth: `maiziwheat520-boop/caiwu`.
- F-6 main protection is enabled: PR required, strict `secrets`/`quality`/
  `compose`, admins enforced, conversation resolution required, and force-push/
  branch deletion disabled. Approval count remains zero for the current
  single-human workflow.
- PR #1 merged Phase 0; PR #4 merged the Phase 1 preflight and Claude report at
  merge commit `55f88dd9f8125d34a8952e5af56844c0033d7b27`.
- F-1 shared-worktree risk is closed by separate private clones and identities.
- Phase 1 implements Entity, Account, JournalEntry, Posting, AuditEvent, the
  append-only audit function, POSTED immutability, entity boundaries, deferred
  per-currency balance checks, and POSTED-only balance queries.
- PR #5 final head `e739088ad8f4d0eec91fda6e1e5ab3c268b1b2e6` passed all six jobs
  across its final push and pull-request CI runs and was merged as `2028e3a`.
- Review remediation removes transactional `SET ROLE`; API/worker now log in as
  a separate non-owner runtime LOGIN while migrations use an owner-only one-shot
  service. Tests prove pool reuse and `RESET ROLE` cannot regain owner power.
- Database guards reject duplicate reversals and stale-snapshot audit forks. Account
  and JournalEntry entity identities are immutable from creation, Account class freezes
  after POSTED use, and POSTED transition revalidates every Posting entity.
- OLD/NEW posting-move and per-currency tests are behavior-sensitive; deployment
  manifest root exclusions, unsafe paths, symlink-before-file-exclusion checks, worker
  heartbeat placement, uv wheel hash locking, role bootstrap/downgrade behavior, and
  lifecycle documentation are hardened.
- Final Hermes isolated PostgreSQL 15 acceptance run: 51 tests passed and coverage
  was 99.31%; Ruff, formatting, mypy, Bandit, migration downgrade/upgrade, local
  sensitive-path scanning, and Linux strict pip-audit passed with no known vulnerabilities.
  During the self-audit, the hash-locked image build and isolated API ready/live/OpenAPI-404,
  direct runtime identity, worker heartbeat, UID, revision-label, and migration smoke passed.
- Final Codex self-audit report:
  `docs/reviews/2026-08-21-phase-1-core-schema-final-codex.md`; final verdict is
  APPROVED FOR MERGE with no open findings.
- Phase 1 Hermes deployment report:
  `docs/reviews/2026-08-21-phase-1-hermes-deployment-codex.md`; API, worker, and
  PostgreSQL are healthy, migration head is `20260821_0002`, the API is loopback-only,
  the deployment manifest verifies, and all business tables contain zero rows.
- F-4 PR #7 merged as `0c5616f`; the exact SHA is deployed and its final backup
  `/srv/ai-center/backups/ledgerbridge/20260821T124742Z-0c5616f648d7`
  passed isolated restore. Ciphertext SHA-256 is
  `9d09705ebb482fc7a96f161e7f1b7db6b40f8e0000c6024b2d3e10f479d44e69`.
- Phase 2 remediation PR #11 merged as `c56b6ff`; the exact merge SHA is deployed
  at migration `20260821_0003`. The deployment report is
  `docs/reviews/2026-08-22-phase-2-hermes-deployment-codex.md`.
- The Slice C handoff implementation is complete at `6300bf5`: the pure
  multipart parser emits `MultipartComplete` only after the closing boundary;
  `ArtifactStore.begin_handoff()` owns bounded `write/complete/abort` sessions,
  descriptor-level verification, staging/published quotas, deduplication, fsync,
  and identity-safe cleanup. The internal route implementation at `74d81eb`
  authenticates through a server-side principal dependency, spools requests into
  the handoff only after a complete multipart boundary, maps bounded error codes,
  and continues through `EvidenceImporter.ingest_published()`; its feature flag
  defaults to false and production rejects it even when explicitly enabled.
  The connector manifest is empty by default, so the route fails closed with
  `CONNECTOR_REGISTRY_UNAVAILABLE` until a separately reviewed manifest exists.
- The authentication and Connector admission design is recorded in
  `docs/security-hardening/2026-08-23-auth-connector-boundary/`: three proposals,
  nine comparable diagrams, a twelve-file evidence digest, and a structured
  `hardening.json`. The current recommendation is an internal loopback/mTLS
  principal boundary for the disabled route, and a signed declarative
  runner-only manifest for any real Connector. No provider, signing key,
  Connector, socket mount or production behavior was added.
- The user selected authentication Option 1 and Connector manifest Option 2 for
  implementation planning. The handoffs are in
  `docs/security-hardening/2026-08-23-auth-connector-boundary/implementation/`:
  `trusted-internal-middleware.md` and
  `signed-declarative-runner-manifest.md`. They are plans only; key custody,
  gateway/provider ownership and source-system ownership remain open decisions.
- The API-versus-worker runner composition is documented in
  `proposals/connector-execution-composition.md`. The proposed production
  choice is worker-owned asynchronous dispatch: API stays socket-free, returns
  `202` only after durable enqueue, and worker owns the runner socket. The
  current synchronous route remains an internal/test profile. The accepted
  dispatch schema, grants, service, and PostgreSQL-backed tests are implemented
  on the Codex branch; the feature-flagged async operation/status endpoint and
  worker claim/lease loop are now wired. They remain default-disabled and fail
  closed because the default manifest is empty and the worker Connector
  registry is empty. The fail-closed runner composition root and migration
  `20260823_0006` API/worker role split are now implemented on the Codex branch;
  signed manifest verification, role-password rollout and production
  enablement remain separate gates.
- The user accepted the worker-async baseline and authorized the schema/grants
  and dispatch-service implementation. The implementation
  handoff is
  `docs/security-hardening/2026-08-23-auth-connector-boundary/implementation/worker-async-dispatch-lease.md`:
  it proposes a separate `evidence_import_dispatch` table, API enqueue plus
  `202`/status contract, worker-only claim/lease/runner access, and a pre-
  production API/worker database-role split. The table/service, feature-flagged
  endpoint and worker claim loop are implemented. Runner composition and the
  role-split migration are implemented but remain disabled in production until
  their independent review gates close.
- Final local validation before the current audit was green: **232 passed /
  139 skipped / 1 warning**, with Ruff,
  strict mypy and offline lock resolution passing. The exact hosted CI coverage
  command was replayed in the prior disposable Hermes PostgreSQL project with
  **348 passed** and **95.26%** coverage; the unchanged 95% threshold was not
  lowered. The role-split migration was separately replayed through head and
  back to base in a disposable PostgreSQL project.
  The temporary project, volume, images and directory were removed afterward.
- Production Hermes remains unchanged and healthy at `e426b488b2abb02f10ef02a61aae7ebe24c3283f`
  / migration `20260822_0004`; no async flag, dispatch row, Connector or real
  evidence was created.

## Ownership checkpoint

- Recorded: 2026-08-24
- Deployment revision: `e426b488b2abb02f10ef02a61aae7ebe24c3283f`
- Codex implementation clone: `G:\我的云端硬盘\AI\LedgerBridge-Codex`
- Codex implementation branch: `ai/chatgpt/r1-synthetic-core-read-api`
- Codex identity: `Codex <codex@ledgerbridge.local>`
- Claude review-only clone: `G:\我的云端硬盘\AI\LedgerBridge-Claude`
- Claude identity when explicitly authorized to commit:
  `Claude <claude@ledgerbridge.local>`
- Retired shared clone: `G:\我的云端硬盘\AI\LedgerBridge` (do not write)

## Active implementation owner

Codex is the single writer for the R1 synthetic Core read API foundation on
`ai/chatgpt/r1-synthetic-core-read-api`. Independent agents are review-only.
No production route enablement, migration, runner, real Connector, or real data
is enabled.

## Review owner

Claude completed the independent Phase 2 audit in the separate clone and wrote
only its report. Codex published the Phase 3 finding-by-finding response and
remediation report, while preserving Claude's remaining quota. Claude's second
narrow audit found one HIGH and four MEDIUM follow-up issues; Codex fixed them at
`bd2ba4a2513597e83764a56215c72b61c99a8c1e` and independently replayed the full
Hermes Linux/PostgreSQL suite, including outbound-send deadline and hostile
record terminalization tests. Claude's final narrow recheck is APPROVED at
`a19fa640247a98adacdb31741f6172b722f14f03` with 0 BLOCKER/HIGH/MEDIUM and 10
LOW notes. Hosted CI run `32593102155` for the current branch head is green
across `secrets`, `quality`, and `compose`.
Codex then applied independent post-approval hardening in `e296b0d` for the
shared text predicate, upload metadata validation, worker-call assertion, and
full-size chunk regression. Hosted CI run `32595863205` for the resulting head
is green across `secrets`, `quality`, and `compose`; Claude's approval applies to
the earlier fixed code/test SHA, while this follow-up is Codex-verified.
Protected PR #18 is open at
`https://github.com/maiziwheat520-boop/caiwu/pull/18`; its current head is
`5d12cf5` before the current audit. The latest documentation-inclusive code/test push run is
`32640445494` and pull-request run `32640447250`; both passed `secrets`,
`quality`, and `compose`. The preceding green implementation-plan and code
runs remain recorded below. The code/test push and pull-request runs for `19f4c30`
(`32615944190` and `32615946593`) passed `secrets`, `quality`, and `compose`;
the design/status head `85253b2` push and pull-request runs (`32618527733`
and `32618529442`) also passed all three jobs; the implementation-plan push
and pull-request runs (`32622694690` and `32622696551`) also passed all three
jobs; the preceding
implementation head `6300bf5` was green in runs `32613520982` and `32613522285`.
The composition decision push and pull-request runs (`32624698270` and
`32624699779`) also passed all three jobs.
The PR is review-only; it has not been merged or deployed.
The approved Slice C upload-boundary design and implementation evidence are
recorded in `docs/tasks/2026-08-23-phase-3-slice-c-upload-endpoint-design.md` and
`docs/reviews/2026-08-23-internal-upload-route-implementation-codex.md`. The
route is internal/test-only, default-disabled, and not exposed through the
production composition. The pure bounded multipart adapter is connected to the
ArtifactStore-owned transactional handoff and the importer continuation API;
the server-side authentication provider and real Connector manifest remain
separate review gates. The handoff and production-disabled gate were replayed
in an isolated Hermes project; no evidence was ingested.

## Next task

Audit the Phase 4 mailbox/provider and Phase 5 dedup/reconciliation/Suspense
contracts, including the persisted candidate-key Review boundary, then prepare the
narrow independent Claude audit.
The default manifest and registry stay empty. Next gates are signed-manifest/key
custody, trusted OAuth, provider/source ownership, concurrent candidate matching,
real parser samples, and a narrow Claude audit.
Merge, production role migration, feature-flag enablement, real Connector
registration, evidence ingestion, and mail collection each require distinct
later authorization.

## Blocking decisions

None for Slice A implementation and review. Slice A merge, Slice B implementation,
each production deployment, real Connector registration, OAuth, and real-data
ingestion require their later explicit gates.

## R1 Migration C checkpoint (2026-08-24)

- `20260824_0014_r1_fact_hardening.py` and
  `20260824_0015_r1_internal_read_surface.py` implement the closed reader boundary
  on top of Migration A/B.  The reader role is external/test-bootstrapped only;
  the migration does not create credentials.
- The allowlist is eight `internal_read` security-barrier views plus five fixed
  `SECURITY DEFINER SET search_path = pg_catalog` functions.  Reader has no public
  base-table, sequence, or append-audit access; runtime write roles have no
  internal-read access.
- Disposable Hermes PostgreSQL 15 R1 migration suite: **14 passed** after final
  hardening.  Windows full suite is **472 passed / 155 skipped / 1 warning**;
  Ruff format/check, strict mypy, offline lock, Bandit, and diff-check pass.
  Follow-up commits `eef214a` and `3392693` additionally make all R1 trigger
  validators owner-executed, preserve compatibility `CONNECT` for
  `ledgerbridge_app` while retaining no fact-table/TEMPORARY/CREATE privileges,
  and update the legacy downgrade assertion. Hosted CI run `32721624365` is
  green across `secrets`, `quality`, and `compose`.
- No merge, production role migration, real data read, or deployment is authorized.

## R1 Migration C security-remediation checkpoint (2026-08-24)

- Independent review findings (1 HIGH, 2 MEDIUM) are addressed fail-closed:
  direct reader SELECT on all eight projection views is revoked; only scoped
  SECURITY DEFINER functions remain executable by `ledgerbridge_reader`.
- `ledgerbridge_backup` is now covered by runtime role, membership, ownership,
  database/default ACL, and internal-read privilege checks. When present it is
  optional and receives CONNECT only.
- Zero-attribution legacy POSTED entries no longer silently opt in to R1; the
  hardening upgrade rejects incomplete attribution. Candidate contract width is
  corrected to fit the fixed 25-character wire value.
- Windows full suite: **475 passed / 189 skipped / 1 warning**; strict mypy,
  Ruff, compileall, offline lock, and diff-check pass. Hermes PostgreSQL 15
  complete R1 migration replay is **48 passed**. The replay also exercises
  fresh-database downgrade and reader-surface isolation so the result is not
  dependent on shared-cluster fixture state.
- Remediation report:
  `docs/reviews/2026-08-24-r1-migration-c-security-remediation-codex.md`.
  No merge, production role migration, real data read, or deployment is
  authorized.

## R1 Migration C security-remediation replay closure (2026-08-24)

- The remaining legacy fixture and error-order contracts are now aligned with
  the fail-closed migration order.  This includes candidate blocker evidence,
  reconciliation scope, 0013/0014 downgrade guards, and fresh reader DB
  fixtures.
- Final evidence: Hermes PostgreSQL 15 **48 passed**; Windows **475 passed /
  189 skipped / 1 warning**. No production database, role, reader bootstrap,
  merge, or deployment was touched.

## R1 Core reader cursor hardening (2026-08-24)

- Safe branch `ai/chatgpt/r1-core-reader-cursor` now contains `f3c2a73`:
  database grants require explicit immutable business-unit ref/UUID bindings,
  returned rows are scope-revalidated, non-canonical cursor encodings fail
  closed, and month-filtered / candidate-detail reads advance through bounded
  keyset pages.
- Windows full suite: **494 passed / 190 skipped / 1 warning**; targeted cursor
  and database-reader tests pass; Ruff and strict mypy pass. Hosted CI runs
  `32744316468`, `32744653451`, and `32745416540` kept `secrets`/`compose` green
  but still failed the quality coverage step; no merge or production enablement
  has occurred.
- Database collection union across multiple scopes remains deliberately disabled
  until a per-scope cursor contract is reviewed; such grants return a fixed
  backend-unavailable response rather than risk cross-scope pagination leakage.

## R1 database hardening final local gate (2026-08-25)

- Fixed implementation head is `c61825e` on the local
  `ai/chatgpt/r1-db-schema-grants-design` branch. Sol's independent short
  recheck of `0014`, `0015`, the complete migration chain, and the CI/bootstrap
  change found no validated BLOCKER/HIGH/MEDIUM. This is not merge or production
  authorization.
- `0014` preserves the `VARCHAR(32)` Candidate contract width and the
  `0013→0015→0013` regression. `0015` retains the reader-only,
  security-barrier, fixed-owner/function-search-path surface with fail-closed
  ACLs. Database reads bind immutable entity/business-unit scope, signed
  canonical cursors, and audit horizons; candidate and reconciliation results
  are revalidated against entity/scope/month. Multiple-scope union remains
  disabled. Backup restore verification covers role membership, ownership,
  object/default ACLs, internal-read privileges, and the restricted owner schema
  creation exception.
- Local WSL PostgreSQL **18.6** evidence: R1 migration **49 passed**; CI-like
  full suite **696 passed / 1 warning**, **91.36%** coverage (`6860` statements,
  `593` missed) with the unchanged `--cov-fail-under=90`; full Alembic
  head→base→head passed; focused R1/backup/reader tests **59 passed / 40
  skipped**. Ruff format/check, strict mypy, Bandit, sensitive-path checks,
  pip-audit, and diff-check passed.
- PG18.6 is not PG15. No latest-HEAD Hosted PG15 result is claimed; prior
  hosted runs remained green for secrets/compose but failed only the coverage
  step. No coverage threshold, skip/omit rule, or pragma bypass was added.
  Production reader bootstrap, mTLS, S1 decryptor, scoped ledger aggregate,
  recovery rehearsal, merge, deployment, and real-data access remain closed.
