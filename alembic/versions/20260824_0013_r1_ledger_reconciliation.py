# ruff: noqa: E501

"""Add immutable ledger attribution and reconciliation snapshot facts.

Migration B is a schema foundation.  It does not add writer commands, reader
views, or runtime grants; existing Phase 5 rows remain backward compatible.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260824_0013"
down_revision: str | None = "20260824_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def _append_only(table: str) -> None:
    op.execute(
        f"""
        CREATE FUNCTION public.r1_{table}_append_only()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION '{table} is append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $function$;
        CREATE TRIGGER r1_{table}_append_only_trigger
        BEFORE UPDATE OR DELETE ON public.{table}
        FOR EACH ROW EXECUTE FUNCTION public.r1_{table}_append_only();
        """
    )


def upgrade() -> None:
    op.create_table(
        "journal_entry_attribution",
        sa.Column("entry_id", UUID, nullable=False),
        sa.Column("entity_id", UUID, nullable=False),
        sa.Column("business_unit_id", UUID, nullable=False),
        sa.Column("accounting_month", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "accounting_month = date_trunc('month', accounting_month)::date",
            name="journal_attribution_month_first_day",
        ),
        sa.ForeignKeyConstraint(["entry_id"], ["journal_entry.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["entity_id"], ["entity.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["entity_id", "business_unit_id"],
            ["business_unit.entity_id", "business_unit.id"],
            ondelete="RESTRICT",
            name="fk_journal_attribution_scope",
        ),
        sa.PrimaryKeyConstraint("entry_id", name="pk_journal_entry_attribution"),
        sa.UniqueConstraint("entry_id", "entity_id", name="uq_journal_attribution_entry_entity"),
    )
    op.create_index(
        "ix_journal_attribution_scope_month",
        "journal_entry_attribution",
        ["entity_id", "business_unit_id", "accounting_month", "entry_id"],
    )
    op.create_table(
        "posting_attribution",
        sa.Column("posting_id", UUID, nullable=False),
        sa.Column("reporting_category_id", UUID, nullable=False),
        sa.Column("category_code_snapshot", sa.String(100), nullable=False),
        sa.Column("category_label_snapshot", sa.String(200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(category_code_snapshot) <> '' AND btrim(category_label_snapshot) <> ''",
            name="posting_attribution_snapshot_not_blank",
        ),
        sa.ForeignKeyConstraint(["posting_id"], ["posting.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["reporting_category_id"], ["reporting_category.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("posting_id", name="pk_posting_attribution"),
    )
    op.add_column("reconciliation_leg", sa.Column("posting_id", UUID, nullable=True))
    op.add_column("reconciliation_leg", sa.Column("is_primary", sa.Boolean(), nullable=True))
    op.add_column("reconciliation_leg", sa.Column("entity_id", UUID, nullable=True))
    op.add_column("reconciliation_leg", sa.Column("business_unit_id", UUID, nullable=True))
    op.add_column("reconciliation_leg", sa.Column("accounting_month", sa.Date(), nullable=True))
    op.create_foreign_key(
        "fk_reconciliation_leg_posting",
        "reconciliation_leg",
        "posting",
        ["posting_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_reconciliation_leg_entity",
        "reconciliation_leg",
        "entity",
        ["entity_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_reconciliation_leg_business_unit",
        "reconciliation_leg",
        "business_unit",
        ["business_unit_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "reconciliation_leg_primary_shape",
        "reconciliation_leg",
        "is_primary IS NULL OR posting_id IS NOT NULL",
    )
    op.create_check_constraint(
        "reconciliation_leg_month_shape",
        "reconciliation_leg",
        "accounting_month IS NULL OR accounting_month = date_trunc('month', accounting_month)::date",
    )
    op.create_index(
        "uq_reconciliation_leg_primary_group",
        "reconciliation_leg",
        ["reconciliation_group_id"],
        unique=True,
        postgresql_where=sa.text("is_primary IS TRUE"),
    )

    op.create_table(
        "reconciliation_snapshot",
        sa.Column(
            "snapshot_ref", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("entity_id", UUID, nullable=False),
        sa.Column("business_unit_id", UUID, nullable=False),
        sa.Column("accounting_month", sa.Date(), nullable=False),
        sa.Column("snapshot_revision", sa.Integer(), nullable=False),
        sa.Column("ledger_audit_sequence", sa.BigInteger(), nullable=False),
        sa.Column("ledger_audit_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("posted_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_event_id", UUID, nullable=False),
        sa.CheckConstraint(
            "accounting_month = date_trunc('month', accounting_month)::date",
            name="snapshot_month_first_day",
        ),
        sa.CheckConstraint("snapshot_revision >= 1", name="snapshot_revision_positive"),
        sa.CheckConstraint("ledger_audit_sequence >= 1", name="snapshot_audit_sequence_positive"),
        sa.CheckConstraint(
            "octet_length(ledger_audit_hash) = 32", name="snapshot_audit_hash_length"
        ),
        sa.CheckConstraint("currency = 'CNY'", name="snapshot_currency_fixed"),
        sa.ForeignKeyConstraint(
            ["entity_id", "business_unit_id"],
            ["business_unit.entity_id", "business_unit.id"],
            ondelete="RESTRICT",
            name="fk_snapshot_scope",
        ),
        sa.ForeignKeyConstraint(["audit_event_id"], ["audit_event.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("snapshot_ref", name="pk_reconciliation_snapshot"),
        sa.UniqueConstraint(
            "entity_id",
            "business_unit_id",
            "accounting_month",
            "snapshot_revision",
            name="uq_snapshot_local_revision",
        ),
        sa.UniqueConstraint("audit_event_id", name="uq_snapshot_audit_event"),
    )
    op.create_table(
        "reconciliation_snapshot_proposal",
        sa.Column("snapshot_ref", UUID, nullable=False),
        sa.Column("proposal_ref", UUID, nullable=False),
        sa.Column("reconciliation_group_id", UUID, nullable=True),
        sa.Column("relation", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_basis", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "relation IN ('1:1','1:N','N:1')", name="snapshot_proposal_relation_allowed"
        ),
        sa.CheckConstraint(
            "status IN ('PROPOSED','CONFIRMED','REJECTED')", name="snapshot_proposal_status_allowed"
        ),
        sa.CheckConstraint(
            "currency = 'CNY' AND amount_basis = 'PRIMARY_LEG'",
            name="snapshot_proposal_amount_basis_fixed",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_ref"], ["reconciliation_snapshot.snapshot_ref"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reconciliation_group_id"], ["reconciliation_group.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("snapshot_ref", "proposal_ref", name="pk_snapshot_proposal"),
    )
    op.create_table(
        "reconciliation_snapshot_suspense",
        sa.Column("snapshot_ref", UUID, nullable=False),
        sa.Column("suspense_ref", UUID, nullable=False),
        sa.Column("suspense_item_id", UUID, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.CheckConstraint(
            "status IN ('OPEN','RESOLVED')", name="snapshot_suspense_status_allowed"
        ),
        sa.CheckConstraint("currency = 'CNY'", name="snapshot_suspense_currency_fixed"),
        sa.ForeignKeyConstraint(
            ["snapshot_ref"], ["reconciliation_snapshot.snapshot_ref"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["suspense_item_id"], ["suspense_item.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("snapshot_ref", "suspense_ref", name="pk_snapshot_suspense"),
    )

    for table in (
        "journal_entry_attribution",
        "posting_attribution",
        "reconciliation_snapshot",
        "reconciliation_snapshot_proposal",
        "reconciliation_snapshot_suspense",
    ):
        _append_only(table)
    op.execute(
        """
        CREATE FUNCTION public.r1_posted_attribution_immutable()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_status public.journal_status;
            v_posting_id uuid;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                v_posting_id := OLD.posting_id;
            ELSE
                v_posting_id := NEW.posting_id;
            END IF;
            SELECT status INTO v_status FROM public.journal_entry AS j
             JOIN public.posting AS p ON p.entry_id = j.id
            WHERE p.id = v_posting_id;
            IF v_status = 'POSTED' THEN
                RAISE EXCEPTION 'posting attribution is immutable after POSTED'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE TRIGGER r1_posting_attribution_posted_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.posting_attribution
        FOR EACH ROW EXECUTE FUNCTION public.r1_posted_attribution_immutable();
        """
    )
    op.execute(
        """
        DO $grant$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY['ledgerbridge_app','ledgerbridge_api','ledgerbridge_worker','ledgerbridge_reader'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format('REVOKE ALL ON TABLE public.journal_entry_attribution, public.posting_attribution, public.reconciliation_snapshot, public.reconciliation_snapshot_proposal, public.reconciliation_snapshot_suspense FROM %I', role_name);
                END IF;
            END LOOP;
        END
        $grant$;
        REVOKE ALL ON TABLE public.journal_entry_attribution, public.posting_attribution, public.reconciliation_snapshot, public.reconciliation_snapshot_proposal, public.reconciliation_snapshot_suspense FROM PUBLIC;
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = (
        "reconciliation_snapshot_suspense",
        "reconciliation_snapshot_proposal",
        "reconciliation_snapshot",
        "posting_attribution",
        "journal_entry_attribution",
    )
    for table in tables:
        if bind.execute(
            sa.text(  # nosec B608 - table is drawn only from the fixed tuple above
                f"SELECT EXISTS (SELECT 1 FROM public.{table})"  # nosec B608 - table is fixed
            )
        ).scalar_one():
            raise RuntimeError("R1 ledger/reconciliation data prevents destructive downgrade")
    op.execute(
        "DROP TRIGGER IF EXISTS r1_posting_attribution_posted_guard ON public.posting_attribution"
    )
    op.execute("DROP FUNCTION IF EXISTS public.r1_posted_attribution_immutable()")
    for table in tables:
        op.execute(f"DROP TRIGGER IF EXISTS r1_{table}_append_only_trigger ON public.{table}")
        op.execute(f"DROP FUNCTION IF EXISTS public.r1_{table}_append_only()")
    for table in tables:
        op.drop_table(table)
    op.drop_index("uq_reconciliation_leg_primary_group", table_name="reconciliation_leg")
    op.drop_constraint("reconciliation_leg_month_shape", "reconciliation_leg", type_="check")
    op.drop_constraint("reconciliation_leg_primary_shape", "reconciliation_leg", type_="check")
    op.drop_constraint(
        "fk_reconciliation_leg_business_unit", "reconciliation_leg", type_="foreignkey"
    )
    op.drop_constraint("fk_reconciliation_leg_entity", "reconciliation_leg", type_="foreignkey")
    op.drop_constraint("fk_reconciliation_leg_posting", "reconciliation_leg", type_="foreignkey")
    for column in ("accounting_month", "business_unit_id", "entity_id", "is_primary", "posting_id"):
        op.drop_column("reconciliation_leg", column)
