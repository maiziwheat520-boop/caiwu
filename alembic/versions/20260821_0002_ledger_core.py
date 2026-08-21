"""Create the Phase 1 double-entry Ledger Core.

Revision ID: 20260821_0002
Revises: 20260821_0001
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260821_0002"
down_revision: str | None = "20260821_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENTITY_TYPE = postgresql.ENUM("PERSON", "COMPANY", name="entity_type", create_type=False)
ACCOUNT_CLASS = postgresql.ENUM(
    "ASSET",
    "LIABILITY",
    "INCOME",
    "EXPENSE",
    "EQUITY",
    "SUSPENSE",
    name="account_class",
    create_type=False,
)
JOURNAL_STATUS = postgresql.ENUM(
    "DRAFT", "POSTED", "REVERSED", name="journal_status", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    ENTITY_TYPE.create(bind, checkfirst=True)
    ACCOUNT_CLASS.create(bind, checkfirst=True)
    JOURNAL_STATUS.create(bind, checkfirst=True)

    op.execute(
        """
        DO $ledgerbridge$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_app') THEN
                CREATE ROLE ledgerbridge_app NOLOGIN;
            END IF;
            EXECUTE format('GRANT ledgerbridge_app TO %I', current_user);
        END
        $ledgerbridge$;
        """
    )

    op.create_table(
        "entity",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("entity_type", ENTITY_TYPE, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_entity_entity_name_not_blank"),
        sa.PrimaryKeyConstraint("id", name="pk_entity"),
    )

    op.create_table(
        "account",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("identifier", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("account_class", ACCOUNT_CLASS, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(identifier) <> ''", name="ck_account_account_identifier_not_blank"
        ),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_account_account_name_not_blank"),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["entity.id"], name="fk_account_entity_id_entity", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_account"),
        sa.UniqueConstraint("entity_id", "identifier", name="uq_account_entity_identifier"),
        sa.UniqueConstraint("id", "entity_id", name="uq_account_id_entity"),
    )

    op.create_table(
        "audit_event",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("sequence", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("action", sa.String(length=200), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.String(length=100), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prev_hash", sa.LargeBinary(), nullable=True),
        sa.Column("hash", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_event"),
        sa.UniqueConstraint("sequence", name="uq_audit_event_sequence"),
    )

    op.create_table(
        "journal_entry",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("origin", sa.String(length=100), nullable=False),
        sa.Column("status", JOURNAL_STATUS, nullable=False),
        sa.Column("source_record_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("adjusts_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reverses_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("primary_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "btrim(origin) <> ''", name="ck_journal_entry_journal_entry_origin_not_blank"
        ),
        sa.CheckConstraint(
            "adjusts_entry_id IS NULL OR adjusts_entry_id <> id",
            name="ck_journal_entry_journal_entry_adjusts_not_self",
        ),
        sa.CheckConstraint(
            "reverses_entry_id IS NULL OR reverses_entry_id <> id",
            name="ck_journal_entry_journal_entry_reverses_not_self",
        ),
        sa.CheckConstraint(
            "(CASE WHEN adjusts_entry_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN reverses_entry_id IS NULL THEN 0 ELSE 1 END) <= 1",
            name="ck_journal_entry_journal_entry_one_correction_relation",
        ),
        sa.ForeignKeyConstraint(
            ["adjusts_entry_id"],
            ["journal_entry.id"],
            name="fk_journal_entry_adjusts_entry_id_journal_entry",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["audit_event_id"],
            ["audit_event.id"],
            name="fk_journal_entry_audit_event_id_audit_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entity.id"],
            name="fk_journal_entry_entity_id_entity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["primary_account_id", "entity_id"],
            ["account.id", "account.entity_id"],
            name="fk_journal_entry_primary_account_entity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reverses_entry_id"],
            ["journal_entry.id"],
            name="fk_journal_entry_reverses_entry_id_journal_entry",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_journal_entry"),
        sa.UniqueConstraint("audit_event_id", name="uq_journal_entry_audit_event_id"),
    )

    op.create_table(
        "posting",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default=sa.text("'CNY'"),
            nullable=False,
        ),
        sa.CheckConstraint("currency = 'CNY'", name="ck_posting_posting_currency_cny_v01"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.id"],
            name="fk_posting_account_id_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["entry_id"],
            ["journal_entry.id"],
            name="fk_posting_entry_id_journal_entry",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_posting"),
    )
    op.create_index("ix_posting_account_id", "posting", ["account_id"], unique=False)
    op.create_index("ix_posting_entry_id", "posting", ["entry_id"], unique=False)

    op.execute(
        """
        CREATE FUNCTION audit_event_block_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            RAISE EXCEPTION 'audit_event is append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $function$;

        CREATE TRIGGER audit_event_no_update_delete
        BEFORE UPDATE OR DELETE ON audit_event
        FOR EACH ROW EXECUTE FUNCTION audit_event_block_mutation();
        """
    )

    op.execute(
        """
        CREATE FUNCTION append_audit_event(
            p_actor text,
            p_action text,
            p_reason text,
            p_rule_version text DEFAULT NULL,
            p_payload jsonb DEFAULT '{}'::jsonb
        )
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
        DECLARE
            v_id uuid := gen_random_uuid();
            v_sequence bigint;
            v_occurred_at timestamptz := clock_timestamp();
            v_prev_hash bytea;
            v_hash bytea;
            v_serialized jsonb;
        BEGIN
            IF btrim(p_actor) = '' OR btrim(p_action) = '' OR btrim(p_reason) = '' THEN
                RAISE EXCEPTION 'actor, action, and reason must not be blank'
                    USING ERRCODE = 'check_violation';
            END IF;

            PERFORM pg_advisory_xact_lock(hashtext('ledgerbridge.audit_event'));
            SELECT hash INTO v_prev_hash
            FROM public.audit_event
            ORDER BY sequence DESC
            LIMIT 1;

            v_sequence := nextval(pg_get_serial_sequence('public.audit_event', 'sequence'));
            v_serialized := jsonb_build_object(
                'id', v_id,
                'sequence', v_sequence,
                'occurred_at', to_char(
                    v_occurred_at AT TIME ZONE 'UTC',
                    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                ),
                'actor', p_actor,
                'action', p_action,
                'reason', p_reason,
                'rule_version', p_rule_version,
                'payload', COALESCE(p_payload, '{}'::jsonb),
                'prev_hash', CASE
                    WHEN v_prev_hash IS NULL THEN NULL
                    ELSE encode(v_prev_hash, 'hex')
                END
            );
            v_hash := public.digest(convert_to(v_serialized::text, 'UTF8'), 'sha256');

            INSERT INTO public.audit_event (
                id,
                sequence,
                occurred_at,
                actor,
                action,
                reason,
                rule_version,
                payload,
                prev_hash,
                hash
            )
            VALUES (
                v_id,
                v_sequence,
                v_occurred_at,
                p_actor,
                p_action,
                p_reason,
                p_rule_version,
                COALESCE(p_payload, '{}'::jsonb),
                v_prev_hash,
                v_hash
            );
            RETURN v_id;
        END
        $function$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION journal_entry_validate_relationships()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            v_target_entity uuid;
            v_target_status journal_status;
            v_target_id uuid;
        BEGIN
            IF NEW.adjusts_entry_id IS NOT NULL THEN
                v_target_id := NEW.adjusts_entry_id;
            ELSE
                v_target_id := NEW.reverses_entry_id;
            END IF;

            IF v_target_id IS NOT NULL THEN
                SELECT entity_id, status
                INTO v_target_entity, v_target_status
                FROM journal_entry
                WHERE id = v_target_id;

                IF NOT FOUND THEN
                    RAISE EXCEPTION 'correction target % does not exist', v_target_id
                        USING ERRCODE = 'foreign_key_violation';
                END IF;
                IF v_target_entity <> NEW.entity_id THEN
                    RAISE EXCEPTION 'correction target must belong to the same entity'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF v_target_status <> 'POSTED' THEN
                    RAISE EXCEPTION 'correction target must be POSTED'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE TRIGGER journal_entry_validate_correction
        BEFORE INSERT OR UPDATE ON journal_entry
        FOR EACH ROW EXECUTE FUNCTION journal_entry_validate_relationships();
        """
    )

    op.execute(
        """
        CREATE FUNCTION journal_entry_block_posted_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            IF OLD.status = 'POSTED' THEN
                RAISE EXCEPTION 'POSTED journal entries are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE TRIGGER journal_entry_posted_immutable
        BEFORE UPDATE OR DELETE ON journal_entry
        FOR EACH ROW EXECUTE FUNCTION journal_entry_block_posted_mutation();
        """
    )

    op.execute(
        """
        CREATE FUNCTION posting_enforce_entity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            v_entry_entity uuid;
            v_account_entity uuid;
        BEGIN
            SELECT entity_id INTO v_entry_entity FROM journal_entry WHERE id = NEW.entry_id;
            SELECT entity_id INTO v_account_entity FROM account WHERE id = NEW.account_id;
            IF v_entry_entity IS NULL OR v_account_entity IS NULL THEN
                RAISE EXCEPTION 'posting references a missing entry or account'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            IF v_entry_entity <> v_account_entity THEN
                RAISE EXCEPTION 'posting entry and account must belong to the same entity'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE TRIGGER posting_entity_boundary
        BEFORE INSERT OR UPDATE ON posting
        FOR EACH ROW EXECUTE FUNCTION posting_enforce_entity();
        """
    )

    op.execute(
        """
        CREATE FUNCTION posting_block_posted_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            v_entry_id uuid;
            v_status journal_status;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                v_entry_id := OLD.entry_id;
                SELECT status INTO v_status FROM journal_entry WHERE id = v_entry_id;
                IF v_status = 'POSTED' THEN
                    RAISE EXCEPTION 'postings on POSTED entries are immutable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;

            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                v_entry_id := NEW.entry_id;
                SELECT status INTO v_status FROM journal_entry WHERE id = v_entry_id;
                IF v_status = 'POSTED' THEN
                    RAISE EXCEPTION 'postings on POSTED entries are immutable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END IF;
            RETURN OLD;
        END
        $function$;

        CREATE TRIGGER posting_posted_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON posting
        FOR EACH ROW EXECUTE FUNCTION posting_block_posted_mutation();
        """
    )

    op.execute(
        """
        CREATE FUNCTION posting_assert_balanced()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            v_entry_ids uuid[] := ARRAY[]::uuid[];
            v_entry_id uuid;
            v_currency text;
            v_total bigint;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                v_entry_ids := array_append(v_entry_ids, OLD.entry_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                v_entry_ids := array_append(v_entry_ids, NEW.entry_id);
            END IF;

            FOREACH v_entry_id IN ARRAY v_entry_ids LOOP
                SELECT p.currency, SUM(p.amount_minor)
                INTO v_currency, v_total
                FROM posting AS p
                WHERE p.entry_id = v_entry_id
                GROUP BY p.currency
                HAVING SUM(p.amount_minor) <> 0
                LIMIT 1;

                IF FOUND THEN
                    RAISE EXCEPTION
                        'journal entry % is unbalanced for currency %: % minor units',
                        v_entry_id,
                        v_currency,
                        v_total
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END LOOP;

            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE CONSTRAINT TRIGGER posting_balanced_per_currency
        AFTER INSERT OR UPDATE OR DELETE ON posting
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION posting_assert_balanced();
        """
    )

    op.execute(
        """
        CREATE FUNCTION journal_entry_assert_posted_complete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            v_posting_count bigint;
        BEGIN
            IF NEW.status = 'POSTED' THEN
                SELECT COUNT(*) INTO v_posting_count
                FROM posting
                WHERE entry_id = NEW.id;
                IF v_posting_count < 2 THEN
                    RAISE EXCEPTION 'POSTED journal entries require at least two postings'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE CONSTRAINT TRIGGER journal_entry_posted_complete
        AFTER INSERT OR UPDATE OF status ON journal_entry
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION journal_entry_assert_posted_complete();
        """
    )

    op.execute(
        """
        REVOKE ALL ON TABLE audit_event FROM PUBLIC;
        REVOKE ALL ON FUNCTION append_audit_event(text, text, text, text, jsonb) FROM PUBLIC;

        GRANT USAGE ON SCHEMA public TO ledgerbridge_app;
        GRANT USAGE ON TYPE entity_type, account_class, journal_status TO ledgerbridge_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
            entity, account, journal_entry, posting
        TO ledgerbridge_app;
        GRANT SELECT ON TABLE audit_event TO ledgerbridge_app;
        GRANT EXECUTE ON FUNCTION
            append_audit_event(text, text, text, text, jsonb)
        TO ledgerbridge_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE EXECUTE ON FUNCTION
            append_audit_event(text, text, text, text, jsonb)
        FROM ledgerbridge_app;
        REVOKE ALL ON TABLE entity, account, journal_entry, posting, audit_event
        FROM ledgerbridge_app;
        REVOKE USAGE ON TYPE entity_type, account_class, journal_status
        FROM ledgerbridge_app;
        """
    )

    op.execute("DROP TRIGGER journal_entry_posted_complete ON journal_entry")
    op.execute("DROP FUNCTION journal_entry_assert_posted_complete()")
    op.execute("DROP TRIGGER posting_balanced_per_currency ON posting")
    op.execute("DROP FUNCTION posting_assert_balanced()")
    op.execute("DROP TRIGGER posting_posted_immutable ON posting")
    op.execute("DROP FUNCTION posting_block_posted_mutation()")
    op.execute("DROP TRIGGER posting_entity_boundary ON posting")
    op.execute("DROP FUNCTION posting_enforce_entity()")
    op.execute("DROP TRIGGER journal_entry_posted_immutable ON journal_entry")
    op.execute("DROP FUNCTION journal_entry_block_posted_mutation()")
    op.execute("DROP TRIGGER journal_entry_validate_correction ON journal_entry")
    op.execute("DROP FUNCTION journal_entry_validate_relationships()")
    op.execute("DROP FUNCTION append_audit_event(text, text, text, text, jsonb)")
    op.execute("DROP TRIGGER audit_event_no_update_delete ON audit_event")
    op.execute("DROP FUNCTION audit_event_block_mutation()")

    op.drop_index("ix_posting_entry_id", table_name="posting")
    op.drop_index("ix_posting_account_id", table_name="posting")
    op.drop_table("posting")
    op.drop_table("journal_entry")
    op.drop_table("audit_event")
    op.drop_table("account")
    op.drop_table("entity")

    JOURNAL_STATUS.drop(op.get_bind(), checkfirst=True)
    ACCOUNT_CLASS.drop(op.get_bind(), checkfirst=True)
    ENTITY_TYPE.drop(op.get_bind(), checkfirst=True)
