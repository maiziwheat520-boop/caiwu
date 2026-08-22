# Task: Phase 3 Platform Security Foundation

- Status: Slice A implemented, remediated, merged into protected main, and deployed to Hermes
- Preflight date: 2026-08-22
- Implementation owner: Codex
- Review owner: Codex fixed-SHA self-audit; preserve a narrow Claude recheck entry point
- Source base: `1afb70e04aa33b4508de075d2838d9b2a6ff2977`
- Preflight branch: `ai/chatgpt/phase-3-platform-prep`
- Planned implementation branches:
  - `ai/chatgpt/phase-3-platform-controls`
  - `ai/chatgpt/phase-3-connector-runner`
- Planned migration: `20260822_0004`
- Workspace decision: D-011

## Goal

Build the security and recovery controls required before any real financial
connector, mailbox OAuth token, or customer-derived evidence can enter
LedgerBridge. This task makes storage capacity fail closed, turns ambiguous
source strings into registered identities, gives every future real Connector an
out-of-process execution boundary, and upgrades restore evidence to cover the
deployed Phase 2 schema and security controls.

This task uses only synthetic payloads and hostile test doubles. It does not
implement or enable a real parser, mailbox collector, OAuth flow, external API,
automatic classification, or ledger posting.

## Preflight result

| Gate | Result |
| --- | --- |
| Git source of truth | Public `maiziwheat520-boop/caiwu`; protected `main` at `1afb70e04aa33b4508de075d2838d9b2a6ff2977` |
| Production revision | Hermes runs `e426b488b2abb02f10ef02a61aae7ebe24c3283f` / `ledgerbridge-app:e426b48` |
| Production schema | Alembic `20260822_0004`; TEMP denied to `ledgerbridge_app` |
| Production state | API, worker, and PostgreSQL healthy; all business/evidence rows and artifact files remain empty |
| Recovery anchor | Post-hotfix backup `20260822T112755Z-e426b488b2ab` passed isolated restore |
| Branch protection | PR required; strict `secrets`, `quality`, and `compose`; admins enforced; conversations required; force-push/delete disabled |
| Write ownership | Codex owns only `LedgerBridge-Codex`; Claude remains review-only |
| Aggregate storage control | Missing: only the 50 MiB per-artifact limit exists |
| Canonical source identities | Missing: both source columns are non-blank free strings |
| Untrusted Connector isolation | Missing: Phase 2 exposes a trusted in-process SDK only |
| Durable restore coverage | Incomplete: F-4 v1 serializes Phase 1 row/function metrics and needs separate Phase 2 probes |
| Real financial evidence or credentials | None in repository or production |

The repository currently has about 29 GiB free on the Hermes system disk. The
confirmed 10 GiB published-artifact cap plus 512 MiB staging cap leaves capacity
for PostgreSQL, images, encrypted backups, and operational headroom.

## Confirmed decisions

1. Reaching a quota rejects the new ingestion and raises a sanitized alert. It
   never automatically deletes an older blob or evidence row.
2. Production defaults are 10 GiB of published artifact bytes and 512 MiB of
   aggregate staging bytes. The existing 50 MiB per-artifact cap remains.
3. Quota coordination uses a cross-process filesystem lock and actual published/
   staging byte counts so database rollback or orphan blobs cannot hide usage.
4. A quota rejection appends an `artifact.ingest_rejected` AuditEvent with a
   random intake UUID and non-secret capacity fields, and emits a structured ERROR
   log. If the database is unavailable, the log remains the fallback signal.
5. Acquisition channels and financial source systems are separate append-only
   core registries. Connector-controlled display text is not an identity.
6. Every future real Connector, including first-party code, executes through the
   isolated runner. In-process Connector implementations remain test/synthetic
   helpers only.
7. Worker/runner IPC uses a shared Unix-domain socket volume. The runner has
   `network_mode: none` and receives no database URL, artifact mount, OAuth value,
   or other production credential.
