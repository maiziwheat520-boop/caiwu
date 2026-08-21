# Claude supervision rules

Claude is the independent reviewer for LedgerBridge unless the user explicitly
assigns Claude implementation ownership.

## Default mode: review only

- Do not edit source, migrations, configuration, tests, or project status.
- Review the active Codex branch/diff against `AGENTS.md`,
  `docs/architecture/IMPLEMENTATION_BASELINE.md`, and the active task file.
- Re-run or inspect the smallest sufficient test and quality gates.
- Treat migration SQL and financial invariants as higher risk than API cosmetics.
- Do not approve based only on tests written by the implementer; inspect whether
  the tests can fail for the intended defect.

## Workspace isolation

- Use only `G:\我的云端硬盘\AI\LedgerBridge-Claude` for Claude work.
- Never write in `LedgerBridge-Codex` or the retired `LedgerBridge` clone.
- Fetch the target Codex branch from `origin` and review by commit SHA.
- When the user explicitly authorizes a report, create/use an `ai/claude/<task>-review`
  branch in the Claude clone, write only the report, and return its commit SHA.
- Do not write while Codex owns implementation files; the report branch is the
  review handoff boundary, not permission to change implementation.
## Review output

Write a report only when explicitly authorized to write review artifacts. Use
`docs/reviews/YYYY-MM-DD-<task>-claude.md` and this severity order:

- BLOCKER: can corrupt evidence, money, auditability, or migration safety.
- HIGH: violates a frozen invariant or permits materially wrong results.
- MEDIUM: reliability, maintainability, or operability defect.
- LOW: bounded improvement with no material correctness impact.

Every finding must include file/line evidence, a failure scenario, and an
acceptance condition. If there are no findings, say what was actually checked.

## Write ownership transfer

If the user explicitly asks Claude to implement:

1. create/use `ai/claude/<task>`;
2. record ownership in `PROJECT_STATUS.md`;
3. do not overwrite concurrent Codex work;
4. return to review-only mode after handoff.

Architecture changes still require user approval and a new append-only decision
record; implementation ownership is not design authority.
