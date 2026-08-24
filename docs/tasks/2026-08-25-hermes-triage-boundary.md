# Hermes financial-triage boundary (2026-08-25)

`ledgerbridge.hermes_triage` consumes only messages admitted by the private-chat
policy. It exposes a classifier protocol and four safe actions:

- `CANDIDATE` for a reviewed financial label;
- `DELETE_TOMBSTONE` for an explicit non-financial label;
- `AMBIGUOUS_RETAIN` when the classifier is missing, fails, or is uncertain;
- `SKIP` for messages rejected by the upstream admission policy.

The default `UnavailableHermesTriageClassifier` never guesses. The synthetic
keyword classifier exists only for local replay and is not wired into
production. No model call, deletion, database write, or automatic posting is
performed by this boundary.

## Manual replay

```text
uv run --frozen --extra dev python scripts/r1_synthetic_hermes_triage_demo.py
{"fixture_action": "CANDIDATE", "fixture_label": "FINANCIAL", "mode": "synthetic", "unavailable_action": "AMBIGUOUS_RETAIN", "unavailable_label": "AMBIGUOUS"}
```

Ruff, strict mypy, compileall, and diff-check pass for the new code.
