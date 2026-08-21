# Project status

Updated: 2026-08-21

## Current phase

Phase 0 is approved, merged to `main`, and deployed on Hermes at merge commit
`61ad9103d68d10a07191c7ad00a4fbb8953deddd`. Phase 1 is in preflight only;
no Ledger Core schema implementation has begun.

## Completed

- Phase 0 scaffold and Claude blocker/high remediation.
- Private GitHub source of truth: `maiziwheat520-boop/caiwu`.
- PR #1 merged with full review history and Claude verdict
  `APPROVED FOR PHASE 1 WITH FOLLOW-UPS`.
- Push and pull-request CI passed through the final review-report commit.
- Hermes runs API and worker from `ledgerbridge-app:61ad910`, with
  `DEPLOYED_REVISION=61ad9103d68d10a07191c7ad00a4fbb8953deddd`.
- PostgreSQL data checksums are on; migration upgrade/downgrade/upgrade passed.
- API and worker run as UID 10001; API artifacts are read-only, worker artifacts
  are writable, and API is published only on `127.0.0.1:8650`.
- F-1 shared-worktree risk is closed by separate private clones and identities.

## Ownership checkpoint

- Recorded: 2026-08-21
- Common base HEAD: `61ad9103d68d10a07191c7ad00a4fbb8953deddd`
- Codex implementation clone: `G:\我的云端硬盘\AI\LedgerBridge-Codex`
- Codex branch: `ai/chatgpt/phase-1-prep`
- Codex identity: `Codex <codex@ledgerbridge.local>`
- Claude review-only clone: `G:\我的云端硬盘\AI\LedgerBridge-Claude`
- Claude branch: `main` (read-only unless the user explicitly transfers ownership)
- Claude identity when explicitly authorized to commit: `Claude <claude@ledgerbridge.local>`
- Retired shared clone: `G:\我的云端硬盘\AI\LedgerBridge` (do not write)

## Active implementation owner

Codex, in the Codex clone and only on `ai/chatgpt/*` branches.

## Review owner

Claude, in the Claude clone. Default mode remains read-only independent review.

## Next task

Submit and review the Phase 1 preflight task card. After it is merged, create
`ai/chatgpt/phase-1-core-schema` and implement the frozen Ledger Core as one
reviewed, genuinely reversible Alembic migration with database-level invariants.

## Blocking decisions

None. GitHub Free cannot enforce branch protection on this private repository;
PR/CI/one-writer controls remain mandatory manual gates. F-6 becomes blocking
if a second human gains write access, real financial/OAuth data enters, or any
direct push bypasses PR.
