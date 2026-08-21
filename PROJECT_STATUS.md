# Project status

Updated: 2026-08-21

## Current phase

Phase 0 is approved, merged, and deployed on Hermes at
`61ad9103d68d10a07191c7ad00a4fbb8953deddd`. Phase 1 Ledger Core implementation
is complete on `ai/chatgpt/phase-1-core-schema`; GitHub CI and concentrated
independent Claude review are the remaining merge gates. Phase 1 has not been
deployed to the production Hermes stack.

## Completed

- Phase 0 scaffold and Claude blocker/high remediation.
- Private GitHub source of truth: `maiziwheat520-boop/caiwu`.
- PR #1 merged Phase 0; PR #4 merged the Phase 1 preflight and Claude report at
  merge commit `55f88dd9f8125d34a8952e5af56844c0033d7b27`.
- F-1 shared-worktree risk is closed by separate private clones and identities.
- Phase 1 implements Entity, Account, JournalEntry, Posting, AuditEvent, the
  append-only audit function, POSTED immutability, entity boundaries, deferred
  per-currency balance checks, and POSTED-only balance queries.
- F-2 dependency locking, F-3 coverage hardening, F-5 operational cleanup, and
  F-7 real migration downgrade/upgrade assertions are implemented locally.
- Hermes isolated PostgreSQL 15 acceptance run: 31 tests passed and coverage was
  97.53%; Ruff, formatting, mypy, Bandit, strict dependency audit, manifest
  verification, locked image build, revision label, worker heartbeat, API ready,
  and OpenAPI 404 checks passed.
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

Claude, in the Claude clone. Default mode remains read-only independent review.
Create the review branch with `--no-track` after the implementation head is
pushed and fixed by SHA.

## Next task

Commit and push the complete Phase 1 implementation, open its pull request, wait
for all push/PR CI checks, then give Claude one concentrated review of the full
implementation rather than piecemeal reviews. Resolve any BLOCKER/HIGH findings
before merge. Do not deploy Phase 1 to production before merge and review approval.

## Blocking decisions

None. GitHub Free cannot enforce branch protection on this private repository;
PR/CI/one-writer controls remain mandatory manual gates. F-6 becomes blocking
if a second human gains write access, real financial/OAuth data enters, or any
direct push bypasses PR.
