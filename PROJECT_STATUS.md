# Project status

Updated: 2026-09-04

## Dedicated cash-reconciliation principal checkpoint (2026-09-04)

The September production projection contains 19 eligible, uniquely matched
company facts, but the Web BFF currently requests it with the generic Web mTLS
principal and therefore receives an empty company scope. This branch adds a
dedicated reconciliation identity on ingress port 8446 with only
`reconciliation:read` and `ledger:read`; its grants are derived from the one
bound personal Web grant and exactly seven bound company-report grants. Web must
use this client without fallback. No migration or financial-data write is added.
See `docs/tasks/2026-09-04-cash-reconciliation-principal.md`.

## Dedicated multi-company report principal checkpoint (2026-09-02)

Production baselines were rechecked as Core `5d03d93fe43670ec4136754050eab06e4dab2b0c`,
Web `e210833f199cec5caafada7ad95f4e533c816c4c`, schema `20260902_0034`, and
policy generation 5. Core now has a backward-compatible v2 mTLS policy model
that can bind the existing Web certificate and a separate company-report
certificate to distinct principals. The report principal has only
`company-report:read`; it cannot use Candidate, evidence, ledger-summary,
payroll, or command routes. The internal ingress binds the primary identity to
TLS port 8443 and the report identity to port 8444, so crossing a certificate
and port fails authentication.

The private policy candidate builder preserves the existing primary authority,
requires exactly five unique company grants with immutable business-unit
bindings, advances generation by exactly one, and creates a new mode-0600 file
without overwriting production policy. Web's dedicated report client consumes
port 8444 while its browser API continues to reject `company_ref`; the browser
selector is populated only from the server-authorized collection. No
certificate, private company grant file, policy activation, migration, data
write, or deployment is included in this branch. See
`docs/tasks/2026-09-02-company-report-multi-principal.md`.

## Atomic company statement batch checkpoint (2026-09-02)

Five complete official MYbank company range statements now supersede the incomplete daily batch
as the production source set. The new v3 parser preserves the strict daily v2 contract while
admitting the three observed official 9/11-column range-export variants, multi-day ordering,
header totals, balance-chain checks, and missing-column semantics. The five sources contain 1,442
rows; nine exact facts already exist from the representative slice, so the private v2 plan binds
an expected delta of 1,433 facts and 1,442 observations. PostgreSQL still performs full exact-fact
comparison for every overlapping serial and rejects the whole transaction on any conflict.

Local validation passes 1,302 tests with 212 environment/platform skips. Deployment, fresh
backup/restore, isolated five-file preflight, production commit, exact replay, count reconciliation,
and post-import backup/restore remain pending.

### Earlier daily-batch checkpoint

The existing-account statement cutover now supports a bounded ordered batch under one
database transaction. One verified encrypted backup and isolated-restore inventory anchors the
initial state; each item inherits the exact accepted inventory from the preceding item and still
runs its own source validation, encrypted Evidence publication, immediate idempotent replay, and
overlapping-fact conflict probe. Any item or final acceptance failure rolls back every database
change and aborts every staged encrypted publication. A fully completed batch may be replayed as
an exact zero-delta operation.

The operator command binds the private manifest digest, ordered item ids, every finalized plan
digest, common revision, backup, restore report, key, and artifact root. Both preflight and
production receipts are private mode-0600 artifacts; receipt validation occurs inside the
transaction before commit. A post-commit receipt-write failure has a distinct committed-state
exit instead of being reported as a rollback. No migration or ledger-posting capability was
added. Production execution remains pending a fresh revision-bound backup/restore, isolated
18-item preflight, committed import, exact batch replay, count reconciliation, and post-import
backup/restore. See `docs/tasks/2026-09-02-company-statement-batch-import.md`.

## Authoritative fact-layer vertical-slice checkpoint (2026-09-02)

Primary integration ownership is recorded on branch
`ai/chatgpt/company-mybank-production-import`. Production now runs Core revision
`87f88d81267434a90cb335751de97c2fe77d1f26` at schema `20260902_0033`. The representative
official company statement slice imported one encrypted Evidence lineage, one statement, nine
transaction facts and observations, and one pending review. Transactional preflight, exact
replay, conflict rejection, encrypted pre/post-release backups, isolated restores, service
health, and the read-only company-report contract all passed. Candidate, Journal Entry, and
Posting counts did not change.

