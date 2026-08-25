# Isolated Outlook staging replay (2026-08-25)

## Scope

`scripts/r1_staging_graph_replay.py` is the first usable Graph-to-demo path.
It reads a short-lived access token from process environment, fetches one
bounded inbox page, maps subject/body preview/attachments into the synthetic
gateway contract, and returns candidate projections. It is intentionally
separate from the production provider and persistence paths.

## Explicit enablement

The script exits without network access unless
`LEDGERBRIDGE_STAGING_NETWORK=1` is present. A staging operator must also set
`LEDGERBRIDGE_STAGING_ACCESS_TOKEN`, `LEDGERBRIDGE_STAGING_MAILBOX`, and
`LEDGERBRIDGE_STAGING_ENTITY_REF`. The optional
`LEDGERBRIDGE_STAGING_GATEWAY_URL` defaults to the loopback gateway.

The token is process-only. Do not put it in `.env`, Git, task notes, shell
history, or captured logs. Use a short-lived token and clear the environment
after the replay.

## Bounds and safety

- Graph host is pinned to `graph.microsoft.com`; one page, five messages.
- Each attachment/evidence item is limited to 1 MiB.
- The gateway remains loopback-only, in-memory, and
  `writes_posting=false`.
- The staging payload sets its synthetic admission watermark to the source
  `receivedDateTime`, so historical test mail is not silently dropped by the
  demo activation boundary. This caller-controlled field is only valid for
  this synthetic launcher; production must use a configured deployment
  watermark.
- No refresh tokens, database writes, artifact writes, audit events, or
  production mailbox settings are enabled.

## Verification

```text
uv run --frozen --extra dev python scripts/r1_staging_graph_replay.py --check
uv run ruff format --check src/ledgerbridge/mail_collector.py scripts/r1_staging_graph_replay.py scripts/r1_synthetic_data_gateway.py
uv run ruff check src/ledgerbridge/mail_collector.py scripts/r1_staging_graph_replay.py scripts/r1_synthetic_data_gateway.py
uv run mypy src/ledgerbridge/mail_collector.py scripts/r1_staging_graph_replay.py scripts/r1_synthetic_data_gateway.py
python -m compileall -q src scripts
```

No real token or mailbox was used during repository verification.
