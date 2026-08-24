# Hermes private-message intake boundary (2026-08-25)

## Scope

`ledgerbridge.hermes_message` defines a provider-neutral, bounded envelope and
the D-015 admission policy for the future Hermes adapter. It accepts only the
primary profile's private user messages after activation. Group chats, family
profiles, assistant/tool/system traffic, and empty messages receive a
`DELETE_TOMBSTONE` decision; pre-activation history receives `IGNORE_HISTORY`.
Eligible messages receive `RETAIN_FOR_TRIAGE` and are not automatically judged
financial or non-financial.

The module performs no network I/O, token handling, persistence, HMAC tombstone
write, deletion, model call, or automatic posting. Those remain separate gates.

## Manual replay

```text
uv run --frozen --extra dev python scripts/r1_synthetic_hermes_message_demo.py
{"dispositions": ["RETAIN_FOR_TRIAGE", "DELETE_TOMBSTONE", "IGNORE_HISTORY", "DELETE_TOMBSTONE"], "messages_checked": 4, "mode": "synthetic", "relevant_messages": 1}
```

Ruff, strict mypy, compileall, and diff-check pass for the new boundary. The
fixture is synthetic-only and does not authorize Hermes or real-data enablement.
