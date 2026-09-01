# MYbank whole-statement cutover

The one-shot command reads every private value from one operator-confirmed JSON
plan. The plan, source, key, and generated preflight receipt stay outside Git,
must be regular files, and must be mode `0600`. Start from the synthetic field
example in `docs/templates/mybank-cutover-plan.example.json`; never edit that
tracked example with real values.

The plan binds the reviewed Git revision, exact source digest and size, expected
transaction count, Accounting Owner, business unit, Managed Account, aliases,
registry revision, workload principal, audit context, encrypted backup, passed
isolated-restore report, evidence key, and artifact root. The command rejects
unknown or missing fields and never prints those values.

Do not hand-copy the source digest, size, or transaction count. Start from the
synthetic draft in `docs/templates/mybank-cutover-draft.example.json`, place the
real draft outside Git, and bind it to the verified XLSX with:

```text
LEDGERBRIDGE_MYBANK_PRIVATE_DRAFT=<absolute private draft path>
LEDGERBRIDGE_MYBANK_PRIVATE_PLAN=<new absolute private plan path>
```

Then run `python scripts/build_mybank_cutover_plan.py`. The builder parses the
whole statement, proves the institution and account suffix, derives the exact
digest/size/row count, writes a new mode-`0600` plan, and prints only
`MYBANK_CUTOVER_PLAN_READY`. It never guesses the Accounting Owner or company;
those UUID bindings remain explicit operator inputs in the private draft.

## Existing registered account mode

When the company MYbank account is already present in the Managed Account
registry, start from
`docs/templates/mybank-existing-account-draft.example.json`. This mode accepts
only the exact Entity UUID, business-unit UUID, Managed Account UUID, and
account suffix. It does not accept or rewrite admission evidence, account keys,
aliases, lifecycle rows, or registry revisions.

The builder derives the source digest, byte size, and non-zero transaction
count. A structurally valid export with no transaction rows is explicitly
reported as `MYBANK_CUTOVER_EMPTY_STATEMENT_SKIPPED` and produces no executable
plan. Before importing, Core requires the Managed Account's latest lifecycle to
be `ACTIVE` and requires exactly one business-unit assignment that covers the
whole statement period.

Every existing-account draft must choose `scope.evidence_mode` explicitly.
`CREATE_NEW` requires a new Evidence UUID and creates one encrypted artifact.
`REUSE_EXISTING` creates neither: it requires the supplied Evidence UUID to
match the exact Entity, business unit, media type, plaintext digest, and byte
size, and verifies one consistent active encrypted-object/blob lineage and its
stored plaintext before the statement can be imported. The runner never falls
back from one mode to the other.

Use the same builder, rollback-only preflight, and separately gated production
commands documented below. One plan imports one statement; for multiple files,
generate and execute the plans sequentially so each plan has its own evidence
UUID, fresh backup/restore inventory, digest-bound preflight receipt, and
rollback boundary. The acceptance inventory requires zero changes to Managed
Account registry facts, Candidates,
Journal Entries, and Postings.

## Isolated preflight

Mount the protected source, plan, key, and a disposable restored database and
artifact volume at the same paths that the production command will use. Export
the following values through the protected process environment, not arguments:

```text
LEDGERBRIDGE_ENV=test
LEDGERBRIDGE_MYBANK_DATABASE_TARGET=isolated
LEDGERBRIDGE_MYBANK_DATABASE_URL=<isolated owner URL>
LEDGERBRIDGE_MYBANK_PRIVATE_PLAN=<absolute private plan path>
LEDGERBRIDGE_MYBANK_PREFLIGHT_RECEIPT=<new absolute private receipt path>
LEDGERBRIDGE_DEPLOYED_REVISION=<full reviewed revision>
```

Then run:

```bash
python scripts/run_mybank_statement_cutover.py --preflight-only
```

The command runs the exact source through import, idempotent replay, and the
overlapping-fact conflict probe under one outer transaction. It validates the
full cutover inventory sequence and rolls back both database and newly staged
encrypted evidence before writing a mode-`0600`, plan-bound receipt.

## Production execution

Use the unchanged plan and receipt. Point the protected database environment at
production and require both explicit gates:

```text
LEDGERBRIDGE_ENV=production
LEDGERBRIDGE_MYBANK_DATABASE_TARGET=production
LEDGERBRIDGE_MYBANK_PRODUCTION_EXECUTION=execute-reviewed-cutover-v1
```

Then run:

```bash
python scripts/run_mybank_statement_cutover.py --execute-production
```

Production commit happens only after import, replay, conflict rejection, full
inventory acceptance, and candidate-zero checks all pass. Any earlier failure
rolls back the outer database transaction and removes the unpublished encrypted
blob. Keep the verified encrypted backup as the disaster-recovery anchor.
