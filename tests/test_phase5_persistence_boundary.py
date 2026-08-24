from pathlib import Path

from ledgerbridge.db import Base
from ledgerbridge.models import (
    ReconciliationGroup,
    ReconciliationLeg,
    ReviewItem,
    SuspenseItem,
)

MIGRATION = Path("alembic/versions/20260824_0010_review_reconciliation_suspense.py")


def test_phase5_models_are_registered_with_expected_tables() -> None:
    assert {
        ReviewItem.__tablename__,
        ReconciliationGroup.__tablename__,
        ReconciliationLeg.__tablename__,
        SuspenseItem.__tablename__,
    } <= set(Base.metadata.tables)
    review_constraints = {
        str(constraint.name)
        for constraint in ReviewItem.__table__.constraints  # type: ignore[attr-defined]
    }
    reconciliation_constraints = {
        str(constraint.name)
        for constraint in ReconciliationGroup.__table__.constraints  # type: ignore[attr-defined]
    }
    suspense_constraints = {
        str(constraint.name)
        for constraint in SuspenseItem.__table__.constraints  # type: ignore[attr-defined]
    }
    assert any(name.endswith("review_item_status_allowed") for name in review_constraints)
    assert any(
        name.endswith("reconciliation_group_relation_allowed")
        for name in reconciliation_constraints
    )
    assert any(name.endswith("suspense_item_resolution_shape") for name in suspense_constraints)


def test_phase5_migration_is_fail_closed_and_fixed_search_path() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "20260824_0010"' in source
    assert 'down_revision: str | None = "20260824_0009"' in source
    assert source.count("SET search_path = pg_catalog") >= 4
    assert "CREATE CONSTRAINT TRIGGER reconciliation_group_validate_on_group" in source
    assert "CREATE CONSTRAINT TRIGGER reconciliation_group_validate_on_leg" in source
    assert "CREATE TRIGGER reconciliation_group_state_machine" in source
    assert "GRANT UPDATE (" in source
    assert "GRANT DELETE" not in source
    assert "DROP TABLE ... CASCADE" not in source
    assert "Phase 5 review data prevents destructive downgrade" in source