The accepted target is one evidence-to-posting data foundation with independently developed
modules behind versioned interfaces. Module/file ownership may proceed in parallel, while shared
contracts, Alembic migrations, integration commits, and production releases remain single-owner.
This slice proves `Official Source Document -> Evidence -> Managed Account -> Bank Statement ->`
`Normalized Financial Facts -> pending-review visibility in ACCOUNT_STATEMENT Company Report`.
It also found and removed an invalid `candidate_source` dependency from the statement report:
the authoritative statement layer no longer needs an Accounting Candidate merely to be visible.
The report shows one pending item and continues to exclude its amounts from confirmed cash flow.
The current Web workload policy does not yet grant this representative company, so Web policy and
selector wiring remain separate work. Supported statement review, later classification, balanced
draft, and human posting are also still open.

## Company financial dashboard checkpoint (2026-09-01)

Core now exposes a category-composition companion to the existing company-report projection for
confirmed Candidate TEST data and posted-ledger formal data. Category rows use immutable snapshots,
remain basis-separated, and must reconcile exactly to the existing company total or fail closed.
The route retains workload mTLS, `ledger:read`, explicit company/business-unit grants, one audit
horizon, bounded months/companies/categories, no-store responses, and exact reader-only database
privileges. Account statements remain a separate cash-flow layer and are not treated as an
income/expense taxonomy.

LedgerBridge Web consumes the companion contract through its BFF and now provides a company
selector, bounded month range, TEST/formal basis switch, total income, total expense, net amount,
and ranked income/expense category shares. Existing facts are display-only TEST input under the
user's authorization; no data was posted, reclassified, backfilled, migrated, or deployed.
Focused validation passed 218 Core tests; the complete Core suite passed 1,152 tests with 208
environment skips. Web passed 141 backend tests (one skip) and 81 component tests. Ruff, strict
changed-source mypy, ESLint, TypeScript, and the production build also passed. PostgreSQL migration
replay remains environment-gated because no disposable database is configured on this host.

## Product-test payroll and statement intake checkpoint (2026-09-01)

The payroll material preview now also accepts the strict
`payroll-summary-authoritative-preview/v1` projection for historical wage-statistics workbooks.
It preserves the summary workbook as the historical source of truth, validates descending unique
months, unique store labels, integer-minor amounts, total-row reconciliation, company scope, and
permanent disabled payment flags.  July/August payroll sheets remain separate TEST_ONLY experiment
materials and are never added together to manufacture historical totals.

Core now exposes the company-scoped TEST_ONLY payroll workspace operations used by Web to read,
organize, validate, and preview historical material.  The adapter binds every successful
organize/validate receipt to the exact submitted workspace revision, period, material type, and
company; it rejects raw-account-shaped identifiers, inconsistent gross pay, and any unexplained
net-pay mismatch.  A mismatching net amount can remain visible only as an explicit blocking
human-review exception.  Payment, payable, submission, and bank capabilities remain absent.

The controlled statement tooling now includes a private-plan builder for the supplied MYbank
workbook and an evidence-bound welfare-benefit source decomposition.  Welfare offsets are
Candidate source components, not ledger Postings: the purchase is preserved and the proven
offset becomes separate welfare income, while downstream double-entry balancing remains with the
ledger.  No real statement, payroll material, account number, owner mapping, or private plan is
tracked in Git.  Windows verification passes 1,121 tests with 208 environment skips; changed-file
Ruff, strict mypy, sensitive-path, and diff checks pass.  Deployment and private test-bundle
import are recorded separately after VM103 verification.

## Similar-transaction classification checkpoint (2026-08-31)

Core now owns a versioned exact similarity key and a visible group contract. It
prefers registry counterparty identity and otherwise accepts only the frozen
seven-field platform summary shape; date and amount are excluded from the key,
while direction, type, counterparty, funding instrument, status, source, Entity,
currency, and risk signature remain bound. The provisional summary basis is
shown to Web and cannot learn a rule. Low-confidence, blocked, structural-risk,
and robust amount-outlier members are excluded. `TRANSFER_REVIEW_REQUIRED` may
participate only after an explicit risk acknowledgement, never via one click or
learned automation.

