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
