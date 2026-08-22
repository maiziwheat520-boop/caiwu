"""Add canonical ingestion and financial source registries.

Revision ID: 20260822_0004
Revises: 20260821_0003
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260822_0004"
down_revision: str | None = "20260821_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingest_channel",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "id ~ '^[a-z][a-z0-9_]{0,63}$'",
            name=op.f("ck_ingest_channel_ingest_channel_id_canonical"),
        ),
        sa.CheckConstraint(
            "btrim(description) <> ''",
            name=op.f("ck_ingest_channel_ingest_channel_description_not_blank"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingest_channel")),
    )
    op.create_table(
        "source_system",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "id ~ '^[a-z][a-z0-9_]{0,63}$'",
            name=op.f("ck_source_system_source_system_id_canonical"),
        ),
        sa.CheckConstraint(
            "btrim(description) <> ''",
            name=op.f("ck_source_system_source_system_description_not_blank"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_system")),
    )

    op.execute(
        """
        INSERT INTO public.ingest_channel (id, description)
        VALUES
            ('manual_upload', 'Operator-provided upload channel'),
            ('synthetic_upload', 'Synthetic test-only upload channel');

        INSERT INTO public.source_system (id, description)
        VALUES ('synthetic', 'Synthetic test-only financial source');
        """
    )
    op.execute(
        """
        DO $ledgerbridge$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.raw_artifact AS artifact
                LEFT JOIN public.ingest_channel AS channel
                  ON channel.id = artifact.source
                WHERE channel.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'Phase 3 requires every raw artifact source to be a registered channel';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM public.source_record AS record
                LEFT JOIN public.source_system AS system
                  ON system.id = record.source
                WHERE system.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'Phase 3 requires every source record source to be a registered system';
            END IF;
        END
        $ledgerbridge$;
        """
    )

    op.alter_column(
        "raw_artifact",
        "source",
        existing_type=sa.String(length=200),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "source_record",
        "source",
        existing_type=sa.String(length=200),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.create_foreign_key(
        "fk_raw_artifact_source_ingest_channel",
        "raw_artifact",
        "ingest_channel",
        ["source"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_source_record_source_source_system",
        "source_record",
        "source_system",
        ["source"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.execute(
        """
        CREATE FUNCTION public.registry_block_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION 'canonical source registries are append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $function$;

        CREATE TRIGGER ingest_channel_no_update_delete
        BEFORE UPDATE OR DELETE ON public.ingest_channel
        FOR EACH ROW EXECUTE FUNCTION public.registry_block_mutation();

        CREATE TRIGGER source_system_no_update_delete
        BEFORE UPDATE OR DELETE ON public.source_system
        FOR EACH ROW EXECUTE FUNCTION public.registry_block_mutation();

        REVOKE ALL ON TABLE public.ingest_channel, public.source_system FROM PUBLIC;
        GRANT SELECT ON TABLE public.ingest_channel, public.source_system
            TO ledgerbridge_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $ledgerbridge$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.raw_artifact)
               OR EXISTS (SELECT 1 FROM public.source_record)
               OR EXISTS (
                    SELECT 1 FROM public.ingest_channel
                    WHERE id NOT IN ('manual_upload', 'synthetic_upload')
               )
               OR EXISTS (
                    SELECT 1 FROM public.source_system
                    WHERE id <> 'synthetic'
               ) THEN
                RAISE EXCEPTION
                    'Phase 3 registry data prevents destructive downgrade';
            END IF;
        END
        $ledgerbridge$;

        DO $ledgerbridge$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_app') THEN
                REVOKE ALL ON TABLE public.ingest_channel, public.source_system
                    FROM ledgerbridge_app;
            END IF;
        END
        $ledgerbridge$;
        """
    )

    op.drop_constraint(
        "fk_source_record_source_source_system",
        "source_record",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_raw_artifact_source_ingest_channel",
        "raw_artifact",
        type_="foreignkey",
    )
    op.alter_column(
        "source_record",
        "source",
        existing_type=sa.String(length=64),
        type_=sa.String(length=200),
        existing_nullable=False,
    )
    op.alter_column(
        "raw_artifact",
        "source",
        existing_type=sa.String(length=64),
        type_=sa.String(length=200),
        existing_nullable=False,
    )

    op.execute("DROP TRIGGER source_system_no_update_delete ON public.source_system")
    op.execute("DROP TRIGGER ingest_channel_no_update_delete ON public.ingest_channel")
    op.execute("DROP FUNCTION public.registry_block_mutation()")
    op.drop_table("source_system")
    op.drop_table("ingest_channel")
