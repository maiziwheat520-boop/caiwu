# Project status

Updated: 2026-08-21

## Current phase

Phase 1 Ledger Core is merged through PR #5 at merge commit
`2028e3a99abe5e20c842a95ec22f2931878d39ee` and deployed on Hermes as
`ledgerbridge-app:2028e3a`. The reviewed PR head was
`e739088ad8f4d0eec91fda6e1e5ab3c268b1b2e6`.

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

## Completed

- Phase 0 scaffold and Claude blocker/high remediation.
- Private GitHub source of truth: `maiziwheat520-boop/caiwu`.
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

## Ownership checkpoint

- Recorded: 2026-08-21
- Deployment base: `2028e3a99abe5e20c842a95ec22f2931878d39ee`
- Codex implementation clone: `G:\我的云端硬盘\AI\LedgerBridge-Codex`
- Codex branch: `ai/chatgpt/phase-1-deployment-record`
- Codex identity: `Codex <codex@ledgerbridge.local>`
- Claude review-only clone: `G:\我的云端硬盘\AI\LedgerBridge-Claude`
- Claude identity when explicitly authorized to commit:
  `Claude <claude@ledgerbridge.local>`
- Retired shared clone: `G:\我的云端硬盘\AI\LedgerBridge` (do not write)

## Active implementation owner

Codex, in the Codex clone and only on `ai/chatgpt/phase-1-deployment-record`.

## Review owner

Codex completed the final self-audit and production deployment record. Claude
remains the independent review option in the separate Claude clone, but the user
deferred that run to conserve quota. Any later Claude audit must review a clean,
fixed full SHA and write only a new review report.

## Next task

Complete F-4 backup/restore automation and a restore-to-empty rehearsal before
Phase 2 introduces evidence ingestion. Preserve the Claude audit hook for later;
Phase 2 scope still requires its own task card and explicit implementation handoff.

## Blocking decisions

None. GitHub Free cannot enforce branch protection on this private repository;
PR/CI/one-writer controls remain mandatory manual gates. F-6 becomes blocking
if a second human gains write access, real financial/OAuth data enters, or any
direct push bypasses PR.
