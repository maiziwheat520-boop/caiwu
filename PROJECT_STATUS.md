# Project status

Updated: 2026-08-23

## Current phase

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
`ai/chatgpt/phase-3-connector-runner` at code/test head `19f4c30` (route
implementation `74d81eb`); the current design/plan head is `fd0c8d4`; the prior runner audit baseline remains
`bd2ba4a2513597e83764a56215c72b61c99a8c1e`. The isolated runner
image and the handoff replay were tested only as disposable Hermes workloads and
are not deployed.

The deployment report is
`docs/reviews/2026-08-22-phase-3-hermes-deployment-codex.md`. Rollback trees and
the prior `c56b6ff`/`e73e718` images remain on Hermes. The public GitHub repository
and protected main branch remain the source of truth; Claude's independent clone
and narrow-audit entry point are preserved.

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
  current synchronous route remains an internal/test profile; no dispatch
  migration or source change has been implemented.

## Ownership checkpoint

- Recorded: 2026-08-22
- Deployment revision: `e426b488b2abb02f10ef02a61aae7ebe24c3283f`
- Codex implementation clone: `G:\我的云端硬盘\AI\LedgerBridge-Codex`
- Codex implementation branch: `ai/chatgpt/phase-3-connector-runner`
- Codex identity: `Codex <codex@ledgerbridge.local>`
- Claude review-only clone: `G:\我的云端硬盘\AI\LedgerBridge-Claude`
- Claude identity when explicitly authorized to commit:
  `Claude <claude@ledgerbridge.local>`
- Retired shared clone: `G:\我的云端硬盘\AI\LedgerBridge` (do not write)

## Active implementation owner

Codex is the single writer on `ai/chatgpt/phase-3-connector-runner` for Slice B
and the Slice C adapter prerequisite.
Claude remains read-only and is reserved for a later narrow audit. No production
runner or real Connector is enabled.

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
`21d0880`. The code/test push and pull-request runs for `19f4c30`
(`32615944190` and `32615946593`) passed `secrets`, `quality`, and `compose`;
the design/status head `85253b2` push and pull-request runs (`32618527733`
and `32618529442`) also passed all three jobs; the implementation-plan push
and pull-request runs (`32622694690` and `32622696551`) also passed all three
jobs; the preceding
implementation head `6300bf5` was green in runs `32613520982` and `32613522285`.
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

Review protected PR #18, including the bounded multipart adapter, handoff,
feature-flagged internal upload route, and the authentication/Connector design
portfolio. The next gate is accepting or rejecting the proposed worker-async
dispatch/status contract, then resolving provider/key custody, gateway/provider
ownership and source-system owner decisions before implementing in reviewable
phases with a narrow Claude audit; the route stays disabled until
those are approved. Merge and any production
deployment require distinct authorization; do not register a real Connector,
enable the flag in production, ingest evidence, or enable the mail collector
without corresponding later authorization.

## Blocking decisions

None for Slice A implementation and review. Slice A merge, Slice B implementation,
each production deployment, real Connector registration, OAuth, and real-data
ingestion require their later explicit gates.
