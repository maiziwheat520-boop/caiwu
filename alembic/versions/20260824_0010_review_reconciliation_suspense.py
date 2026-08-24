"""Persist Phase 5 review, reconciliation, and Suspense boundaries."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260824_0010"
down_revision: str | None = "20260824_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_item",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'OPEN'"), nullable=False),
        sa.Column("source_record_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_actor", sa.String(length=200), nullable=True),
        sa.Column("decision_reason", sa.String(length=1000), nullable=True),
        sa.CheckConstraint(
            "kind IN ('DEDUP', 'RECONCILIATION', 'SUSPENSE')",
            name="review_item_kind_allowed",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'RESOLVED', 'REJECTED')",
            name="review_item_status_allowed",
        ),
        sa.CheckConstraint("btrim(summary) <> ''", name="review_item_summary_not_blank"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="review_item_payload_object"),
        sa.CheckConstraint(
            "(status = 'OPEN' AND decided_at IS NULL AND decision_actor IS NULL "
            "AND decision_reason IS NULL) OR "
            "(status IN ('RESOLVED', 'REJECTED') AND decided_at IS NOT NULL "
            "AND btrim(decision_actor) <> '' AND btrim(decision_reason) <> '')",
            name="review_item_decision_shape",
        ),
        sa.ForeignKeyConstraint(["source_record_id"], ["source_record.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["audit_event_id"], ["audit_event.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("audit_event_id", name="uq_review_item_audit_event"),
    )
    op.create_index("ix_review_item_status_created", "review_item", ["status", "created_at", "id"])

    op.create_table(
        "reconciliation_group",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("review_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relation", sa.String(length=3), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'PROPOSED'"), nullable=False
        ),
        sa.Column("currency", sa.String(length=3), server_default=sa.text("'CNY'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_actor", sa.String(length=200), nullable=True),
        sa.Column("decision_reason", sa.String(length=1000), nullable=True),
        sa.CheckConstraint(
            "relation IN ('1:1', '1:N', 'N:1')",
            name="reconciliation_group_relation_allowed",
        ),
        sa.CheckConstraint(
            "status IN ('PROPOSED', 'CONFIRMED', 'REJECTED')",
            name="reconciliation_group_status_allowed",
        ),
        sa.CheckConstraint("currency = 'CNY'", name="reconciliation_group_currency_cny_v01"),
        sa.CheckConstraint(
            "(status = 'PROPOSED' AND decided_at IS NULL AND decision_actor IS NULL "
            "AND decision_reason IS NULL) OR "
            "(status IN ('CONFIRMED', 'REJECTED') AND decided_at IS NOT NULL "
            "AND btrim(decision_actor) <> '' AND btrim(decision_reason) <> '')",
            name="reconciliation_group_decision_shape",
        ),
        sa.ForeignKeyConstraint(["review_item_id"], ["review_item.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("review_item_id", name="uq_reconciliation_group_review_item"),
    )
    op.create_index(
        "ix_reconciliation_group_status_created",
        "reconciliation_group",
        ["status", "created_at", "id"],
    )

    op.create_table(
        "reconciliation_leg",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("reconciliation_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default=sa.text("'CNY'"), nullable=False),
        sa.CheckConstraint("amount_minor <> 0", name="reconciliation_leg_amount_nonzero"),
        sa.CheckConstraint("currency = 'CNY'", name="reconciliation_leg_currency_cny_v01"),
        sa.ForeignKeyConstraint(
            ["reconciliation_group_id"], ["reconciliation_group.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["source_record_id"], ["source_record.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reconciliation_group_id",
            "source_record_id",
            name="uq_reconciliation_leg_group_source_record",
        ),
    )

    op.create_table(
        "suspense_item",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("review_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_record_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default=sa.text("'CNY'"), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("suspense_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'OPEN'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolution_actor", sa.String(length=200), nullable=True),
        sa.Column("resolution_reason", sa.String(length=1000), nullable=True),
        sa.CheckConstraint(
            "reason IN ('UNKNOWN_COUNTERPARTY', 'UNMATCHED_TRANSFER', 'BALANCE_GAP', "
            "'LOAN_BREAKDOWN')",
            name="suspense_item_reason_allowed",
        ),
        sa.CheckConstraint("amount_minor <> 0", name="suspense_item_amount_nonzero"),
        sa.CheckConstraint("currency = 'CNY'", name="suspense_item_currency_cny_v01"),
        sa.CheckConstraint("status IN ('OPEN', 'RESOLVED')", name="suspense_item_status_allowed"),
        sa.CheckConstraint(
            "(status = 'OPEN' AND resolved_at IS NULL AND resolution_account_id IS NULL "
            "AND resolution_actor IS NULL AND resolution_reason IS NULL) OR "
            "(status = 'RESOLVED' AND resolved_at IS NOT NULL "
            "AND resolution_account_id IS NOT NULL AND btrim(resolution_actor) <> '' "
            "AND btrim(resolution_reason) <> '')",
            name="suspense_item_resolution_shape",
        ),
        sa.ForeignKeyConstraint(["review_item_id"], ["review_item.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_record_id"], ["source_record.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["suspense_account_id"], ["account.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["resolution_account_id"], ["account.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("review_item_id", name="uq_suspense_item_review_item"),
    )
    op.create_index(
        "ix_suspense_item_status_created", "suspense_item", ["status", "created_at", "id"]
    )

    op.execute(
        """
        CREATE FUNCTION public.review_item_enforce_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'OPEN' THEN
                    RAISE EXCEPTION 'review items must start OPEN'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END IF;

            IF NEW.kind IS DISTINCT FROM OLD.kind
               OR NEW.source_record_id IS DISTINCT FROM OLD.source_record_id
               OR NEW.summary IS DISTINCT FROM OLD.summary
               OR NEW.payload IS DISTINCT FROM OLD.payload
               OR NEW.audit_event_id IS DISTINCT FROM OLD.audit_event_id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'review item identity is immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.status <> 'OPEN' THEN
                RAISE EXCEPTION 'terminal review items are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.status NOT IN ('RESOLVED', 'REJECTED') THEN
                RAISE EXCEPTION 'review item may only transition OPEN to a terminal state'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE TRIGGER review_item_state_machine
        BEFORE INSERT OR UPDATE ON public.review_item
        FOR EACH ROW EXECUTE FUNCTION public.review_item_enforce_transition();

        CREATE FUNCTION public.reconciliation_group_enforce_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_kind text;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                SELECT kind INTO v_kind FROM public.review_item WHERE id = NEW.review_item_id;
                IF v_kind IS DISTINCT FROM 'RECONCILIATION' THEN
                    RAISE EXCEPTION 'reconciliation group requires a reconciliation review item'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.status <> 'PROPOSED' THEN
                    RAISE EXCEPTION 'reconciliation groups must start PROPOSED'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.review_item_id IS DISTINCT FROM OLD.review_item_id
               OR NEW.relation IS DISTINCT FROM OLD.relation
               OR NEW.currency IS DISTINCT FROM OLD.currency
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'reconciliation group identity is immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.status <> 'PROPOSED' THEN
                RAISE EXCEPTION 'terminal reconciliation groups are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.status NOT IN ('CONFIRMED', 'REJECTED') THEN
                RAISE EXCEPTION
                    'reconciliation group may only transition PROPOSED to terminal state'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE FUNCTION public.reconciliation_group_validate()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_group_id uuid;
            v_relation text;
            v_count bigint;
            v_positive bigint;
            v_negative bigint;
            v_total bigint;
        BEGIN
            IF TG_TABLE_NAME = 'reconciliation_leg' THEN
                v_group_id := COALESCE(
                    (to_jsonb(NEW) ->> 'reconciliation_group_id')::uuid,
                    (to_jsonb(OLD) ->> 'reconciliation_group_id')::uuid
                );
            ELSE
                v_group_id := COALESCE(
                    (to_jsonb(NEW) ->> 'id')::uuid,
                    (to_jsonb(OLD) ->> 'id')::uuid
                );
            END IF;
            SELECT relation, COUNT(l.id), COUNT(*) FILTER (WHERE amount_minor > 0),
                   COUNT(*) FILTER (WHERE amount_minor < 0), COALESCE(SUM(amount_minor), 0)
              INTO v_relation, v_count, v_positive, v_negative, v_total
              FROM public.reconciliation_group AS g
              LEFT JOIN public.reconciliation_leg AS l
                ON l.reconciliation_group_id = g.id
             WHERE g.id = v_group_id
             GROUP BY g.relation;
            IF NOT FOUND OR v_count = 0 OR v_total <> 0 THEN
                RAISE EXCEPTION 'reconciliation group must contain non-empty zero-sum legs'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF (v_relation = '1:1' AND NOT (v_positive = 1 AND v_negative = 1))
               OR (v_relation = '1:N' AND NOT (v_negative = 1 AND v_positive > 1))
               OR (v_relation = 'N:1' AND NOT (v_negative > 1 AND v_positive = 1)) THEN
                RAISE EXCEPTION 'reconciliation group cardinality does not match relation'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE CONSTRAINT TRIGGER reconciliation_group_validate_on_group
        AFTER INSERT OR UPDATE ON public.reconciliation_group
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.reconciliation_group_validate();

        CREATE CONSTRAINT TRIGGER reconciliation_group_validate_on_leg
        AFTER INSERT OR UPDATE OR DELETE ON public.reconciliation_leg
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.reconciliation_group_validate();

        CREATE TRIGGER reconciliation_group_state_machine
        BEFORE INSERT OR UPDATE ON public.reconciliation_group
        FOR EACH ROW EXECUTE FUNCTION public.reconciliation_group_enforce_transition();

        CREATE FUNCTION public.suspense_item_enforce_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_kind text;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                SELECT kind INTO v_kind FROM public.review_item WHERE id = NEW.review_item_id;
                IF v_kind IS DISTINCT FROM 'SUSPENSE' THEN
                    RAISE EXCEPTION 'Suspense item requires a Suspense review item'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.status <> 'OPEN' THEN
                    RAISE EXCEPTION 'Suspense items must start OPEN'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.review_item_id IS DISTINCT FROM OLD.review_item_id
               OR NEW.source_record_id IS DISTINCT FROM OLD.source_record_id
               OR NEW.amount_minor IS DISTINCT FROM OLD.amount_minor
               OR NEW.currency IS DISTINCT FROM OLD.currency
               OR NEW.reason IS DISTINCT FROM OLD.reason
               OR NEW.suspense_account_id IS DISTINCT FROM OLD.suspense_account_id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'Suspense identity is immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.status <> 'OPEN' THEN
                RAISE EXCEPTION 'resolved Suspense items are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.status <> 'RESOLVED'
               OR NEW.resolution_account_id IS NULL
               OR NEW.resolution_account_id = NEW.suspense_account_id THEN
                RAISE EXCEPTION 'Suspense may only transition OPEN to a different account'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE TRIGGER suspense_item_state_machine
        BEFORE INSERT OR UPDATE ON public.suspense_item
        FOR EACH ROW EXECUTE FUNCTION public.suspense_item_enforce_transition();
        """
    )

    op.execute(
        """
        REVOKE ALL ON TABLE public.review_item, public.reconciliation_group,
            public.reconciliation_leg, public.suspense_item FROM PUBLIC;

        GRANT SELECT, INSERT, UPDATE ON TABLE public.review_item,
            public.reconciliation_group, public.reconciliation_leg, public.suspense_item
            TO ledgerbridge_app;

        GRANT SELECT ON TABLE public.review_item, public.reconciliation_group,
            public.reconciliation_leg, public.suspense_item TO ledgerbridge_api;
        GRANT UPDATE (
            status, decided_at, decision_actor, decision_reason
        ) ON TABLE public.review_item TO ledgerbridge_api;
        GRANT UPDATE (
            status, decided_at, decision_actor, decision_reason
        ) ON TABLE public.reconciliation_group TO ledgerbridge_api;
        GRANT UPDATE (
            status, resolved_at, resolution_account_id, resolution_actor, resolution_reason
        ) ON TABLE public.suspense_item TO ledgerbridge_api;

        GRANT SELECT, INSERT ON TABLE public.review_item, public.reconciliation_group,
            public.reconciliation_leg, public.suspense_item TO ledgerbridge_worker;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $ledgerbridge$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.review_item)
               OR EXISTS (SELECT 1 FROM public.reconciliation_group)
               OR EXISTS (SELECT 1 FROM public.reconciliation_leg)
               OR EXISTS (SELECT 1 FROM public.suspense_item) THEN
                RAISE EXCEPTION 'Phase 5 review data prevents destructive downgrade';
            END IF;
        END
        $ledgerbridge$;
        """
    )
    op.execute(
        """
        REVOKE ALL ON TABLE public.review_item, public.reconciliation_group,
            public.reconciliation_leg, public.suspense_item
            FROM ledgerbridge_app, ledgerbridge_api, ledgerbridge_worker;
        DROP TRIGGER suspense_item_state_machine ON public.suspense_item;
        DROP TRIGGER reconciliation_group_validate_on_leg ON public.reconciliation_leg;
        DROP TRIGGER reconciliation_group_validate_on_group ON public.reconciliation_group;
        DROP TRIGGER reconciliation_group_state_machine ON public.reconciliation_group;
        DROP TRIGGER review_item_state_machine ON public.review_item;
        DROP FUNCTION public.suspense_item_enforce_transition();
        DROP FUNCTION public.reconciliation_group_validate();
        DROP FUNCTION public.reconciliation_group_enforce_transition();
        DROP FUNCTION public.review_item_enforce_transition();
        """
    )
    op.drop_index("ix_suspense_item_status_created", table_name="suspense_item")
    op.drop_table("suspense_item")
    op.drop_table("reconciliation_leg")
    op.drop_index("ix_reconciliation_group_status_created", table_name="reconciliation_group")
    op.drop_table("reconciliation_group")
    op.drop_index("ix_review_item_status_created", table_name="review_item")
    op.drop_table("review_item")