Migration `20260831_0026` adds an API-only SECURITY DEFINER batch command and
append-only batch/member/assertion receipts. It validates all supplied members,
locks Candidate UUIDs in deterministic order, checks current revision, PENDING
state, month, scope, source, and exact group facts, then calls the existing
per-Candidate append-only command in the same PostgreSQL transaction. Any error
rolls back all member events and the batch receipt; successful replay writes no
new Candidate event. Only business unit and reporting category may propagate.
Database-backed retries probe an actor-, scope-, content-, and operation-bound
receipt before reading mutable Candidate projections, so a completed batch can
still replay after its members become terminal. PostgreSQL independently
recomputes the complete `ledgerbridge.classification-key.v1` key and current
risk signature for every locked member, and the append-only receipt/audit event
preserve the closed acknowledged-risk list. Backup/restore metadata now checks
the new tables, constraints, triggers, pinned SECURITY DEFINER functions, ACLs,
and API-only execution matrix.
No production migration, Candidate mutation, enablement, or deployment was run.
The Web vertical slice and registry-backed learned-rule management remain the
next implementation steps.

## MYbank reviewed one-shot runner checkpoint (2026-08-31)

The integrated Core branch now contains a fail-closed one-shot MYbank statement
runner. It loads an operator-confirmed private plan from a regular mode-`0600`
file, supports an isolated `--preflight-only` import/replay/conflict rehearsal,
binds the resulting private receipt to the canonical plan and reviewed revision,
and requires a separate explicit production switch before the same transaction
can commit. Database facts and encrypted evidence remain under one outer rollback
boundary until inventory, candidate-zero, idempotent replay, and overlapping-fact
conflict checks all pass.

Only a synthetic field template and operating instructions are tracked. No real
owner, entity, business-unit, account, statement path, credential, private plan,
production write, deployment, or enablement was added. The remaining operating
gate is for the user to confirm the exact owner/entity/business-unit/account
mapping through the website/private plan, then run the isolated preflight before
any separately authorized production execution.

## Controlled evidence unlock checkpoint (2026-08-30)

The real-data cutover branch now contains the default-disabled Core and sidecar boundaries needed
for the Web password popup to request one controlled evidence unlock. The public projection has
explicit `NOT_REQUIRED`, `PASSWORD_REQUIRED`, and `UNLOCKED` states; Web consumes that state and
does not infer it. Core binds the request to reviewed scope, workload capability, a short-lived
user assertion, operation id, and assertion nonce. The password is not persisted, logged, echoed,
queued, or passed through process arguments.

Archive parsing runs only in the dedicated `evidence-unlocker` process over a private Unix domain
socket. The API artifact volume remains read-only; the no-network/no-database sidecar validates
encrypted ZIP members and writes encrypted outputs. Production compose keeps both the HTTP route
and sidecar profile disabled by default. Migration `20260830_0025` now follows the coordinated
`0022 -> 0023 -> 0024` chain and installs append-only reviewed-source, operation, receipt, and
output facts plus the authoritative audit-horizon candidate projection. Backup/restore validation
pins their owners, signatures, triggers, ACLs, and effective privileges. No production source or
enablement was added. See
`docs/adr/0002-isolate-evidence-unlock-in-a-sidecar.md`.

## Manual-review posting correction checkpoint (2026-08-30)

The isolated branch `ai/chatgpt/manual-review-writeback-core` now contains the
forward-only `20260830_0022` Core slice. PENDING candidates can atomically append
one CORRECT_AND_CONFIRM event that changes an allowlisted business unit,
category, integer minor-unit amount, or accounting month and ends CONFIRMED.
The command preserves prior/result revisions, source and evidence snapshots,
field-change children, optimistic revision checks, idempotency, and the audit
binding. Terminal states remain read-only.

The new accounting-dimensions reader catalog returns only active authorized
stable refs/codes. Duplicate active labels, unknown/cross-entity targets, and
retired targets fail closed. CORRECT_AND_CONFIRM also revalidates the final
derived business unit and category when only amount or month changes, so an old
Candidate cannot copy a retired dimension into a new revision. Synthetic
RESOLVE_CONFLICT now applies its legal correction patch like the database
backend. The SECURITY DEFINER catalog function has reader-only EXECUTE after
explicit PUBLIC/API/worker and optional app/backup revocation.