8. One runner request is limited to 10,000 records, 16 MiB of response bytes, and
   90 seconds wall time. Any limit breach rejects the whole result.
9. Backup/restore tooling continues to read v1 backups and emits a v2 restore
   report with Phase 2/platform control evidence.
10. One umbrella task owns two independently reviewable implementation PRs:
    platform controls first, isolated Connector runner second.

## Frozen invariants

- Existing ledger, audit-chain, POSTED-transition, evidence immutability,
  provenance, idempotency, and database least-privilege invariants remain intact.
- Database TEMP stays revoked from PUBLIC and every security-sensitive function
  keeps `search_path=pg_catalog` with fully qualified business relations.
- RawArtifact and SourceRecord metadata remain permanent and append-only. Quota
  pressure never authorizes deleting them or silently pruning blob bytes.
- A database row never references a missing or unverified blob.
- Exact duplicate bytes converge on one content-addressed blob. A full published
  quota does not block a verified duplicate when staging capacity is available.
- Connectors never receive database or artifact-store authority and cannot write
  AuditEvent, ImportJob, SourceRecord, JournalEntry, or Posting directly.
- Ambiguous, malformed, timed-out, crashed, or oversized Connector output creates
  no partial SourceRecord batch and no ledger transaction.
- The core revalidates every runner result; container isolation is not treated as
  proof that returned financial data is correct.
- No real evidence, OAuth secret, mailbox content, financial fixture, or customer
  identifier is committed or used by acceptance tests.
- Merge never implies production deployment. Each implementation PR and each
  production migration/deployment requires its own explicit authorization.

## Implementation slice A: platform controls

### Aggregate artifact and staging quotas

Add explicit settings and Compose wiring:

- `LEDGERBRIDGE_ARTIFACT_TOTAL_MAX_BYTES=10737418240` (10 GiB);
- `LEDGERBRIDGE_ARTIFACT_STAGING_MAX_BYTES=536870912` (512 MiB);
- `LEDGERBRIDGE_ARTIFACT_STAGING_TTL_SECONDS=3600` (one hour);
- existing `LEDGERBRIDGE_ARTIFACT_MAX_BYTES=52428800` remains the per-file cap.

Published and staging usage are separate limits. The maximum controlled disk
footprint is therefore the published cap plus the staging cap. A new, unique
blob is linked into its final path only while holding the quota lock and only if
the resulting published usage is within 10 GiB. A duplicate destination is
opened and verified through the existing single-descriptor path and consumes no
new published quota.

All staging files are visible below the private `.staging` directory. Writers
refresh the staging file timestamp as chunks are committed. Cleanup runs under
the same cross-process lock and removes only regular, non-symlink staging files
older than the configured TTL. A concurrent or stalled writer must fail closed
if its pathname disappears; it must never publish an unverifiable inode or a
database row. Unknown entries, symlinks, quota-counter overflow, unreadable
paths, or filesystem scan errors block ingestion instead of being ignored.

Quota errors are distinct from per-file size and integrity errors:

- `ARTIFACT_TOTAL_QUOTA` for a unique publication exceeding the published cap;
- `ARTIFACT_STAGING_QUOTA` for aggregate staging pressure;
- `ARTIFACT_QUOTA_STATE` when usage cannot be measured safely.

The durable rejection audit payload contains only the intake UUID, quota kind,
configured limit, observed usage, requested/reserved bytes, and machine error
code. It excludes original filenames, statement contents, raw fields, source
transaction IDs, and exception strings.

### Canonical acquisition and source registries

Migration `20260822_0004` introduces two small append-only registry tables:

- `ingest_channel` identifies how bytes entered the system, such as
  `manual_upload` or a future `microsoft_graph_attachment`;
- `source_system` identifies the financial system represented by parsed records,
  such as a future `alipay`, `wechat_pay`, or `boc`.

Identifiers are lowercase ASCII snake-case, 1-64 characters, matched by
`^[a-z][a-z0-9_]{0,63}$`. They are stable machine identities, not localized
display labels. Both tables have immutable IDs, bounded non-secret descriptions,
creation timestamps, and owner-level UPDATE/DELETE blockers. Runtime gets SELECT
only; additions are reviewed migrations, not Connector output.

