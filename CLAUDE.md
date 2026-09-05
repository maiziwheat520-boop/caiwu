# Claude working rules for LedgerBridge

## Where the code is

`D:\repos\LedgerBridge-Codex` (Core) and `D:\repos\LedgerBridge-Web` (Web), with
short-lived worktrees under `D:\repos\_worktrees\`. Never create a Git
repository, clone, or worktree anywhere under `G:\我的云端硬盘` — Git's
small-file I/O and the Drive virtual filesystem deadlock the machine, which is
why D-018 exists. The Drive workspace holds rules, decisions and records; it
does not hold code.

`production/core` and `production/web` always point at the live revision and are
the only integration baselines. Branch from those, never from an old feature or
release branch.

## Default mode: own the sub-feature end to end

When the user hands over a sub-feature, this conversation carries it to
production and verifies it there (D-033). That includes integration, backup,
deploy, live verification and the record. "Handed to the main task" is not a
finished state. Stop earlier only when the user says to, or when the workspace
is in a product test window.

Read `auto/ledgerbridge-release/README.md` in the Drive workspace before
integrating or releasing. One release lock covers the whole finance project;
`busy` means wait, not preempt.

Review-only mode still exists, but it is now something the user asks for rather
than the default.

## Gates

Run them the way CI does, which needs no setup:

```
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev ruff format --check .
uv run --frozen --extra dev pytest -q
```

Do not hand-assemble a `PYTHONPATH` or borrow another checkout's `.venv`. `uv
run --frozen` resolves the current worktree in about a second.

**Three of CI's gates cannot pass on a developer machine.** Know this before you
conclude the tree is broken, and before you claim it is clean:

| CI runs | Here |
|---|---|
| `mypy src alembic tests scripts` | Fails to complete: `rapidocr` is in the `ocr` extra that CI does not install, and those four paths make mypy see every test module under two names. `uv run mypy` does complete and currently reports 4 errors that predate current work. |
| `pytest --cov-fail-under=90` | Coverage here is 79%. The missing points are the ~214 tests that skip without PostgreSQL. |
| `alembic upgrade/downgrade/upgrade` | Needs PostgreSQL. |

So a green local run means "nothing this machine can check is broken", not "CI
will pass". Say it that way in reports. Closing the gap means giving this
machine a PostgreSQL to test against and making one mypy invocation work in both
places; neither is done.

`gh` is not authenticated here, so CI results cannot be read from this machine
either. Do not assert that CI passed.

## Deploying Core

```
uv run --frozen --extra dev python scripts/deploy_core.py --host <user@address> --dry-run
```

Drop `--dry-run` to release. It reads the live revision and the compose file set
from the host, refuses a dirty tree, an already-deployed revision, or a branch
that does not contain the live revision, takes a backup, switches atomically,
waits for health, and rolls back to the previously live revision on any failure.

It does not delete files on the host; a release that removes one refuses and
says to deploy by hand.

After it succeeds: push `production/core`, record the release in the Drive
workspace's `work.md`, then release the lock.

## Higher-risk changes

Migrations, roles, encryption, authorization and real financial data still
require the full path in D-011/D-028: encrypted backup, isolated restore
rehearsal, rollback preparation and the existing security review. Every
migration must register its schema revision in `MYBANK_CUTOVER_SCHEMA_REVISIONS`
or backups fail closed — the gate catches this only after the migration is live.

Treat migration SQL and financial invariants as higher risk than API cosmetics.
Do not accept tests written by the implementer at face value; check whether they
can fail for the intended defect.

## When asked to review

Write a report only when explicitly authorized. Use
`docs/reviews/YYYY-MM-DD-<task>-claude.md` and this severity order:

- BLOCKER: can corrupt evidence, money, auditability, or migration safety.
- HIGH: violates a frozen invariant or permits materially wrong results.
- MEDIUM: reliability, maintainability, or operability defect.
- LOW: bounded improvement with no material correctness impact.

Every finding needs file/line evidence, a failure scenario, and an acceptance
condition. With no findings, say what was actually checked.

Architecture changes still require user approval and a new append-only decision
record; implementation ownership is not design authority.
