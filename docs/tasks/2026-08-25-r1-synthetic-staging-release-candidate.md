# R1 synthetic staging release candidate (2026-08-25)

## Candidate

Branch: `codex/r1-synthetic-demo`
HEAD: `8988bed feat: add staging operator CLI`

This is a usable local/staging candidate, not a production release. It has no
production mailbox, Core writer grants, mTLS workload identity, refresh-token
store, or Posting path.

## One-time setup

From the repository root, choose an existing entity UUID for the demo and keep
the SQLite file under the ignored `var/` directory:

```powershell
$env:LEDGERBRIDGE_SYNTHETIC_PERSISTENCE_PATH = "$(Join-Path (Get-Location) 'var\synthetic-gateway.sqlite3')"
uv run --frozen --extra dev python scripts/r1_synthetic_data_gateway.py
```

The gateway listens only on `127.0.0.1:8653`.

## Manual acceptance flow

In another terminal:

```powershell
# JSON intake from stdin (replace UUIDs with your staging entity/evidence IDs)
'{"message_id":"manual-1","source_event_ref":"40000000-0000-0000-0000-000000000101","entity_ref":"10000000-0000-0000-0000-000000000001","text":"请处理发票","evidence":[{"evidence_ref":"20000000-0000-0000-0000-000000000101","media_type":"text/plain","content_base64":"c3ludGhldGljIGludm9pY2U="}]}' |
  uv run --frozen --extra dev python scripts/r1_staging_cli.py intake-json -

# List candidates and copy the candidate_ref from the JSON output
uv run --frozen --extra dev python scripts/r1_staging_cli.py list

# Apply a review decision; operation_id is generated if omitted
uv run --frozen --extra dev python scripts/r1_staging_cli.py command <CANDIDATE_UUID> `
  --action IGNORE --expected-revision 1 --reason "not relevant" --actor-ref operator:staging
```

Expected results: intake returns HTTP 201 with `triage_action=CANDIDATE` and
`writes_posting=false`; the command returns revision 2 with status `IGNORED`.
Repeating the same command with the same operation ID is idempotent. To test a
completion instead, use `--action COMPLETE_FIELDS --patch-json` with all six
normalized fields (business-unit ref/label, category code/label, amount_minor,
and accounting_month).

## Restart and cleanup checks

Stop and restart the gateway with the same persistence path, then run `list`;
the candidate projection remains available while raw evidence bytes do not.
After review:

```powershell
Remove-Item -LiteralPath .\var\synthetic-gateway.sqlite3 -Force
```

## Automated checks used for this candidate

```text
uv run ruff check src/ledgerbridge/synthetic_persistence.py scripts/r1_synthetic_data_gateway.py scripts/r1_staging_cli.py
uv run mypy src/ledgerbridge/synthetic_persistence.py scripts/r1_synthetic_data_gateway.py scripts/r1_staging_cli.py
python -m compileall -q src scripts
uv run --frozen --extra dev python scripts/r1_synthetic_data_gateway.py --check
```

The full repository test suite and production deployment are intentionally not
part of this quick release-candidate handoff.
