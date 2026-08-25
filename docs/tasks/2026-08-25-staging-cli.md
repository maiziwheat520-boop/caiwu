# Isolated staging CLI (2026-08-25)

`scripts/r1_staging_cli.py` wraps the loopback gateway for three operator
actions: `intake-json`, `intake-eml`, `list`, and `command`. It never opens a
database or reads credentials. All requests are restricted to HTTP loopback;
input is capped at 10 MiB and responses at 2 MiB.

The gateway must be running with the optional
`LEDGERBRIDGE_SYNTHETIC_PERSISTENCE_PATH` when using `command`; without that
isolated SQLite store, review commands return 503. `command` forwards the
versioned `CandidateCommand` contract, including expected revision and
operation ID for deterministic idempotency. The `actor_ref` is trusted only
inside this loopback staging process and must not be mistaken for production
authentication.

## Quick flow

```text
uv run --frozen --extra dev python scripts/r1_synthetic_data_gateway.py
uv run --frozen --extra dev python scripts/r1_staging_cli.py intake-eml message.eml --entity-ref <ENTITY_UUID>
uv run --frozen --extra dev python scripts/r1_staging_cli.py list
uv run --frozen --extra dev python scripts/r1_staging_cli.py command <CANDIDATE_UUID> --action IGNORE --expected-revision 1 --reason "not relevant" --actor-ref operator:staging
```

No production mailbox, PostgreSQL writer, mTLS identity, or Posting path is
enabled by this CLI.
