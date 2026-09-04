"""Allow stable overlap when only an empty-counterparty derived ref changed.

Revision ID: 20260904_0045
Revises: 20260904_0044
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260904_0045"
down_revision: str | None = "20260904_0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    definition = connection.execute(
        sa.text(
            "SELECT pg_get_functiondef("
            "'internal_import.import_bank_statement(jsonb)'::regprocedure)"
        )
    ).scalar_one()
    if _ANCHOR not in definition:
        raise RuntimeError("bank statement overlap baseline changed")
    connection.execute(sa.text(definition.replace(_ANCHOR, _REPLACEMENT)))
    op.execute(_ACL_SQL)


_ANCHOR = """            IF v_transaction.fact_sha256 IS DISTINCT FROM v_fact_digest
               OR v_transaction.occurred_at IS DISTINCT FROM v_occurred_at
               OR v_transaction.amount_minor IS DISTINCT FROM v_amount_minor
               OR v_transaction.balance_minor IS DISTINCT FROM v_balance_minor
               OR v_transaction.currency IS DISTINCT FROM 'CNY'
               OR v_transaction.counterparty_ref IS DISTINCT FROM v_item->>'counterparty_ref'"""

_REPLACEMENT = """            IF (
                    v_transaction.fact_sha256 IS DISTINCT FROM v_fact_digest
                    AND NOT (
                        v_transaction.counterparty_name IS NULL
                        AND v_transaction.counterparty_account IS NULL
                        AND v_transaction.counterparty_institution IS NULL
                        AND nullif(v_item->>'counterparty_name','') IS NULL
                        AND nullif(v_item->>'counterparty_account','') IS NULL
                        AND nullif(v_item->>'counterparty_institution','') IS NULL
                    )
               )
               OR v_transaction.occurred_at IS DISTINCT FROM v_occurred_at
               OR v_transaction.amount_minor IS DISTINCT FROM v_amount_minor
               OR v_transaction.balance_minor IS DISTINCT FROM v_balance_minor
               OR v_transaction.currency IS DISTINCT FROM 'CNY'
               OR (
                    v_transaction.counterparty_ref IS DISTINCT FROM v_item->>'counterparty_ref'
                    AND NOT (
                        v_transaction.counterparty_name IS NULL
                        AND v_transaction.counterparty_account IS NULL
                        AND v_transaction.counterparty_institution IS NULL
                        AND nullif(v_item->>'counterparty_name','') IS NULL
                        AND nullif(v_item->>'counterparty_account','') IS NULL
                        AND nullif(v_item->>'counterparty_institution','') IS NULL
                    )
               )"""

_ACL_SQL = """
REVOKE ALL ON FUNCTION internal_import.import_bank_statement(jsonb)
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_app;
GRANT EXECUTE ON FUNCTION internal_import.import_bank_statement(jsonb)
    TO ledgerbridge_worker;
"""


def downgrade() -> None:
    raise RuntimeError("generic bank statement imports are forward-only")
