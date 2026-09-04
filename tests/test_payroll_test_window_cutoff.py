"""The payroll test window has exactly one cutoff.

Enumerating the months that happened to exist when the window opened made any
later period fail validation outright, instead of routing it to human review.
"""

from __future__ import annotations

import typing

from ledgerbridge.internal_payroll_routes import PayrollTestWorkspaceCreate
from ledgerbridge.payroll_integration import (
    PAYROLL_TEST_CUTOFF_DATE,
    PAYROLL_TEST_CUTOFF_MONTH,
)


def test_wire_cutoff_literal_matches_the_shared_constant() -> None:
    literal = PayrollTestWorkspaceCreate.model_fields["cutoff_date"].annotation
    assert typing.get_args(literal) == (PAYROLL_TEST_CUTOFF_DATE,)


def test_cutoff_date_falls_inside_the_cutoff_month() -> None:
    assert PAYROLL_TEST_CUTOFF_DATE.startswith(f"{PAYROLL_TEST_CUTOFF_MONTH}-")
