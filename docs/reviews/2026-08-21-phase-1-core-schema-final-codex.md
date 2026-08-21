# LedgerBridge Phase 1 final Codex self-audit

- Date: 2026-08-21
- Reviewer: Codex
- Repository: `maiziwheat520-boop/caiwu`
- Pull request: #5
- Base revision: `55f88dd9f8125d34a8952e5af56844c0033d7b27`
- Pre-audit head: `3e1e6fcb58258abf188ba57e94c736431b18a339`
- Reviewed candidate: pre-audit head plus the working-tree remediation recorded below
- Verdict: **APPROVED FOR FINAL COMMIT AND CI**
- Open findings: **0 blocker, 0 high, 0 medium, 0 low**

Final merge approval remains conditional on both GitHub push and pull-request CI runs
passing for the commit that contains this report and the self-audit remediation.

## Threat model

Assets reviewed:

- entity-separated ledger history and posted aggregates;
- POSTED-entry and posting immutability;
- the append-only AuditEvent hash chain;
- owner/migration credentials versus the least-privileged runtime login;
- deployment bundle revision and file-set integrity.

Relevant attacker capabilities:

- a compromised API/worker process or leaked `ledgerbridge_app` credential;
- a concurrent runtime client attempting to exploit transaction ordering;
- a contributor or deployment-tree writer attempting to conceal drift;
- malformed ledger writes within the SQL privileges intentionally granted to the runtime role.

Trust boundaries and assumptions:

- Hermes host administrators, the PostgreSQL owner, and the migration runner are trusted;
- the runtime role is not trusted with DDL, trigger control, TRUNCATE, direct AuditEvent
  writes, owner-role membership, or owner credentials;
- Phase 1 has no financial business endpoints, OAuth flow, or production data migration;
- Git review and the image revision label are authenticity anchors; the manifest is an
  unsigned drift detector.

## Coverage

The audit reviewed every changed executable surface in the Phase 1 range:

- Alembic schema, functions, triggers, grants, downgrade, and runtime-role checks;
- ORM models and posted-balance queries;
- database connection identity and pool behavior;
- PostgreSQL bootstrap, Compose, Dockerfile, CI, and dependency locking;
- deployment-manifest creation and verification;
- API health/OpenAPI exposure and worker heartbeat;
- migration, concurrency, permissions, business-invariant, and deployment tests.

The immutable first Claude report was also rechecked finding by finding. Its blocker and
four high-severity findings remain closed by the existing remediation.

## Self-audit findings fixed

### CDX-H1 — Mutable identity fields could create a cross-entity posted entry

- Severity: High
- Status: Fixed

Before this self-audit, entity equality was checked when a Posting row was inserted or
updated, but existing Posting rows were not revalidated when a DRAFT Account or
JournalEntry changed entity. A runtime writer could therefore:

1. create a balanced DRAFT entry for entity A;
2. move a non-primary Account to entity B, or move the JournalEntry and its primary
   account reference to entity B;
3. transition the entry to POSTED.

That sequence could preserve balanced amounts while crossing the entity isolation
boundary and corrupting entity-scoped aggregates.

Remediation:

- Account `entity_id` is immutable from creation
  (`alembic/versions/20260821_0002_ledger_core.py:286`);
- JournalEntry `entity_id` is immutable from creation
  (`alembic/versions/20260821_0002_ledger_core.py:478`);
- the POSTED-completeness constraint performs a defense-in-depth scan for any posting
  account belonging to another entity
  (`alembic/versions/20260821_0002_ledger_core.py:643`);
- regression tests cover both zero-posting identity mutation and pre-existing drift
  (`tests/test_ledger_core.py:561`, `tests/test_ledger_core.py:596`).

The user explicitly selected permanent identity immutability over a more complex
conditional-locking design. Drafts created under the wrong entity must be deleted and
recreated.

### CDX-L1 — A symlink resolving to the manifest could be excluded before rejection

- Severity: Low
- Status: Fixed

The deployment file walker previously compared `candidate.resolve()` with the manifest
path before rejecting a symlink. A non-excluded path that resolved to the manifest could
therefore disappear from the manifest inventory instead of failing closed.

The walker now skips intentionally excluded directories first, then rejects symlink
candidates before manifest/filename exclusions
(`scripts/deployment_manifest.py:44`). A dedicated regression test uses a symlink whose
target is `MANIFEST.sha256`
(`tests/test_deployment_manifest.py:80`).

## Prior Claude findings

| Finding | Final status |
|---|---|
| P1-B1 transactional `SET ROLE` rollback/pool reuse | Closed: runtime logs in directly as `ledgerbridge_app`; no `SET ROLE` path |
| P1-H1 owner/superuser application login | Closed: runtime and migration URLs/services are separate and tested |
| P1-H2 duplicate reversal | Closed: partial unique index permits one reversal per original |
| P1-H3 mutable Account entity/class after posted use | Closed and strengthened: entity identity is immutable from creation; class freezes after POSTED use |
| P1-H4 insensitive OLD-entry move test | Closed: the target remains balanced while the source alone becomes unbalanced |
| M1–M7 | Closed by concurrency, role-bootstrap, currency, migration, coverage, manifest, and lifecycle remediation |
| M8 post-transition audit-event binding | Intentionally deferred to the workflow/API phase; Phase 1 creation authorization remains mandatory |

## Verification evidence

Final isolated PostgreSQL 15 run on Hermes:

- 51 tests passed;
- 99.31% statement coverage against a 95% gate;
- Ruff check and format check passed;
- strict mypy passed;
- Bandit passed;
- upgrade → downgrade to base → upgrade passed;
- strict locked dependency audit reported no known vulnerabilities;
- local sensitive-path scan passed.

During the self-audit, an isolated production-style image also built successfully and
API live/ready, OpenAPI 404, worker heartbeat, non-owner
`session_user/current_user=ledgerbridge_app`, UID 10001, and revision-label checks
passed. All temporary containers, networks, images, archives, and directories were
removed. Production Hermes remained on Phase 0 image `ledgerbridge-app:61ad910`;
API, worker, and PostgreSQL stayed healthy and no production schema or volume changed.

## Tooling limitation

The Codex Security workbench could not create a scan because its Windows launcher
decoded the Chinese workspace path with GBK and raised `UnicodeDecodeError` before
returning a scan ID. Codex Security Access also reported `not_granted`. Therefore no
canonical workbench scan artifact or TAC-backed attestation exists for this review.

The review continued with direct full-diff inspection, an explicit threat model,
targeted exploit reasoning, new regression tests, and isolated PostgreSQL/container
verification. This tooling limitation does not reduce the repository/test coverage
listed above, but it is preserved so a later Claude or Codex Security audit can reproduce
the review without assuming a workbench scan occurred.

## Recommendation

Commit and push the two self-audit remediations and this report, then require both GitHub
CI runs to pass at the new immutable SHA. Do not merge or deploy until that evidence is
recorded. Claude's review-only clone and the original immutable Claude report remain
available as a later independent-audit entry point.
