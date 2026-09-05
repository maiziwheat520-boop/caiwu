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

`uv run --frozen` resolves the current worktree in about a second. Use it
rather than assembling a `PYTHONPATH` or borrowing another checkout's `.venv` —
both resolve the wrong tree and cost minutes finding out.

**A green local run means "nothing this machine can check is broken."** Say it
that way in reports. Three CI gates go unchecked here, for two different
reasons:

| CI gate | Status here |
|---|---|
| `pytest --cov-fail-under=90` | Machine gap. Coverage is 79%; the shortfall is the ~214 tests that skip without PostgreSQL. |
| `alembic upgrade/downgrade/upgrade` | Machine gap. Needs PostgreSQL. |
| `mypy src alembic tests scripts` | Broken everywhere, CI included. See below. |

### The type gate fails rather than merely being unavailable

On every branch carrying current code, that mypy command aborts on two errors
before checking anything:

- `src/ledgerbridge/bill_preprocessing.py` imports `rapidocr`, which lives in
  the `ocr` extra that CI does not install.
- `tests/` has no `__init__.py`, so those four path arguments make mypy see each
  test module under two names.

Behind the abort sit **258 errors in 23 files** as of 2026-09-06 — 3 in `src/`,
7 in `scripts/`, 248 in `tests/`, which `strict = true` covers. Nothing has
type-checked them since `8db25ab` on 2026-08-29. Making the command run again is
two lines of `[tool.mypy]` config; clearing what it then reports is the real
work, and no one has scoped it.

`uv run mypy` with no arguments does pass, because `pyproject.toml` sets
`packages = ["ledgerbridge"]` and it never reaches `tests/`. Treat it as a
narrower gate, not as CI's.

### CI reports green on `main` because `main` is stale

`origin/main` sits at `cd4edd8` (2026-08-25), **186 commits behind
`production/core`**, and predates the commit that breaks the type gate. D-009's
branch protection is intact and passing — over a branch holding none of the code
now in production.

### Reading CI from here

`gh` has no stored login, but the machine's Git credential answers for it:

```
export GH_TOKEN="$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill | sed -n 's/^password=//p')"
gh run list --branch production/core --limit 5
```

`gh auth login --with-token` rejects that same credential for missing the
`read:org` scope; `GH_TOKEN` skips the check. Reading runs and logs works.
A persistent login needs `gh auth login` run by the user.

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