The 0022 downgrade retains the precise new-event diagnostic and then refuses
every nonempty R1 fact database, including durable 0017–0021 import receipts,
evidence links, hotel cutover, counterparty, managed-account, and bank-statement
facts; only an empty isolated database may round-trip.
The full local suite passes **778 tests** with **200 skips** and one existing
Starlette warning; the focused Core slice passes **141 tests** with **43
PostgreSQL skips**. Ruff format/check, changed-file mypy, sensitive-path,
single-Alembic-head, and diff checks pass. PostgreSQL integration and ACL tests
are present but remain unexecuted locally because this host has no configured
disposable PostgreSQL URL or local PostgreSQL runtime. No production Candidate,
database, migration, deployment, or Web release was changed.

## Accounting Owner registry checkpoint (2026-08-30)

The isolated `ai/chatgpt/account-owner-registry` branch now provides the Shared Financial
Foundation's explicit Entity-owned Managed Account registry. Migration `20260830_0023` keeps
`20260830_0022` as its final predecessor and adds evidence-backed account admission, normalized
aliases, effective-dated business-unit assignments, fact allocation revisions, immutable
business-unit ref/label snapshots, audit-bound idempotent writes, and an audit-horizon-bound v1
read projection. MyBank statement import now accepts only a pre-registered owner Entity UUID and
Managed Account UUID, and its external-Session seam allows Evidence, registry admission, and
statement import to share one caller-owned transaction. No owner, account, statement, private
sample, credential, production data, merge, or deployment was created.

Local verification passes 772 tests with 199 PostgreSQL/environment tests skipped, including
the new migration replay tests that await the centrally owned 0022 migration and a PostgreSQL
URL. Changed-file lint, format, typing, sensitive-path, and backup/restore checks pass. The
repository-wide format/type gates still report pre-existing failures in unchanged hotel-payout,
configuration, and D1 files. See `docs/tasks/2026-08-30-account-owner-registry.md`.

## Company reporting projection checkpoint (2026-08-30)

Branch `ai/chatgpt/company-reporting-core` now contains a default-disabled, read-only
`ledgerbridge.company-report.v1` projection and `/internal/v1/company-reports` adapter. Each
response selects one of three non-interchangeable fact bases: confirmed candidate, confirmed
account statement, or posted ledger. Only posted ledger exposes formal revenue, expense, and
profit. Browser-supplied scope is not trusted; Core derives a bounded company and business-unit
allowlist from the verified workload principal and reads all companies at one immutable audit
horizon.

Revision `20260830_0024` owns the separate `company_reporting_read` function surface and grants
the reader only its exact entrypoint. It also marks a small shared-write hardening boundary:
future journal-entry attribution captures the business-unit ref/label at write time. Existing
history is not guessed; missing snapshots close only the affected business-unit breakdown while
company-level posted totals remain available. Authoritative opening/closing balances and the
material taxonomy remain explicit gaps. Production sampling was read-only, and no deployment,
production migration, candidate mutation, posting, or credential change was performed. See
`docs/tasks/2026-08-30-company-reporting-projection.md` and
`docs/contracts/company-report-v1.openapi.yaml`.

Local implementation validation passed 72 focused tests and the 823-test Core Windows suite;
198 platform/database integration cases were skipped by their existing environment gates. The
remaining merge gate is to integrate the separately owned `0022` and `0023` migrations before
`0024`, then replay the chain against a disposable PostgreSQL database.
## Original reconciliation projection checkpoint (2026-08-30)

