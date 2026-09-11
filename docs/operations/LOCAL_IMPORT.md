# Local import (single-user mode)

What can be imported on one machine, what cannot, and why. Local mode changes
who is admitted and over which socket; it does not change what a financial fact
is, and it does not lower a single gate that guards the deployed database.

Read this before assuming a step is missing: two of the import paths in this
repository deliberately refuse to run outside production, and one of those
refusals is not a configuration flag but a proof about a real backup.

## The two import families

| | Controlled review batch | Registered-account statement cutover |
|---|---|---|
| Code | `src/ledgerbridge/controlled_import.py` | `src/ledgerbridge/mybank_statement_cutover.py` |
| Writes | entity, business unit, categories, encrypted evidence, PENDING candidates | statement, transaction facts, evidence |
| Role | owner | owner |
| Idempotence | receipt in `internal_import.controlled_batch_receipt`, keyed by `batch_ref` | replay comparison inside one transaction |
| Precondition | an operator-written source manifest | an encrypted backup **and** a passed isolated-restore rehearsal |
| Runs locally | yes | no — see [What does not run here](#what-does-not-run-here) |

Both refuse to post anything. Candidates arrive `PENDING`; `Ledger Draft ->
Posted Entry` still needs a person, locally as everywhere else.

## The path that runs locally

```text
python scripts/local_mode.py init
docker compose --env-file .env.local -f docker-compose.local.yml up -d
python scripts/local_mode.py migrate
python scripts/local_mode.py import --source-manifest <absolute path>
python scripts/local_mode.py serve
```

`import` does two things production runs as separate commands
(`prepare_controlled_review_import.py`, then `import_controlled_review_batch.py`),
joined here because locally they are one intention:

1. Encrypt every evidence file named by the manifest into the artifact store
   under `~/.ledgerbridge-local/artifacts`, using the key at
   `~/.ledgerbridge-local/keys/evidence-key.json`. The key is created on first
   use and never rotated; it lives outside the working tree so that nothing
   under the repository holds a key by accident.
2. Write the batch inside one transaction as the owner role, then append its
   receipt.

The database URL is read out of `.env.local`, so importing never involves
pasting a password into a shell. Nothing about the batch is printed except its
counts.

### The source manifest

One JSON file, schema `ledgerbridge.controlled-review-source.v1`, validated
strictly — unknown fields are rejected. The tracked synthetic example is the
fixture in `tests/test_controlled_import.py`; keep real manifests outside Git.
Required: `batch_ref`, `generated_at`, `source_description`, one `entity`, one
`business_unit`, at least one `category`, at least one `evidence` descriptor
(with the plaintext SHA-256 and byte size of the file, which are verified
before encryption), and at least one `candidate`.

Two things bite on the first attempt:

- **`candidate_ref` prefixes must differ across batches.** The candidate's
  `short_id` is derived from the first eight hex characters of its UUID, and it
  is unique per entity. Two fixtures numbered `7000...01` and `7000...02` both
  become `C-70000000` and the second import fails on the unique constraint.
- **A second prepared manifest for the same `batch_ref` conflicts.** Preparing
  again publishes a new encrypted object, so the prepared digest changes, and
  the import refuses with *batch receipt conflicts with prepared manifest*
  rather than writing twice. To re-run a batch, point `--prepared-manifest` at
  the one already prepared.

### Replaying

Running `import` again on the same batch re-reads its own receipt and writes
nothing:

```text
LOCAL_IMPORT_OK replayed=false evidence=1 candidates=1 batch=<ref> horizon=43
LOCAL_IMPORT_OK replayed=true  evidence=1 candidates=1 batch=<ref> horizon=43
```

The unchanged `horizon` is the check worth making: it is the audit-chain
sequence at the time of the receipt, so an unchanged horizon means no event was
appended. This is how to resolve a run that died halfway, rather than by
counting rows.

## Roles, per step

Local mode keeps production's role separation rather than logging in once as
something roomy. The live database has already caught this twice.

| Step | Role | Why not a wider one |
|---|---|---|
| `migrate` | `ledgerbridge_owner` | migrations create objects |
| `import` | `ledgerbridge_owner` | the batch writes dimensions, evidence and candidates |
| listing the books at startup | `ledgerbridge_owner` | the reader cannot select from `entity`; asked once, on a separate connection, before the first request |
| every served request | `ledgerbridge_reader` | it can execute the `internal_read` definer functions and select from no base table at all — the boundary the whole read design rests on |
| appending "this document was opened" | `ledgerbridge_api` | the reader cannot write, and the connection that answers a question should not also write the record of having answered |

An earlier version served requests as `ledgerbridge_api`. Every unit test
passed; the live database refused with *permission denied for table entity*.
That was the design working.

## The local database really is Core's, and how that was checked

The repository carries PostgreSQL integration tests that are skipped unless
given real role URLs. They create their own temporary databases and assert the
privilege boundaries the read design rests on: the runtime login stays
unprivileged, it cannot create temporary tables, the audit function ACL is
append-only, the canonical source registries are read-only to the runtime,
permanent evidence cannot be mutated.

Run against the local container on 2026-09-12, the eight files holding those
tests passed in full — 178 tests, including all five privilege assertions.
That is what distinguishes *a PostgreSQL that holds the rows* from *Core's
database*.

**Do not run the whole suite against the local database.** Two things happen,
both discovered the hard way:

- Something in the wider suite empties `ledgerbridge` when
  `LEDGERBRIDGE_DATABASE_URL` names it. The schema survives at head; every row
  does not. Re-import afterwards.
- Exporting the `LEDGERBRIDGE_*` URLs process-wide also fails 117 tests that
  pass with a clean environment, because they construct `Settings()` expecting
  one.

A `local_mode.py verify` subcommand that did exactly this was written and then
removed for those two reasons. If the check is wanted again, it needs to name
the integration files explicitly and point them at a throwaway database.

Getting the roles right matters here too: handing every variable the owner URL
turns the five privilege assertions into failures that look like database
defects and are actually a mis-wired runner.

## What does not run here

### Registered-account statement cutover

This is the path that imports bank statement transaction facts — the one the
deployed database used for its 11 statements — and it does not run on this
machine.

The blocker is not a flag. `_run_transactional_database_existing_account_import`
calls `verify_mybank_cutover_safety_proof` before it does anything, on both the
preflight and the execute path, and that function requires:

- a backup directory holding `backup.json` (format
  `ledgerbridge-encrypted-backup-v3`, with a GPG fingerprint and the
  ciphertext's SHA-256), `ledgerbridge-backup.tar.gpg`, and a matching
  `SHA256SUMS`;
- a `restore-rehearsal-*.json` beside it, format
  `ledgerbridge-restore-rehearsal-v3`, `status: passed`,
  `production_unchanged: true`, `isolated_resources_removed: true`;
- and that report's `cutover_inventory` to equal the live database's counts
  exactly.

Those artifacts come from `scripts/backup_restore.py`, which is a Hermes
script: it defaults to `/srv/ai-center/...` and `/dev/shm`, and shells out to
`gpg`, `pg_dump`, `docker compose` and GNU `tar`. It does not run on Windows.

There is no correct way to hand-write those files. They are a proof that a
restorable backup exists and was rehearsed; producing them without doing the
work would be forging evidence, not configuring a tool.

### Account registry intake

`scripts/run_account_registry_intake.py` refuses unless
`LEDGERBRIDGE_ENV=production` on **both** of its paths — the rollback preflight
demands `production` plus `database_target=production-rollback-only`, and the
execution path demands `production` plus an explicit execution token. It also
reads a `DEPLOYED_REVISION` file from the working directory and requires it to
match the plan's target revision.

So a Managed Account cannot be registered locally through that command, which
in turn means the statement cutover has nothing to import against even if its
safety proof were satisfiable.

Note the asymmetry, because it is easy to misread: the *statement cutover*
command's preflight is the opposite — it refuses when `LEDGERBRIDGE_ENV` **is**
`production` and requires `database_target=isolated`. That path is meant to run
off-production, but it commits nothing, and it still verifies the safety proof
first.

## Views that stay dark locally

Four GET routes are mounted but answer `404 CANDIDATE_COMMAND_DISABLED`:
`/internal/v1/candidate-events`,
`/internal/v1/candidate-classification-groups`,
`/internal/v1/company-transaction-classifications` and its summary. They live
inside command routers, and Core gates the whole module behind
`enable_internal_candidate_command_api`, which also needs a command assertion
key, issuer and audience.

Their read routes are mounted anyway, without the commands beside them, so the
failure says *disabled* rather than *not found* and turning the module on later
is a settings change rather than a re-assembly. Whether to turn it on is the
user's call: it means minting an assertion key for a local machine, and saying
"the command API is enabled" in a profile that deliberately serves no command.

## Open question for the user

D-028 says that once an import capability has been released and restore-
verified, importing further files uses a bounded preview, a transactional
write, a receipt, a count check and an idempotent replay — and explicitly does
not repeat the whole recovery rehearsal for each file.

The code has not implemented that relaxation for statement cutovers. Every
statement import still verifies a full encrypted-backup-and-restore proof, and
`docs/operations/MYBANK_CUTOVER.md` still instructs one fresh backup/restore
inventory per plan. The controlled-review batch path above is the shape D-028
describes, but it writes candidates rather than statement transaction facts.

Closing that gap would mean changing a gate on the path that writes real
financial facts. That is not a change to make in passing, so it is recorded
here and in the task document, and left for the user to decide.
