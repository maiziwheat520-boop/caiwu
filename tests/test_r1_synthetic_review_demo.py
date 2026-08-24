from __future__ import annotations

from scripts.r1_synthetic_review_demo import run_self_check


def test_r1_synthetic_review_demo_runs_open_decide_and_terminal_conflict() -> None:
    result = run_self_check()

    assert result == {
        "mode": "synthetic",
        "review_count": 1,
        "initial_status": "OPEN",
        "final_status": "RESOLVED",
        "terminal_conflict_status": 409,
    }
