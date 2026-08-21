# Project status

Updated: 2026-08-21

## Current phase

Phase 0 is approved, merged, and deployed on Hermes at
`61ad9103d68d10a07191c7ad00a4fbb8953deddd`. Phase 1 Ledger Core is implemented
on `ai/chatgpt/phase-1-core-schema` in PR #5. The first independent Claude review
at review commit `88d4e775a42e924998173590a0d91f34830d1fbc` returned CHANGES REQUIRED
(1 BLOCKER, 4 HIGH); those findings were remediated through `3e1e6fc`, whose
push and pull-request CI runs both passed.

At the user's direction, Codex then performed the final self-audit to conserve
Claude capacity. It found and fixed one HIGH cross-entity identity bypass and one
LOW deployment-manifest symlink exclusion-order issue. The final working tree
passed 51 tests and all local/Hermes gates. Self-audit
commit `a094b1abfd56c2eb625eae95275bb875e698c3b3` was pushed; push run
`32468672289` and PR run `32468676815` passed secrets, quality, and compose
(6/6 jobs). Phase 1 is ready for explicit merge authorization. Claude's separate
clone and immutable report remain a later independent-audit entry point. Phase 1
has not been merged or deployed.

## Completed

- Phase 0 scaffold and Claude blocker/high remediation.
- Private GitHub source of truth: `maiziwheat520-boop/caiwu`.
- PR #1 merged Phase 0; PR #4 merged the Phase 1 preflight and Claude report at
  merge commit `55f88dd9f8125d34a8952e5af56844c0033d7b27`.
- F-1 shared-worktree risk is closed by separate private clones and identities.
- Phase 1 implements Entity, Account, JournalEntry, Posting, AuditEvent, the
  append-only audit function, POSTED immutability, entity boundaries, deferred
  per-currency balance checks, and POSTED-only balance queries.
- PR #5 head `3e1e6fcb58258abf188ba57e94c736431b18a339` passed all six jobs across
  its push and pull-request CI runs after the first-review remediation.
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
  APPROVED FOR MERGE with no open findings; explicit user authorization is still required.
- Production Hermes remains on Phase 0: API, worker, and PostgreSQL healthy;
  API is loopback-only and no production volume or schema was changed.

## Ownership checkpoint

- Recorded: 2026-08-21
- Implementation base: `55f88dd9f8125d34a8952e5af56844c0033d7b27`
- Codex implementation clone: `G:\我的云端硬盘\AI\LedgerBridge-Codex`
- Codex branch: `ai/chatgpt/phase-1-core-schema`
- Codex identity: `Codex <codex@ledgerbridge.local>`
- Claude review-only clone: `G:\我的云端硬盘\AI\LedgerBridge-Claude`
- Claude identity when explicitly authorized to commit:
  `Claude <claude@ledgerbridge.local>`
- Retired shared clone: `G:\我的云端硬盘\AI\LedgerBridge` (do not write)

## Active implementation owner

Codex, in the Codex clone and only on `ai/chatgpt/phase-1-core-schema`.

## Review owner

Codex owns the current final self-audit. Claude remains the independent review
option in the separate Claude clone, but the user deferred that run to conserve
quota. Any later Claude audit must review a clean, fixed full SHA and write only
a new review report.

## Next task

Request separate merge authorization for PR #5 once this documentation-only
evidence commit passes CI. Preserve the Claude audit hook for later; do not deploy
Phase 1 to production without separate authorization.

## Blocking decisions

None. GitHub Free cannot enforce branch protection on this private repository;
PR/CI/one-writer controls remain mandatory manual gates. F-6 becomes blocking
if a second human gains write access, real financial/OAuth data enters, or any
direct push bypasses PR.
