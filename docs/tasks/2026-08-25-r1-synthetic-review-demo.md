# R1 synthetic review workflow demo (2026-08-25)

## Goal

Provide a local, usable review-loop walkthrough after the R1 read demo without
connecting PostgreSQL, Hermes, production credentials, or real financial data.

## Delivered

- `scripts/r1_synthetic_review_demo.py` reuses the production `/v1/reviews`
  handlers and response models with a deterministic in-memory DEDUP fixture.
- `--check` exercises `OPEN -> RESOLVED` and confirms a second decision returns
  the same `409 REVIEW_CONFLICT` boundary as the database service.
- The default invocation listens only on `127.0.0.1:8652`; production settings,
  database grants, real evidence, OAuth, and automatic posting are unchanged.
- `tests/test_r1_synthetic_review_demo.py` keeps the proof record stable.

## Verification

```text
uv run --frozen --extra dev python scripts/r1_synthetic_review_demo.py --check
{"final_status": "RESOLVED", "initial_status": "OPEN", "mode": "synthetic", "review_count": 1, "terminal_conflict_status": 409}
uv run --frozen --extra dev pytest -q tests/test_r1_synthetic_review_demo.py
1 passed
ruff format/check: passed
mypy scripts/r1_synthetic_review_demo.py tests/test_r1_synthetic_review_demo.py: passed
```

This is a local demonstration of the review state machine, not an operator
authentication deployment or permission to enable the real Review API.
