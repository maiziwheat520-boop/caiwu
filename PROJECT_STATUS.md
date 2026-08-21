# Project status

Updated: 2026-08-21

## Current phase

Phase 0 is approved, merged, and deployed on Hermes at
`61ad9103d68d10a07191c7ad00a4fbb8953deddd`. Phase 1 Ledger Core is implemented
on `ai/chatgpt/phase-1-core-schema` in PR #5. The first independent Claude review
at review commit `88d4e775a42e924998173590a0d91f34830d1fbc` returned CHANGES REQUIRED
(1 BLOCKER, 4 HIGH); Codex has completed the concentrated remediation locally.
The remediation commit/push, fresh CI, and one final fixed-SHA Claude audit remain
merge gates. Phase 1 has not been deployed to the production Hermes stack.

## Completed

- Phase 0 scaffold and Claude blocker/high remediation.
- Private GitHub source of truth: `maiziwheat520-boop/caiwu`.
- PR #1 merged Phase 0; PR #4 merged the Phase 1 preflight and Claude report at
  merge commit `55f88dd9f8125d34a8952e5af56844c0033d7b27`.
- F-1 shared-worktree risk is closed by separate private clones and identities.
- Phase 1 implements Entity, Account, JournalEntry, Posting, AuditEvent, the
  append-only audit function, POSTED immutability, entity boundaries, deferred
  per-currency balance checks, and POSTED-only balance queries.
- PR #5 head `80d01ee3afe9f6b9954e8a93ec206f174d73880f` passed both push and pull-request
  CI before the independent review.
- Review remediation removes transactional `SET ROLE`; API/worker now log in as
  a separate non-owner runtime LOGIN while migrations use an owner-only one-shot
  service. Tests prove pool reuse and `RESET ROLE` cannot regain owner power.
- Database guards now reject duplicate reversals, changes to entity/class on an
  Account used by POSTED history, and audit-chain forks under stale snapshots.
- OLD/NEW posting-move and per-currency tests are behavior-sensitive; deployment
  manifest root exclusions, unsafe paths, worker heartbeat placement, uv wheel
  hash locking, role bootstrap/downgrade behavior, and lifecycle documentation
  are hardened.
- Latest Hermes isolated PostgreSQL 15 acceptance run: 48 tests passed and
  coverage was 99.31%; Ruff, formatting, mypy, and Bandit passed. Local
  sensitive-path scanning passed; Linux strict pip-audit found no vulnerabilities.
  The hash-locked image/Compose build and isolated API ready/live/OpenAPI-404,
  direct runtime identity, worker heartbeat, and migration-head smoke tests passed.
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

Claude, in the Claude clone. Conserve Claude capacity until the remediation head
is committed, pushed, fixed by full SHA, and CI is green. The final prompt must
first require Claude to remove its own temporary defect-injection changes or
review from a fresh clean clone; it may write only a new review report.

## Next task

Review the final diff, then request authorization for the remediation commit and
push to PR #5. Wait for both CI runs before issuing one concentrated final
Claude audit.
Do not deploy Phase 1 to production before merge and review approval.

## Blocking decisions

None. GitHub Free cannot enforce branch protection on this private repository;
PR/CI/one-writer controls remain mandatory manual gates. F-6 becomes blocking
if a second human gains write access, real financial/OAuth data enters, or any
direct push bypasses PR.
