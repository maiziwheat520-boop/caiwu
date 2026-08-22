# Project status

Updated: 2026-08-22

## Current phase

Phase 1 Ledger Core plus F-4 backup/restore automation is merged through PR #7
at merge commit `0c5616f648d720da88dd37deac94610486e7e611` and deployed on
Hermes as `ledgerbridge-app:0c5616f`. The F-4 reviewed PR head was
`958a26106a3118afcdf0c27ec3b70ccb9b479733`.

The first independent Claude review at
`88d4e775a42e924998173590a0d91f34830d1fbc` returned CHANGES REQUIRED
(1 BLOCKER, 4 HIGH). Codex remediated those findings and, at the user's direction,
completed the final self-audit to conserve Claude capacity. All four push/PR CI
runs for the executable and evidence heads passed their six jobs.

After separate deployment authorization, Hermes was upgraded in place from
`20260821_0001` to `20260821_0002`. The legacy `ledgerbridge` role remains the
migration owner; API and worker now log in directly as the unprivileged
`ledgerbridge_app` role. The database and all five Phase 1 business tables remain
empty. The previous image, deployment tree, database dump, role dump, and manifest
bundle remain available as rollback anchors. Claude's separate clone and immutable
report remain a later independent-audit entry point for the fixed SHA.

F-4 backup/restore automation is complete. PR #7 and the merged-main push passed
the `secrets`, `quality`, and `compose` jobs. The merged SHA is deployed, and
the final encrypted backup plus isolated restore-to-empty rehearsal passed:
roles were restored before the database, runtime grants were non-empty,
migration/checksum/object/row/artifact/deployment integrity matched, disposable
resources were removed, and production remained unchanged. The previous
deployment tree remains available for rollback. The GitHub repository is now
public after a full-history secret and source-boundary audit found no credential
or key exposure.

Phase 2 Evidence and Import is implemented and fixed-SHA self-audited on
`ai/chatgpt/phase-2-evidence-import` from preflight merge
`232378ef70f3cfa24324dc6add61ce6089d107b4`. The final reviewed executable SHA is
`b092eb88772d30964524c7475ee96b0ccc86c395`. Migration `20260821_0003`, the
immutable evidence models, durable content-addressed store, narrow Connector SDK,
idempotent import orchestration, SourceRecord FK, and M8 DRAFT-to-POSTED audit
binding are complete. Security review closed a PostgreSQL temporary-schema
shadowing path and importer provenance/mutability defects. Isolated PostgreSQL 15
acceptance passed 109 tests at 96.88% coverage on the hardened database/import
commit; the final durability-only patch passed local gates and a Linux production-
image artifact smoke. PR #10 push and pull-request CI passed `secrets`, `quality`,
and `compose` (6/6 jobs), including the complete 110-test suite. A pre-existing
Phase 1 metadata drift remains documented for follow-up. Real
parsers, OAuth, production evidence, migration, and deployment remain out of scope.

PR #10 was subsequently merged as
`23bfbd3bcc79068c3744dab05a961d497590ec8e`. Claude's independent report commit
`cc07e08aa264c5a2aa42d9b422a049cf5c926ee9` returned CHANGES REQUIRED with
1 BLOCKER, 1 HIGH, 8 MEDIUM, and 8 LOW findings. Codex closed the `pg_temp`
ledger invariant bypass, artifact verification TOCTOU, and applicable medium/low
items in executable `40fcd022ae6d3127aa7bdc17afecb6b1a159cda0`.
Disposable Hermes PostgreSQL 15 validation passed an empty migration
`head -> base -> head` round trip followed by 128/128 tests at 95.79% coverage;
Ruff, formatting, full strict mypy, Bandit, Compose parsing, and a production
image build/import smoke passed. PR #11 merged as
`c56b6ffdde9f723efe1792ae1312ec8795bba165`; its final push/PR CI and the
merged-main run passed `secrets`, `quality`, and `compose`.

After separate deployment authorization, Hermes was upgraded to image
`ledgerbridge-app:c56b6ff` and Alembic `20260821_0003`. A pre-deploy encrypted
backup passed isolated restore before the change. Post-deploy checks prove
database TEMP is denied to `ledgerbridge_app`, all 14 security functions pin
`search_path=pg_catalog`, all 13 public triggers are enabled, and all eight
business/evidence tables plus the artifact volume remain empty. A new encrypted
backup also passed isolated restore. No real evidence ingestion occurred.

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

## Ownership checkpoint

- Recorded: 2026-08-21
- Deployment revision: `c56b6ffdde9f723efe1792ae1312ec8795bba165`
- Codex implementation clone: `G:\我的云端硬盘\AI\LedgerBridge-Codex`
- Codex branch: `ai/chatgpt/phase-2-deployment-record`
- Codex identity: `Codex <codex@ledgerbridge.local>`
- Claude review-only clone: `G:\我的云端硬盘\AI\LedgerBridge-Claude`
- Claude identity when explicitly authorized to commit:
  `Claude <claude@ledgerbridge.local>`
- Retired shared clone: `G:\我的云端硬盘\AI\LedgerBridge` (do not write)

## Active implementation owner

Codex, in the Codex clone and only on `ai/chatgpt/phase-2-deployment-record` for
the deployment evidence record. Claude remains read-only.

## Review owner

Claude completed the independent fixed-SHA audit in the separate clone and wrote
only its report. Codex published the finding-by-finding response, merged the
validated remediation, and preserved Claude's remaining quota. A later recheck
can be limited to the BLOCKER/HIGH closures and report addendum.

## Next task

Publish and merge the deployment evidence record. Phase 3 planning may then
address aggregate artifact quotas, canonical source identities, out-of-process
untrusted connectors, and stronger durable restore-report coverage. Do not ingest
real evidence or enable the mail collector without a separate approved task.

## Blocking decisions

None for Phase 2 remediation or deployment. F-4 and F-6 are closed. Real-data
ingestion and Phase 3 connector/OAuth work still require explicit authorization.
