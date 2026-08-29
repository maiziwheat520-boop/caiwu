from pathlib import Path


def test_migration_persists_counterparties_and_partial_refund_satisfaction() -> None:
    source = Path("alembic/versions/20260830_0020_counterparty_and_refund_links.py").read_text(
        encoding="utf-8"
    )

    assert "counterparty_identity" in source
    assert "counterparty_classification" in source
    assert "candidate_counterparty" in source
    assert "REVERSAL_MATCH_REQUIRED" in source
    assert "PARTIAL_REFUND" in source
    assert "list_candidate_counterparty_facts" in source
    assert "ORDER BY classification_revision DESC" in source
    assert "^2026-" not in source
