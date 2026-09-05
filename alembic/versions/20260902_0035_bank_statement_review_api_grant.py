"""Allow the API role to execute the authenticated bank review command.

Revision ID: 20260902_0035
Revises: 20260902_0034
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260902_0035"
down_revision: str | None = "20260902_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "internal_command.review_bank_statement(uuid,uuid,uuid,text,text,integer,text,text)"


def upgrade() -> None:
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO ledgerbridge_api")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SIGNATURE} FROM ledgerbridge_api")
