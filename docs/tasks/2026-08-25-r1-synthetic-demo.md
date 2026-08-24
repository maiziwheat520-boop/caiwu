# R1 synthetic demo (2026-08-25)

## Goal

Provide a usable local walkthrough immediately after the protected R1/S1 merge,
without connecting to PostgreSQL, Hermes, production credentials, or real
financial evidence.

## Delivered

- `scripts/r1_synthetic_demo.py` builds the same R1 FastAPI route composition
  with the synthetic backend, a fixed loopback-only principal, and a process
  local audit sink.
- `--check` exercises capabilities, candidate collection/detail, evidence
  download, reconciliation, and ledger summary in one command and exits with a
  JSON proof record.
- The default invocation starts a loopback listener for manual `curl` checks.
- `tests/test_r1_synthetic_demo.py` verifies all six routes, scope filtering,
  evidence no-store/security headers, ledger totals, and the audit event.

## Verification

```text
uv run --frozen --extra dev python scripts/r1_synthetic_demo.py --check
{"candidate_count": 3, "evidence_audit_events": 1, "ledger_totals_minor": {"SUPPLIES": -12345}, "mode": "synthetic", "routes_checked": 6}
uv run --frozen --extra dev pytest -q tests/test_r1_synthetic_demo.py
1 passed
```

This is a demo/test boundary only. It is not an authentication deployment,
does not provide production audit durability, and must not be pointed at real
data.