Branch `ai/chatgpt/original-reconciliation-core` adds a default-private, read-only
`ledgerbridge.original-reconciliation.v1` projection for the historical A:M / 40-row
reconciliation layout. It keeps the existing monthly reconciliation route unchanged,
requires exact entity and business-unit authorization, separates pending review, missing
materials, confirmed-pending-posting facts, and formal POSTED ledger totals, and preserves
unknown ledger totals or balances as null/GAP instead of zero. The deployed legacy layout and
source-to-slot rules remain injected private configuration; no real labels, financial material,
schema migration, production reader enablement, merge, or deployment is included. See
`docs/tasks/2026-08-30-original-reconciliation-projection.md`.
The production adapter now loads that private layout only from a hash-pinned read-only mount and
requires `ledger:read` in addition to the original capabilities. It can prove a complete empty
POSTED ledger and return a valid zero-valued A:M / 40-row projection; any non-empty aggregate
summary still fails closed because it lacks the primary `posting.id` identities required by the
frozen fact contract. Confirmed Candidates remain pending posting and never enter formal totals.
The reader also exposes `MISSING_TIME_GRANULARITY` rather than inferring the workbook's intra-month
rows; future missing historical business-unit attribution is reserved as a breakdown GAP and must
not invalidate an otherwise sound company-level formal total.
## Payroll publication adapter checkpoint (2026-08-30)

The real-data cutover branch now contains a default-disabled, read-only adapter for the
`payroll-ledgerbridge-publication/v1` exchange object. It derives company scope from the
existing authenticated `WorkloadPrincipal` and its single `EntityGrant`, maps that entity to
the provider's stable `company_id`, and never accepts browser-supplied company scope. The
adapter does not read the PayrollVerification database, copy its pages, import formal payroll
data, connect to a bank, or expose any payment action. It rejects unknown major versions,
cross-company identities, non-integer minor-unit money, invalid verification/material
projections, broken approval audit chains, and any payable or submission-capable projection.

Production configuration remains disabled. Runtime enablement still requires a separately
deployed authenticated PayrollVerification endpoint reachable from VM103 and a trusted
provider-side publication authorizer. The current Windows-only development endpoint is not a
production network dependency. See `docs/tasks/2026-08-30-payroll-publication-adapter.md` and
`docs/reviews/2026-08-30-payroll-integration-handoff-codex.md`.

The accepted target architecture is a modular monolith: Personal Finance, Hotel Reconciliation,
and Payroll remain independently maintained business modules on one PostgreSQL database. They
share the Financial Foundation identity/account/evidence/audit contracts but own their private
schemas and do not directly write one another's tables. The HTTP payroll adapter is a transitional
implementation behind the stable v1 contract, not a commitment to a separate payroll database.
See `CONTEXT-MAP.md` and `docs/adr/0001-modular-finance-contexts-shared-database.md`.

## May financial foundation checkpoint (2026-08-29)

The real-data cutover branch now contains a one-command, review-only financial foundation
builder for WeChat Pay, Alipay, and the controlled Bank of China monthly review workbook. The
real May 2026 rehearsal normalized 208 rows (93 WeChat, 73 Alipay, 42 BOC), retained 194 rows in
the provisional result, registered 13 supplied or observed accounts, and produced five missing
statement requests. Record-id, evidence-reference, and transfer-balance checks all passed. The
generated workbook has a concise first-page summary and separate transaction, account,
internal-transfer, and missing-evidence sheets. It does not automatically post, confirm, or
replace production candidates. Production Web import and replay verification remain the next
gate. See `docs/operations/MAY_FINANCIAL_FOUNDATION.md`.

## OCR and managed-account preprocessing checkpoint (2026-08-29)

The real-data cutover branch now contains an offline bill OCR boundary and a statement-backed
Managed Account model. RapidOCR 3.9.2 and ONNX Runtime 1.29.0 are optional, pinned dependencies
used only by a networkless, secret-free one-shot container. Bill extraction records field-level
confidence, refuses cropped dates, treats summary screenshots as context only, and can replace
the legacy fixed-cell photo candidate builder through an explicit private OCR-observation input.
Managed Account transfers require bilateral statement evidence; cross-company movements remain
Related-Party Transfers rather than being silently eliminated. No OCR-derived production
candidate replacement or automatic posting has been performed at this checkpoint.

## F-4 immutable image binding hotfix checkpoint (2026-08-28)

An encrypted-storage cutover preflight exposed a fail-closed restore rehearsal
failure on production Hermes: the healthy API/worker containers still reference
immutable image ID `f417a464f7d0...` with the full deployed revision label, while
the mutable tag `ledgerbridge-app:e426b48` resolves to `cb2cd5c13bcb...` with
only the seven-character revision label. No disk, filesystem, service cutover,
or real-data enablement occurred; the newly created encrypted backup remains at
`/srv/ai-center/backups/ledgerbridge/20260828T061115Z-e426b488b2ab`.

