from typing import Any

import pytest
from pydantic import ValidationError

from ledgerbridge.cash_reconciliation import CashReconciliationProjection


def projection_payload() -> dict[str, Any]:
    return {
        "contract_version": "ledgerbridge.cash-reconciliation.v2",
        "accounting_month": "2026-08",
        "rules": [
            {
                "rule_key": "income.synthetic",
                "source_kind": "BANK_TRANSACTION",
                "source_ref": "bank.synthetic",
                "flow_kind": "INCOME",
                "business_unit_label": "Unit A",
                "item_label": "Room receipt",
                "match_pattern": "synthetic",
                "amount_direction": "CREDIT",
                "effective_from": "2026-01-01",
                "effective_to": None,
            }
        ],
        "rows": [
            {
                "rule_key": "income.synthetic",
                "flow_kind": "INCOME",
                "business_unit_label": "Unit A",
                "item_label": "Room receipt",
                "source_kind": "BANK_TRANSACTION",
                "source_ref": "bank.synthetic",
                "transaction_count": 1,
                "amount_minor": 1000,
                "facts": [
                    {
                        "fact_ref": "fact-1",
                        "occurred_on": "2026-08-01",
                        "amount_minor": 1000,
                    }
                ],
            }
        ],
        "issues": [
            {
                "issue_kind": "UNMATCHED",
                "source_kind": "BANK_TRANSACTION",
                "fact_ref": "BANK_TRANSACTION:fact-2",
                "occurred_on": "2026-08-02",
                "amount_minor": -200,
                "matched_rule_keys": [],
            },
            {
                "issue_kind": "MULTIPLE_RULES",
                "source_kind": "BANK_TRANSACTION",
                "fact_ref": "BANK_TRANSACTION:fact-3",
                "occurred_on": "2026-08-03",
                "amount_minor": 300,
                "matched_rule_keys": ["income.synthetic", "income.synthetic.duplicate"],
            },
        ],
        "eligible_fact_count": 3,
        "matched_fact_count": 1,
        "unmatched_fact_count": 1,
        "conflicted_fact_count": 1,
        "issue_count": 2,
        "issues_truncated": False,
        "totals": {"income_minor": 1000, "expense_minor": 0, "current_minor": 0},
    }


def test_projection_accepts_visible_unmatched_and_conflicted_facts() -> None:
    projection = CashReconciliationProjection.model_validate(projection_payload())

    assert projection.matched_fact_count == 1
    assert projection.conflicted_fact_count == 1
    assert projection.totals.income_minor == 1000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("eligible_fact_count", 4),
        ("matched_fact_count", 2),
        ("issue_count", 1),
        ("issues_truncated", True),
    ],
)
def test_projection_rejects_inconsistent_coverage(field: str, value: object) -> None:
    payload = projection_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        CashReconciliationProjection.model_validate(payload)


def test_projection_rejects_conflict_with_only_one_rule() -> None:
    payload = projection_payload()
    issues = list(payload["issues"])
    issues[1] = {**issues[1], "matched_rule_keys": ["income.synthetic"]}
    payload["issues"] = issues

    with pytest.raises(ValidationError):
        CashReconciliationProjection.model_validate(payload)
