# Task: Local single-user mode

- Status: active
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

## Decisions (2026-09-11, user)

1. Local storage is PostgreSQL in Docker on loopback. Core's invariants
   (balanced entries, `internal_read` definer functions, triggers) are enforced
   *in PostgreSQL*; a file-backed store would lose them and recreate the
   second-fact-layer problem somewhere new.
2. The UI is `LedgerBridge-Web` pointed at loopback, not a new desktop window.
   That needs a fourth Web mode beside `synthetic-preview`,
   `authenticated-preview` and `core-backed`: plain HTTP to Core over loopback,
   no client certificates, no Passkey, and a refusal to start on a non-loopback
   bind.
3. `finance-desk` and `~/.finance-desk` are frozen in place, not deleted. They
   stay readable for comparison until local mode answers the same questions;
   nothing new is written there.

## Implementation evidence

- `src/ledgerbridge/local_mode.py` and `tests/test_local_mode.py`: the profile
  and its refusals. 11 tests, with `ruff`, `ruff format` and `mypy` clean.
- Two intended refusals turned out to be enforced by `Settings` itself (a
  production read API without the R1 gate, and real ingest). Those tests assert
  the upstream `ValidationError` and keep local mode's own check as a second
  net, exercised through `model_copy` so that relaxing the upstream validator
  turns this suite red instead of silently emptying the net.
- Still to do: `docker-compose.local.yml`, `scripts/local_mode.py`, the Web
  local mode, importing the statements, and the parser and rule carry-over.

## Baseline correction (2026-09-11)

This task started from `448d700` on `ai/chatgpt/company-mybank-production-import`,
named as the integration branch by `PROJECT_STATUS.md`. That document is dated
2026-09-02 and is stale: `448d700` sits two commits past its merge-base with
`production/core`, which is 121 commits ahead. The branch has been rebased onto
`production/core`; the local database followed from schema `20260902_0034` to
`20260906_0051`, which is also the first real application of that last
migration - it had been verified by source assertion only.

## Later decisions (2026-09-11, user)

4. The review screen keeps its write buttons in local mode. Core's local mode
   serves only the GET read routes, so a confirm or ignore POST fails at Core.
   The UI is not yet honest about that; it was left alone rather than redesigned
   in passing.
5. The BOC company CSV and PDF readers stay on a local branch. They are merged
   into this branch for local use and do not enter the production release line.

## Import chain (2026-09-12)

`docs/operations/LOCAL_IMPORT.md` is the runbook. Summary of what it settles:

- The controlled-review batch path (`controlled_import.py`) runs locally,
  unmodified, and is the shape D-028 describes: bounded preparation,
  transactional write as the owner role, receipt, idempotent replay. It is now
  reachable as `local_mode.py import`, which reads the database URL out of
  `.env.local` so importing never means pasting a password into a shell.
- The registered-account statement cutover path does **not** run here.
  `verify_mybank_cutover_safety_proof` runs before the preflight and the
  execution alike, and demands a v3 encrypted backup plus a passed isolated
  restore rehearsal whose inventory equals the live counts. Those artifacts come
  only from `scripts/backup_restore.py`, which targets Hermes (`/srv/ai-center`,
  `/dev/shm`, `gpg`, GNU `tar`) and does not run on Windows. Hand-writing them
  would be forging a proof, not configuring a tool.
- `run_account_registry_intake.py` refuses unless `LEDGERBRIDGE_ENV=production`
  on both its paths, so a Managed Account cannot be registered locally either.

### Proved against the live local database, 2026-09-12

- Import, then replay: `replayed=false` then `replayed=true`, with an unchanged
  audit horizon and a zero row delta across `entity`, `candidate`,
  `evidence_object` and `audit_event`. This is the task's fourth acceptance
  test, met for the batch path.
- A second batch appeared in `check` without anyone widening a list
  (`entities: 2`, `business_units: 2`).
- `serve` on `127.0.0.1:8661` (confirmed loopback-only by `netstat`) answered
  `/internal/v1/capabilities`, `/internal/v1/candidates` (2 rows, from both
  books) and `/internal/v1/personal-finance-summary`, each `Cache-Control:
  no-store`.
- `/internal/v1/evidence/{ref}/content` returned the decrypted synthetic
  document, and the read appended `internal.read.evidence.content` by
  `workload:local-single-user` to the audit chain.

## Findings the live data forced (2026-09-12)

Three defects that every unit test passed through, all found by importing real
rows rather than by reading the code:

1. **The local grant saw nothing that was assigned.** `local_principal` granted
   the entity with an empty business-unit set. The read design keeps two
   independent keys per unit - the ref the HTTP layer authorizes on and the UUID
   the scoped SQL functions query - and refuses to resolve one into the other,
   so an assigned candidate was invisible while the page rendered cleanly. Fixed
   by carrying `LocalBook(entity_ref, business_units)` through the bootstrap.
2. **The local app mounted one router.** `/capabilities` answered, so the
   assembly test passed; the Web client also asks for statements,
   reconciliations, reports and personal finance, all of which would have 404ed
   and read as missing data. Now the six GET-only routers `main.py` mounts are
   mounted, and `_refuse_non_read_routes` makes "local mode serves reads only" a
   property the process asserts about itself before it binds a socket, rather
   than a list someone has to keep curating.
3. **Evidence downloads failed closed.** They need an evidence key file and a
   persistent audit sink, and the sink writes through the api role - which local
   mode had collapsed into the reader. The profile now carries both URLs and the
   guard checks both hosts.

## Open issues (awaiting user decision)

1. **D-028's repeat-import relaxation is not implemented for statement
   cutovers.** D-028 says that once an import capability is released and
   restore-verified, further files use a bounded preview, transactional write,
   receipt, count check and idempotent replay, and do not repeat the whole
   recovery rehearsal. Every statement import still verifies a full backup and
   restore proof, and `MYBANK_CUTOVER.md` still instructs one fresh
   backup/restore inventory per plan. Closing the gap means changing a gate on
   the path that writes real financial facts; not changed here.
2. **Four review views are mounted but disabled.** `candidate-events`,
   `candidate-classification-groups`, `company-transaction-classifications` and
   its summary are GET routes living inside command routers; Core gates the
   whole module behind `enable_internal_candidate_command_api`, which also
   requires a command assertion key, issuer and audience. Their reads are
   mounted without the commands beside them, so the failure says
   `CANDIDATE_COMMAND_DISABLED` rather than "not found" and enabling the module
   later is a settings change. Whether to enable it locally is a decision: it
   means minting an assertion key on a single-user machine and declaring the
   command API enabled in a profile that serves no command.

## A destructive mistake, recorded

A `local_mode.py verify` subcommand was written that ran the whole suite with
the local role URLs exported, and it was wrong twice over: it emptied
`ledgerbridge` (the schema survived at head; every row did not), and exporting
the `LEDGERBRIDGE_*` variables process-wide failed 117 tests that pass with a
clean environment. It has been removed; the local database was re-imported.

What stands from that exercise is the targeted run: the eight files holding
PostgreSQL integration tests pass in full against the local container - 178
tests, including all five privilege-boundary assertions - which is the evidence
that the local database enforces what Core's does rather than merely holding
the same rows.

## Review findings

-
