"""Finalize one private MYbank cutover plan without disclosing its values."""

from __future__ import annotations

import sys

from ledgerbridge.mybank_cutover_plan_builder import (
    MyBankCutoverPlanBuildError,
    run_mybank_cutover_plan_builder,
)


def main() -> int:
    try:
        return run_mybank_cutover_plan_builder()
    except MyBankCutoverPlanBuildError:
        print("MYBANK_CUTOVER_PLAN_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