Branch `ai/chatgpt/storage-cutover-image-binding` introduces backup format v3,
which records and verifies the immutable application image ID while retaining
v1/v2 restore compatibility. Focused Windows backup/restore tests pass **42**.
The ignored one-time cutover wizard now detects tag drift, preserves the drifted
image under a recovery tag, rebuilds a correctly labelled candidate from the
manifest-verified e426 deployment tree without restarting production, and
reuses the existing ciphertext for isolated rehearsal. The candidate rebuild,
rehearsal, storage migration, merge, and deployment still await their explicit
operator/review gates.

## R1 database Core read adapter checkpoint (2026-08-24)

The default-disabled Core route now has an explicit database reader backend on
`ai/chatgpt/r1-db-schema-grants-design`. It requires a separate
`LEDGERBRIDGE_READER_DATABASE_URL` and uses only Migration C's scoped
`internal_read` functions for candidate and reconciliation projections. The
synthetic backend remains the default; production enablement is still rejected.
The S1 application decryptor and scoped LedgerSummary aggregate are now implemented
behind explicit injection and Migration 0015's exact reader function surface;
descriptor-to-envelope binding and unknown-object normalization are covered. The
default route still returns 503 without an injected decryptor/reader bootstrap. No
reader credential, real data, Hermes production change, merge, or deployment was performed. See
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

- Current reviewed head is `b9e3446` on the local
  `ai/chatgpt/r1-db-schema-grants-design` branch (implementation/test base
  `c61825e`). Sol's independent short
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
- PG18.6 is not PG15. Hosted run `32751756532` verified the pushed
  implementation/workflow head `d0f0cf2` on PostgreSQL 15: `secrets`, `quality`,
  and `compose` all completed successfully, including the unchanged coverage
  floor, migration replay, Bandit, and pip-audit. No coverage threshold,
  skip/omit rule, or pragma bypass was added.
  Production reader bootstrap, mTLS, production KeyProvider/durable receipt
  wiring, recovery rehearsal, merge, deployment, and real-data access remain
  closed; the application S1 decryptor and scoped aggregate are implemented
  only behind explicit injection and the reviewed migration function surface.
- The workflow uses a pinned `postgres:15-alpine` service and runs the three
  jobs on `push`/`pull_request`. The run covers the clean pushed SHA only; the
  current worktree now contains uncommitted parallel edits to `0015`,
  `internal_read_service`, and its test, which were not staged or pushed and
  require a separate review/run.

## S1 decryptor and LedgerSummary CI closure (2026-08-25)

- S1 implementation is committed through remote SHA `c5be03e` on
  `ai/chatgpt/r1-db-schema-grants-design`: descriptor-bound encrypted evidence
  decryption, scoped `LedgerSummary`, metadata drift protection, and backup
  restore verification for the new reader function.
- Hosted PostgreSQL 15 run `32755403473` passed `secrets`, `quality`, and
  `compose`. Quality passed the unchanged 90% coverage gate, full pytest,
  Alembic upgrade→downgrade→upgrade, Bandit, and pip-audit. The preceding
  failures were PG-only exact-function-set assertions that omitted
  `get_ledger_summary_as_of`; the test contract is now synchronized.
- Local Windows full suite: **540 passed / 190 skipped / 1 warning**; Ruff,
  strict mypy, and targeted backup/S1 tests pass. The final remote content
  also includes the exact PostgreSQL function-signature allowlist used by the
  backup verifier.
- No production bootstrap, reader enablement, deployment, or real-data access
  is authorized. Production KeyProvider, durable receipt wiring, mTLS, Hermes
  replay, and HTTP decryptor injection remain closed gates.

## Protected main merge and first runnable demo (2026-08-25)

- PR #19 (`R1/S1 Core reader and demo-ready foundation`) was merged through the
  protected pull-request path as `1714a7866ea3e85789db42c4c5f9929ea7994b07`.
  The merge preserves the production-disabled boundaries; no reader bootstrap,
  mTLS, real KeyProvider, or real-data access was enabled.