`raw_artifact.source` becomes an FK to `ingest_channel.id` without changing its
stored column name in this phase. `source_record.source` becomes an FK to
`source_system.id`. The migration seeds only synthetic/manual identities needed
by existing tests and the empty production schema; no real provider integration
is implied.

The core binds a Connector manifest to one registered `source_system`. Parsed
records no longer choose their own identity per row. A conflicting or unknown
source is a contract violation and publishes no partial batch. Existing external
transaction uniqueness continues to use the canonical stored source value.

The migration is reversible only while no registry-dependent artifact or source
record exists. Data-bearing downgrade refuses rather than deleting provenance.

### Backup and restore format v2

Keep encrypted backup v1 read compatibility. Existing v1 ciphertext/sidecar
bundles must decrypt, validate hashes, restore roles before the database, and
restore the revision they contain. New backups emit
`ledgerbridge-encrypted-backup-v2`; new rehearsals emit
`ledgerbridge-restore-rehearsal-v2` with richer database/deployment metadata.

A v1 backup lacks source-side Phase 2 metrics, so compatibility must not pretend
to provide a v2-equivalent comparison. Its new restore report identifies
`source_format=v1`, lists exactly which legacy fields were compared, and records
the richer post-restore observations as unpaired evidence. A v2 backup carries
both sides and requires exact comparison for every v2 field.

At minimum v2 records and validates:

- Alembic revision, database owner, checksums, and runtime-role attributes;
- row counts for Entity, Account, JournalEntry, Posting, AuditEvent, RawArtifact,
  ImportJob, SourceRecord, and both registry tables when present;
- every expected security function by name plus exact `proconfig` search path;
- every expected public trigger by table/name plus `tgenabled='O'`;
- database TEMP denial, public-schema CREATE denial, table/sequence/function
  grants, and AuditEvent SELECT-only behavior;
- artifact digest, published/staging usage, quota configuration, and absence of
  unsafe staging entries;
- deployment manifest/revision/image label and connector-runner boundary when
  that slice is present;
- isolated resources removed and production state unchanged.

Validation derives the required object set from the restored Alembic revision;
an old backup is not judged by a future schema's object list. Unsupported future
formats fail loudly. Reports and sidecars contain no passwords, URLs with
credentials, filenames from financial evidence, or decrypted payload content.

## Implementation slice B: isolated Connector runner

### Execution topology

Add a distinct `connector-runner` service and image. It is not based on the
`x-app` environment block and therefore cannot inherit the database URL. Its
only shared resource is a named runtime volume mounted at
`/run/ledgerbridge-connector` for the Unix socket. It has:

- `network_mode: none`;
- no artifact, PostgreSQL, Docker socket, host path, secret, or OAuth mount;
- no database, migration, artifact-root, mail, or provider environment values;
- non-root UID, read-only root filesystem, `no-new-privileges`, all capabilities
  dropped, a bounded tmpfs, 128 MiB memory, 64 PIDs, and a CPU limit;
- a health check that proves the supervisor/socket loop is responsive without
  running a financial parser.

The worker mounts the socket volume but keeps its existing database and artifact
roles. The API and PostgreSQL services do not mount the socket. The production
profile contains no real Connector registration, so deploying the foundation
does not start ingesting or parsing evidence.

### Protocol and validation

Define a versioned, length-delimited protocol with explicit request IDs, operation
(`detect` or `parse`), Connector name/version, registered source-system ID,
metadata, declared artifact size, and verified SHA-256. Evidence bytes are
streamed; they are never encoded into an unbounded JSON control message. Control
frames, byte streams, record frames, and terminal responses all have independent
hard limits.

