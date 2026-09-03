from datetime import UTC, datetime
from uuid import UUID

from scripts import run_historical_classification_backfill as backfill


def test_approved_company_batches_have_fixed_identity() -> None:
    row = {
        "transaction_ref": UUID("70ba3e9f-3205-56fc-84f6-cf42ddc8f73f"),
        "entity_id": UUID("34706039-7677-55c6-be43-f3ab490be8fd"),
        "account_key": "mybank:company:0001",
        "amount_minor": 12_345,
        "occurred_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "counterparty_name": "陈明哲",
        "transaction_name": "往来款",
    }

    assert backfill.company_digest([row]) == (
        "b7f99a92c4e9b031a8d20d518adb35d4b802f2255c9f3cf69ff54e7a4e7fc21a"
    )
    assert backfill.operation_id(backfill.COMPANY_BATCHES[0], row["transaction_ref"]) == UUID(
        "c23fc95a-d9c0-5c1e-9005-591b9bbf02e7"
    )


def test_rule_plans_preserve_approved_totals_and_scopes() -> None:
    assert [(rule.code, rule.expected_count, rule.expected_total) for rule in backfill.RULES] == [
        ("P01", 1_574, 21_362_070),
        ("P02", 49, 247_106_114),
        ("P05", 28, 321_834),
        ("P07", 11, 12_532_545),
    ]
    assert [batch.category for batch in backfill.COMPANY_BATCHES] == [
        "RELATED_PARTY_CURRENT",
        "PAYROLL",
        "FINANCING",
    ]
