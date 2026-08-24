from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260824_0011"
down_revision: str | None = "20260824_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_item",
        sa.Column("candidate_key", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "review_item_candidate_key_shape",
        "review_item",
        "candidate_key IS NULL OR candidate_key ~ '^[a-f0-9]{64}$'",
    )
    op.create_index(
        "uq_review_item_candidate_key",
        "review_item",
        ["candidate_key"],
        unique=True,
        postgresql_where=sa.text("candidate_key IS NOT NULL"),
    )
    # Reassert the worker/API read-write boundary after ALTER TABLE.  The
    # migration must remain correct on databases whose role grants drifted.
    op.execute(
        "GRANT USAGE ON SCHEMA public TO ledgerbridge_worker, ledgerbridge_api; "
        "GRANT SELECT, INSERT ON TABLE public.review_item TO ledgerbridge_worker; "
        "GRANT SELECT ON TABLE public.review_item TO ledgerbridge_api;"
    )


def downgrade() -> None:
    op.drop_index("uq_review_item_candidate_key", table_name="review_item")
    op.drop_constraint("review_item_candidate_key_shape", "review_item", type_="check")
    op.drop_column("review_item", "candidate_key")