The supervisor enforces one request's 90-second deadline and 16 MiB response cap.
The core enforces at most 10,000 records and retains the Phase 2 per-field JSON,
depth, integer, currency, locator, and provenance limits. It recomputes or checks
the request byte count/digest and reconstructs typed `ParsedSourceRecord` values
instead of trusting deserialized Python objects.

Malformed framing, unknown protocol/Connector/source IDs, truncated streams,
digest mismatch, output overflow, record overflow, timeout, process exit, or
socket loss produce bounded machine errors, sanitized summaries, zero partial
records, and an observable ImportJob/audit result. A runner restart cannot reuse
a stale response for another request ID.

All production Connector manifests must select `execution_mode=runner`. The
in-process protocol remains usable only from tests and explicitly synthetic
fixtures. Enabling a real manifest is a separate reviewed change.

## Out of scope

- Real Alipay, WeChat Pay, Bank of China, CSV, XLSX, ZIP, PDF, OFX, QIF, or EML
  parser logic.
- Microsoft Graph, Outlook OAuth, refresh-token storage, mailbox polling, or
  enabling the `mail-collector` profile.
- Real/customer-derived financial bytes or credentials, including informal
  redactions and screenshots.
- Archive extraction, document OCR, password-protected files, or malware scanning.
- Classification rules, reconciliation, ReviewItem UI, suspense cleanup, tags,
  LLM calls, dashboards, or Hermes business endpoints.
- Automatic JournalEntry creation or DRAFT-to-POSTED transition from imports.
- Automatic artifact deletion, retention execution, or remote backup upload.
- Production migration/deployment without later explicit authorization.

## Acceptance tests

### Quota and filesystem behavior

- Boundary tests cover one byte below/equal/above every per-file, staging, and
  published quota; invalid/negative/overflowing configuration fails startup.
- Multiple processes writing different and identical content cannot exceed
  either aggregate limit and never publish a partial or mismatched destination.
- A full published store still accepts a verified duplicate when staging capacity
  is available; a new digest is rejected without a RawArtifact row.
- Verified orphan blobs and crash-left staging bytes are included in usage.
- Fresh/active staging is retained; only stale regular files are removed. Symlink,
  device, unreadable, path-escape, or concurrent replacement cases fail closed.
- Injected read, lock, scan, write, fsync, link, cleanup, audit, and database
  failures preserve the no-row-to-missing-blob invariant.
- Quota AuditEvent/log assertions are behavior-sensitive and prove filenames,
  raw data, exception strings, and credentials are absent.

### Registry and migration behavior

- `head -> 0003 -> head` proves table/FK/trigger/grant presence and absence, not
  only the Alembic version.
- Noncanonical IDs, unknown channels/systems, runtime registry mutation, and
  owner UPDATE/DELETE are rejected by the database.
- Existing noncanonical production data makes upgrade fail closed with no partial
  schema change. Any dependent data makes downgrade refuse.
- Connector output cannot override its registered source system. External
  identity uniqueness remains deterministic across display-name/case variants.
- All new functions pin `search_path=pg_catalog`, relations are `public.*`, TEMP
  remains denied, and `pg_temp` shadow tests cover new registry/alert paths.

### Restore v2 behavior

- Current v1 Phase 1 and Phase 2 backup fixtures remain readable and restorable
  in disposable resources, with explicit legacy comparison coverage rather than
  invented source-side Phase 2 expectations.
- A new v2 backup/restore report proves all revision-specific tables, functions,
  trigger names/enabled state, grants, TEMP/schema denial, quota state, artifacts,
  deployment revision, and cleanup.
- Deleting one required object, disabling one trigger, changing one function
  search path, granting TEMP/CREATE, altering one row count, or changing one
  artifact byte makes the rehearsal fail.
- Failed restore attempts clean only their exact disposable resources and prove
  production state unchanged.

### Runner isolation and protocol

- Compose is parsed structurally to prove the runner has no network, database/
  artifact/secret environment, privileged mount, capability, writable root, or
  excessive resource limit.
- A hostile synthetic Connector cannot resolve/reach the network, open the
  artifact root, read database/OAuth variables, write the image filesystem, or
  affect worker/PostgreSQL state.
