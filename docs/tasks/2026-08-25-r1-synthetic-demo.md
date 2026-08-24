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

## Provenance

- The demo launcher and regression test were developed locally on
  `codex/r1-synthetic-demo`, introduced in `f53a8d0`; the initial task record
  was documented in `551277f`.
- The protected R1/S1 baseline is [PR #19](https://github.com/maiziwheat520-boop/caiwu/pull/19),
  merged as `1714a7866ea3e85789db42c4c5f9929ea7994b07` from feature head
  `4d9ed117d11503a0c29f702ce5de13b504578433`. Its [Hosted run
  `32758962228`](https://github.com/maiziwheat520-boop/caiwu/actions/runs/32758962228)
  finalized as `completed/success`.
- The demo was delivered through [PR #20](https://github.com/maiziwheat520-boop/caiwu/pull/20),
  merged into protected `main` as `3122610236755294eeac505d7e2bee47a4f97a69`
  from head `aed3ce6df5368275494daf7ffabedd38f0d90225`. Its [Hosted run
  `32759774150`](https://github.com/maiziwheat520-boop/caiwu/actions/runs/32759774150)
  is `completed/success`; `secrets`, `quality`, and `compose` all succeeded.
- The local proof above is reproducible evidence for the synthetic-only demo;
  it is separate from, and does not expand, the production deployment gate.
