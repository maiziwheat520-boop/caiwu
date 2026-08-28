"""Close production review receipt and retired-role deployment gaps."""

import os
from collections.abc import Sequence

from alembic import op

revision: str = "20260829_0018"
down_revision: str | None = "20260828_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        r"""
        ALTER TABLE internal_read.evidence_read_receipt
            DROP CONSTRAINT ck_evidence_read_receipt_san;
        ALTER TABLE internal_read.evidence_read_receipt
            ADD CONSTRAINT ck_evidence_read_receipt_san
            CHECK (
                verified_san ~ '^spiffe://ledgerbridge(\.test|\.local)?/[a-z0-9/_-]+$'
            ) NOT VALID;
        ALTER TABLE internal_read.evidence_read_receipt
            VALIDATE CONSTRAINT ck_evidence_read_receipt_san;
        ALTER FUNCTION public.r1_validate_evidence_read_receipt() SECURITY DEFINER;

        DO $revoke$
        DECLARE
            target regprocedure;
        BEGIN
            FOR target IN
                SELECT p.oid::regprocedure
                  FROM pg_proc AS p
                  JOIN pg_namespace AS n ON n.oid = p.pronamespace
                 WHERE n.nspname = 'public'
                   AND p.proname LIKE 'r1\_%' ESCAPE '\'
            LOOP
                EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', target);
            END LOOP;
        END
        $revoke$;
        """
    )
    if os.getenv("LEDGERBRIDGE_ENV", "development").lower() == "production":
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
        raise RuntimeError("production review hardening is irreversible")
    op.execute(
        r"""
        ALTER TABLE internal_read.evidence_read_receipt
            DROP CONSTRAINT ck_evidence_read_receipt_san;
        ALTER TABLE internal_read.evidence_read_receipt
            ADD CONSTRAINT ck_evidence_read_receipt_san
            CHECK (
                verified_san ~ '^spiffe://ledgerbridge(\.test)?/[a-z0-9/_-]+$'
            );
        ALTER FUNCTION public.r1_validate_evidence_read_receipt() SECURITY INVOKER;
        """
    )