- Framing fuzz tests cover invalid lengths, unknown versions, reordered/truncated
  frames, digest/size mismatch, oversized metadata/output, duplicate locators,
  more than 10,000 records, 90-second timeout, crash, and stale request IDs.
- Exactly-one-match routing and all Phase 2 output validation remain intact
  across the IPC boundary; every failure publishes zero partial SourceRecords.
- Runner health/restart and worker error mapping are tested in a disposable Linux
  Compose environment without enabling any real Connector.

### Quality and release gates

- Ruff, format, strict mypy, Bandit, sensitive-path scan, full-history Gitleaks,
  strict frozen dependency audit, migration round-trip, and Compose build pass.
- Coverage includes quota locking/cleanup, registries, recovery v2, IPC framing,
  runner/client failure paths, and worker integration. The threshold is not
  reduced and the omit list is not expanded.
- Linux/POSIX concurrency and socket tests run outside Windows-only skips.
- Before either implementation slice is deployed, create a fresh encrypted
  backup and pass an isolated restore rehearsal. Deployment requires a separate
  user authorization and preserves the previous tree/image.
- After an authorized deployment, repeat backup/restore and verify quotas,
  registry grants, runner isolation, service health, manifest/image revision,
  empty production data, and all security triggers enabled.

## Delivery sequence

1. Merge this documentation-only preflight through protected CI.
2. From that merge SHA, implement slice A on
   `ai/chatgpt/phase-3-platform-controls`; run a fixed-SHA self-audit and protected
   PR before any deployment decision.
3. Implement slice B from the reviewed slice-A base on
   `ai/chatgpt/phase-3-connector-runner`; run hostile-container/IPC acceptance and
   another protected PR.
4. Keep real Connector registration, OAuth, mail collection, and real evidence in
   a later task with separate decisions, credentials review, and deployment gates.

## Implementation evidence

Slice A implementation and the authorized remediation batch are complete on
`ai/chatgpt/phase-3-platform-controls`. The executable remediation commit is
`b72b229363f60de71c19933c45a7ef8bc45ee346`. It adds migration `20260822_0004`,
cross-process filesystem quota admission, separate append-only provenance
registries, structured quota rejection audit/log signals, encrypted backup/
restore format v2 with v1 read compatibility, and the runtime provenance,
artifact-manifest, verifier-pinning, connector-namespace, and restore-baseline
controls required by the fixed-SHA security review.

Local Windows gates pass Ruff, formatting, strict mypy, compileall, and 77
non-PostgreSQL tests; platform-only cases skip explicitly. The targeted
connector/backup suite passes 30 tests. Hermes disposable Linux/PostgreSQL 15
was migrated to `20260822_0004`; all 16 revision-owned triggers are enabled,
runtime TEMP is denied, runtime grants match the baseline, and direct probes
reject the pg_temp shadow attempt plus POSTED ledger mutations. The Hermes
test container could not install pytest because its isolated DNS was unavailable
and the image cache lacked the dev wheels; that probe therefore makes no remote
full-suite claim. The later protected PR CI ran the complete PostgreSQL-backed
suite successfully. Production remained on `c56b6ff` / `20260821_0003` and
received no evidence.

The deterministic remediation report is
`docs/reviews/2026-08-22-phase-3-security-remediation-codex.md`. The final
fixed-SHA security rescan is recorded in
`docs/reviews/2026-08-22-phase-3-security-scan-final-codex.md`: it has zero
unclosed Slice A findings and one low-severity same-UID inode finding explicitly
deferred to Slice B. Slice B and every deployment remain separately gated.

The follow-up CI fixes are committed as
`cdcac19de3f28c6c42db4629995b79764b48db7c`. They route unknown connector source
systems through the internal failure job before the provenance foreign key is
written and make the state-machine assertion accept PostgreSQL's invariant
evaluation order. Protected PR #14 and its push/pull-request workflows passed
all six `secrets`, `quality`, and `compose` jobs (`32568176284`,
`32568174194`), then merged into main as
`06725c3561d92630c4d15631076ba81f68371779`; the merged-main push run
`32568522459` also passed all three jobs.