- `scripts/r1_synthetic_demo.py` is now a one-command, loopback-only walkthrough
  of all six R1 internal-read GET routes. It uses only packaged synthetic data,
  a fixed demo principal, and an in-process audit sink. `--check` exits after
  exercising the routes and prints a machine-readable proof record; without the
  flag it starts the local demo listener on `127.0.0.1:8651`.
- Local evidence: `uv run --frozen --extra dev python
  scripts/r1_synthetic_demo.py --check` returned six successful routes,
  three candidates, one evidence audit event, and ledger total `SUPPLIES=-12345`.
  The dedicated regression test passed. Main CI run `32758962228` completed
  successfully across `secrets`, `quality`, and `compose`.
- The demo was then merged through PR #20 as
  `3122610236755294eeac505d7e2bee47a4f97a69`; its post-merge main CI
  `32759774150` also completed successfully across all three jobs.

## R1 durable evidence-read receipt boundary (2026-08-25)

- The database reader now has an explicit, opt-in `EvidenceReadReceipt` and
  `InternalReadReceiptSink` contract. `DatabaseInternalReadReceiptSink` calls
  Migration 0015's allowlisted `internal_read.append_internal_evidence_read_audit`
  function and commits only after the function returns an audit id.
- `DatabaseInternalReadService` accepts the receipt sink only through explicit
  injection. It records the receipt after descriptor-bound decryption and
  plaintext digest verification, before returning evidence bytes; any sink
  failure is converted to a fail-closed backend-unavailable error.
- The receipt binds the verified workload `policy_generation`; the envelope's
  external key generation remains separately verified by the decryptor and is
  not substituted into the audit policy field.
- Migration 0015 now enforces a trusted-writer boundary for durable receipts:
  only authenticated `ledgerbridge_api` connections have schema `USAGE` and
  exact receipt-function `EXECUTE`; `ledgerbridge_reader` cannot call the
  `SECURITY DEFINER` receipt function, and the API role has no direct receipt
  or fact-table write privilege. Principal/SAN/policy fields are therefore
  writer assertions, not reader-authorized claims.
- The explicit `enable_internal_read_persistent_receipt` test-only gate now
  injects the receipt sink from the API writer database URL; the default route
  and production settings remain unchanged.
- This is not production wiring: the default route composition remains
  unchanged, no reader bootstrap or production KeyProvider was added, and no
  real evidence was read. Focused audit/database-reader/migration-source tests
  pass **58**; 41 PostgreSQL integration cases remain skipped without the
  disposable database URL.

## Payroll three-channel verification contract (2026-09-01)

- The test-only payroll reader now validates the full disbursement
  reconciliation contract: five MYBANK statements, one Bank of China receipt,
  one WeChat receipt, all wage-table employees, per-channel actual totals, the
  authoritative theoretical total, signed difference, and final match flag.
- Any missing, duplicate, cross-employee, total, channel, or status drift fails
  closed before the projection reaches the BFF.
- This is local feature-parity work only. It does not parse live bank amounts,
  enable payment, deploy production data, or authorize synchronization.

## Scoped monthly reconciliation v2 (2026-09-03)

- `ledgerbridge.cash-reconciliation.v2` derives its entity and business-unit
  scope from verified reader grants and exposes scoped rules, uniquely matched
  rows, unmatched facts, and multi-rule conflicts. Facts with more than one
  matching rule are excluded from every rule and total.
- Bank facts use the Asia/Shanghai transaction date for natural-month and
  effective-date checks. Confirmed WeChat Candidates currently have only
  month-level evidence, so their reviewed `accounting_month` is authoritative
  and rule effective dates use month-overlap semantics.
- Migration `20260903_0038` is integrated directly after production
  company-classification migration `20260903_0037` in one Alembic history.
- Local evidence: focused reconciliation tests pass 10/10; PostgreSQL 15
  upgrade, downgrade, re-upgrade, function security, and reader-only execution
  checks pass. The complete Windows suite retains the same 32 baseline failures
  as production commit `fdf8568` and adds no new failure.
- No posting command is added. Production deployment remains pending the
  unified `core,web` release lock, encrypted backup, and isolated restore gate.
