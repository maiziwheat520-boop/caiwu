# Task: Local single-user mode

- Status: proposed
- Implementation owner: Claude (`ai/claude/local-single-user-mode`)
- Review owner: user
- Branch: `ai/claude/local-single-user-mode`, worktree
  `D:\repos\_worktrees\ledgerbridge-local-mode-20260911`, based on `448d700`
- Owned files: `docs/tasks/2026-09-11-local-single-user-mode.md`,
  `scripts/local_mode.py` (new), `docker-compose.local.yml` (new),
  `.env.local.example` (new), `src/ledgerbridge/local_mode.py` (new).
  No Alembic migration, no change to `config.py` defaults, no change to any
  existing route, service, or production manifest.

## Why this exists

The user wants the ledger usable on one machine without the production
deployment. A previous attempt built that as a separate program in a separate
repository (`D:\repos\finance-desk`, 30 commits, no remote, never recorded in
`work.md` or any handoff). That violates D-015 and D-028: one product, one
authoritative fact layer, no second set of real business facts. It also
duplicated work that already exists here — production already holds 11
statements and 2,447 transaction facts from the same company sources.

This task replaces that attempt. Nothing new is invented about what a financial
fact *is*; the local mode only changes how the existing Core is started and who
is allowed to talk to it.

## Goal

One command starts Core against a local PostgreSQL on loopback, and a local
read-only UI shows the same accounts, statements, facts and reports that the
deployed Core would show — with no mTLS proxy, no Passkey, no Hermes, and no
cloud host.

## Key finding: this is a profile, not an architecture change

`Settings` already carries every switch this needs, and they are already
default-off:

| Setting | Production | Local mode |
|---|---|---|
| `internal_read_backend` | `database` | `database` (same) |
| `internal_read_transport` | `unix-mtls-proxy` | `disabled` |
| `internal_read_operational_gate` | `r1-production-v1` | `closed` |
| `enable_internal_read_api` | `true` | `true` |
| `env` | `production` | `development` |

`scripts/r1_synthetic_demo.py` already establishes the shape of a loopback-only
launcher with a fixed local principal. Local mode is that launcher pointed at a
real local database instead of the packaged fixture. No new fact model, no new
table, no new writer.

## In scope

- `docker-compose.local.yml`: postgres + migrate only, bound to `127.0.0.1`,
  a local volume, no published port beyond loopback.
- `scripts/local_mode.py`: start Core with the local profile, refusing to run
  if any production-only setting is present (see Frozen invariants).
- A local read-only UI over the existing `/internal/v1` GET routes.
- Importing the user's statements through the **existing** importers
  (`bank_statement_*`, `mybank_*`, `abc_statement`, `boc_statement`), with the
  same preflight → transaction → receipt → idempotent replay discipline.
- Carrying over from `finance-desk` only what is genuinely missing here, as
  separate reviewed changes: the BOC company CSV and PDF container readers, and
  the classification rule set. Both are parser/rule level, not fact level.

## Out of scope

- Any weakening of the production path. Local mode must not make it easier to
  run production without mTLS, the reader role, or the operational gate.
- Automatic posting. Local mode is read-only over facts; classification stays a
  rule proposal, and `Ledger Draft -> Posted Entry` still needs a human.
- Replacing `~/.finance-desk` data by copying its SQLite rows in. Those rows
  were produced by a parser outside this repository's evidence discipline; the
  statements get re-imported from the original files instead.
- OAuth, mail collection, Hermes, OneDrive, any network connector.

## Frozen invariants

- One authoritative fact layer. Local mode reads the same schema through the
  same code; it does not define a second one.
- Money stays signed integer minor units; entries balance per currency in
  PostgreSQL.
- No LLM, rule, or importer writes a posting.
- Local mode refuses to start when `env=production`, when any
  `*_operational_gate` is open, or when `internal_read_transport` is not
  `disabled` — the failure is loud, not a silent downgrade.
- Real statements and artifacts are never committed.

## Acceptance tests

- Starting local mode with a production-shaped environment fails closed, with a
  test that asserts the refusal rather than the success path.
- The local launcher binds loopback only; a test asserts the bound host.
- The same query answered by the deployed read API and by local mode over a
  restored dump returns the same facts.
- Importing one statement twice in local mode produces an exact zero delta.

## Open questions for the user

1. Local PostgreSQL in Docker, or a file-backed local database? Core's
   invariants (balanced entries, `internal_read` definer functions, triggers)
   are enforced *in PostgreSQL*. A file-backed store would mean losing them,
   which would recreate the second-fact-layer problem in a new place. The
   proposal above assumes local PostgreSQL in Docker for that reason.
2. UI shape: reuse `LedgerBridge-Web` pointed at loopback, or the Tkinter
   desktop window that already exists in the old toolbox? The Web path reuses
   real screens; the desktop path is what the user has been opening so far.
3. What happens to `D:\repos\finance-desk` and `~/.finance-desk`: freeze in
   place, or delete once the equivalent data is confirmed present here?

## Implementation evidence

- Not started. This document is the proposal required by `AGENTS.md`
  ("Do not silently alter frozen architecture ... wait for user approval").

## Review findings

-