## Authorized Hermes deployment addendum

The user separately authorized production deployment after the Slice A merge.
The pre-deploy `c56b6ff` backup and isolated rehearsal passed. Hermes was then
upgraded to `e73e718`/`20260822_0004`; a post-deploy rehearsal correctly exposed
that the v2 verifier did not model column-level runtime grants. Protected PR #16
(`055b6f66c5c19c99f4d9f97cc594cb014b1d5397`, merged as
`e426b488b2abb02f10ef02a61aae7ebe24c3283f`) added the exact column-grant
baseline and narrowed `import_job` UPDATE authority. The first hotfix rehearsal
also rejected a six-character image tag, so the services were rebuilt and
restarted using the valid seven-character tag `e426b48`.

Final Hermes evidence is recorded in
`docs/reviews/2026-08-22-phase-3-hermes-deployment-codex.md`: the API, worker,
and PostgreSQL are healthy; the 35-file manifest, live/ready probes, OpenAPI
404, migration head, runtime TEMP/schema denial, trigger/function/grant
baseline, artifact permissions, empty business/evidence rows, and rollback
anchors all passed. The final encrypted backup
`/srv/ai-center/backups/ledgerbridge/20260822T112755Z-e426b488b2ab` passed the
isolated restore rehearsal `restore-rehearsal-20260822T112825Z.json`. No real
financial evidence was imported. Slice B and every real data entry point remain
separately gated.

## Review findings

The initial fixed-SHA diff scan found six reportable candidates (three medium,
three low). Five Slice A findings are remediated in `b72b229`; the same-UID open
inode identity issue is explicitly deferred to Slice B. The final scan of the
base-to-remediation range is complete with no unclosed Slice A finding. Claude
capacity is preserved; a later narrow audit can focus on the five closure claims
and the eventual runner isolation/IPC framing.

## Slice B implementation evidence (Codex, 2026-08-22)

Slice B is implemented on the pushed branch `ai/chatgpt/phase-3-connector-runner`
in commits `23412d2`, `3f468ec`, `cb8f6d2`, `ebf5a42`, `6c1b6c4`, `ebc2974`, and
`991e617`. The implementation adds the versioned framed
Unix-socket protocol, bounded supervisor/client, importer error mapping, explicit
`execution_mode=runner` validation, and a distinct no-network `connector-runner`
Compose service. The protocol rejects duplicate JSON keys, binds every response
to a request ID and verified digest, and never exposes partial records after a
terminal failure. Production manifests still contain no real Connector.

Local gates pass Ruff, formatting, strict mypy, Bandit, offline lock validation,
and full pytest (`99 passed, 103 skipped`). The exact Linux/PostgreSQL replay
passes **204 tests** with **95.01%** coverage at the unchanged 95% threshold. A
disposable Hermes image built from `cb8f6d2` passed the synthetic IPC smoke and hostile network/filesystem probe;
its container was not attached to the production Compose project. Slice B has
not been deployed and no real evidence was imported.

An earlier temporary cleanup accidentally used the production Compose project
name with `down --volumes`, stopping production and removing its named volumes.
Services and schema were immediately recreated from the unchanged deployed tree.
Post-recovery health, migration, manifest, grants, trigger/function, empty-data,
and artifact-root checks passed. A new encrypted backup
`/srv/ai-center/backups/ledgerbridge/20260822T121526Z-e426b488b2ab` and isolated
rehearsal `restore-rehearsal-20260822T121556Z.json` also passed. This incident is
kept as explicit operational evidence; future temporary Compose projects must
use a unique `-p` name and never target the production tree.

The full implementation report is
`docs/reviews/2026-08-22-phase-3-runner-codex.md`. The remaining gates are the
hosted CI run for the pushed head, a narrow independent audit, protected PR
review, and separate authorization for merge or production deployment.
