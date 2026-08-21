# Codex–Claude implementation and supervision workflow

## Roles

| Stage | Codex | Claude |
|---|---|---|
| Implement | Owns files on `ai/chatgpt/<task>` | Read-only |
| Self-check | Runs tests and records evidence | May inspect progress |
| Independent review | Answers findings | Reviews diff, migration, and tests |
| Fix | Resolves accepted findings | Verifies only |
| Merge readiness | Produces handoff | Must have no unresolved BLOCKER/HIGH |

The user can reverse ownership explicitly. When reversed, Claude uses
`ai/claude/<task>` and Codex becomes reviewer. Never allow concurrent writes to
the same task.

## Workspace isolation and handoff

- Codex implementation clone: `G:\我的云端硬盘\AI\LedgerBridge-Codex`.
- Claude review clone: `G:\我的云端硬盘\AI\LedgerBridge-Claude`.
- Retired shared clone: `G:\我的云端硬盘\AI\LedgerBridge`; never write it.
- Each clone has its own `.git`, branch, index, and local Git identity.
- `PROJECT_STATUS.md` records the ownership timestamp and common base HEAD.
- Reviewer output is handed off by an explicit `ai/claude/*-review` commit; the
  implementer consumes that commit only after review writing has stopped.
## Task lifecycle

1. Copy `docs/tasks/TEMPLATE.md` to a dated task file.
2. Record scope, owned files, invariants, and acceptance tests before coding.
3. Record implementation evidence, not a narrative of every command.
4. Reviewer records findings with severity and concrete failure scenarios.
5. Implementer resolves or explicitly disputes findings with evidence.
6. Update `PROJECT_STATUS.md` and the handoff before changing model or task.

## Architecture control

The implementation baseline is frozen. Discovering a design issue authorizes a
proposal, not a unilateral schema change. Capture the issue and impact, then wait
for the user's decision. Approved replacements are append-only decisions that
state which earlier decision they supersede.
