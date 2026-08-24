"""Retire the legacy superset runtime role in production deployments."""

import os
from collections.abc import Sequence

from alembic import op

revision: str = "20260823_0007"
down_revision: str | None = "20260823_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The compatibility role remains available for the test harness and for
    # non-production local upgrades. A production migration is the explicit
    # point at which the old broad role is made unusable.
    if os.getenv("LEDGERBRIDGE_ENV", "development").lower() != "production":
        return
    op.execute(
        """
        ALTER ROLE ledgerbridge_app NOLOGIN;
        REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM ledgerbridge_app;
        REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM ledgerbridge_app;
        REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM ledgerbridge_app;
        REVOKE ALL PRIVILEGES ON TYPE public.entity_type, public.account_class,
            public.journal_status, public.import_job_status, public.dispatch_state
            FROM ledgerbridge_app;
        REVOKE USAGE ON SCHEMA public FROM ledgerbridge_app;
        """
    )


def downgrade() -> None:
    if os.getenv("LEDGERBRIDGE_ENV", "development").lower() == "production":
        raise RuntimeError("production retirement of ledgerbridge_app is irreversible")
