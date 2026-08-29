"""Persist audited candidate-to-candidate evidence links.

Revision ID: 20260829_0019
Revises: 20260829_0018
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260829_0019"
down_revision: str | None = "20260829_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "candidate_evidence_link",
        sa.Column("link_ref", UUID, nullable=False),
        sa.Column("subject_candidate_id", UUID, nullable=False),
        sa.Column("evidence_candidate_id", UUID, nullable=False),
        sa.Column("entity_id", UUID, nullable=False),
        sa.Column("business_unit_id", UUID, nullable=False),
        sa.Column("risk_code", sa.String(64), nullable=False),
        sa.Column("relation", sa.String(64), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("match_basis", postgresql.JSONB, nullable=False),
        sa.Column("audit_event_id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "subject_candidate_id <> evidence_candidate_id",
            name="candidate_evidence_link_distinct_candidates",
        ),
        sa.CheckConstraint(
            "risk_code = 'HOTEL_PAYOUT_STATEMENT_REQUIRED'",
            name="candidate_evidence_link_risk_allowed",
        ),
        sa.CheckConstraint(
            "relation = 'SAME_ECONOMIC_TRANSACTION'",
            name="candidate_evidence_link_relation_allowed",
        ),
        sa.CheckConstraint("amount_minor > 0", name="candidate_evidence_link_amount_positive"),
        sa.CheckConstraint("currency = 'CNY'", name="candidate_evidence_link_currency_cny"),
        sa.CheckConstraint(
            "jsonb_typeof(match_basis) = 'object'",
            name="candidate_evidence_link_basis_object",
        ),
        sa.ForeignKeyConstraint(
            ["subject_candidate_id"], ["candidate.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["evidence_candidate_id"], ["candidate.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["entity.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["business_unit_id", "entity_id"],
            ["business_unit.id", "business_unit.entity_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["audit_event_id"], ["audit_event.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("link_ref", name="pk_candidate_evidence_link"),
        sa.UniqueConstraint(
            "subject_candidate_id", "risk_code", name="uq_candidate_evidence_link_subject_risk"
        ),
        sa.UniqueConstraint(
            "evidence_candidate_id", "risk_code", name="uq_candidate_evidence_link_evidence_risk"
        ),
        sa.UniqueConstraint("audit_event_id", name="uq_candidate_evidence_link_audit"),
    )
    op.create_table(
        "hotel_payout_cutover_receipt",
        sa.Column("cutover_ref", UUID, nullable=False),
        sa.Column("manifest_sha256", sa.LargeBinary(32), nullable=False),
        sa.Column("source_manifest_sha256", sa.LargeBinary(32), nullable=False),
        sa.Column("ignored_candidate_count", sa.Integer(), nullable=False),
        sa.Column("imported_candidate_count", sa.Integer(), nullable=False),
        sa.Column("link_count", sa.Integer(), nullable=False),
        sa.Column("audit_horizon_sequence", sa.BigInteger(), nullable=False),
        sa.Column("audit_horizon_hash", sa.LargeBinary(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(manifest_sha256) = 32 AND "
            "octet_length(source_manifest_sha256) = 32 AND "
            "octet_length(audit_horizon_hash) = 32",
            name="hotel_payout_cutover_receipt_digest_lengths",
        ),
        sa.CheckConstraint(
            "ignored_candidate_count >= 0 AND imported_candidate_count > 0 AND link_count >= 0",
            name="hotel_payout_cutover_receipt_counts",
        ),
        sa.PrimaryKeyConstraint("cutover_ref", name="pk_hotel_payout_cutover_receipt"),
        schema="internal_import",
    )
    op.execute(
        r"""
        CREATE FUNCTION public.r1_candidate_evidence_link_append_only()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION 'candidate_evidence_link is append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $function$;
        CREATE TRIGGER r1_candidate_evidence_link_append_only_trigger
        BEFORE UPDATE OR DELETE ON public.candidate_evidence_link
        FOR EACH ROW EXECUTE FUNCTION public.r1_candidate_evidence_link_append_only();

        CREATE FUNCTION internal_import.hotel_payout_cutover_receipt_append_only()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION 'hotel_payout_cutover_receipt is append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $function$;
        CREATE TRIGGER hotel_payout_cutover_receipt_append_only_trigger
        BEFORE UPDATE OR DELETE ON internal_import.hotel_payout_cutover_receipt
        FOR EACH ROW EXECUTE FUNCTION internal_import.hotel_payout_cutover_receipt_append_only();

        CREATE FUNCTION public.r1_validate_candidate_evidence_link()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_subject_entity uuid;
            v_subject_unit uuid;
            v_subject_amount bigint;
            v_subject_source text;
            v_subject_status text;
            v_evidence_entity uuid;
            v_evidence_unit uuid;
            v_evidence_amount bigint;
            v_evidence_source text;
            v_evidence_status text;
            v_audit_action text;
            v_audit_rule text;
            v_audit_payload jsonb;
        BEGIN
            SELECT c.entity_id, r.business_unit_id, r.amount_minor, cs.source_system_id, r.status
              INTO v_subject_entity, v_subject_unit, v_subject_amount,
                   v_subject_source, v_subject_status
              FROM public.candidate AS c
              JOIN public.candidate_source AS cs ON cs.candidate_id = c.id
              JOIN LATERAL (
                    SELECT revision.business_unit_id, revision.amount_minor, revision.status
                      FROM public.candidate_revision AS revision
                     WHERE revision.candidate_id = c.id
                     ORDER BY revision.revision DESC LIMIT 1
              ) AS r ON true
             WHERE c.id = NEW.subject_candidate_id;
            SELECT c.entity_id, r.business_unit_id, r.amount_minor, cs.source_system_id, r.status
              INTO v_evidence_entity, v_evidence_unit, v_evidence_amount,
                   v_evidence_source, v_evidence_status
              FROM public.candidate AS c
              JOIN public.candidate_source AS cs ON cs.candidate_id = c.id
              JOIN LATERAL (
                    SELECT revision.business_unit_id, revision.amount_minor, revision.status
                      FROM public.candidate_revision AS revision
                     WHERE revision.candidate_id = c.id
                     ORDER BY revision.revision DESC LIMIT 1
              ) AS r ON true
             WHERE c.id = NEW.evidence_candidate_id;
            IF v_subject_entity IS NULL OR v_evidence_entity IS NULL
               OR v_subject_source <> 'hotel_bill_ocr'
               OR v_evidence_source <> 'boc_mail_derived_review'
               OR v_subject_status <> 'PENDING' OR v_evidence_status <> 'PENDING'
               OR v_subject_entity IS DISTINCT FROM NEW.entity_id
               OR v_evidence_entity IS DISTINCT FROM NEW.entity_id
               OR v_subject_unit IS DISTINCT FROM NEW.business_unit_id
               OR v_evidence_unit IS DISTINCT FROM NEW.business_unit_id
               OR v_subject_amount IS DISTINCT FROM NEW.amount_minor
               OR v_evidence_amount IS DISTINCT FROM NEW.amount_minor THEN
                RAISE EXCEPTION 'candidate evidence link facts do not match current candidates'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.match_basis->>'method' <> 'EXACT_AMOUNT_DATE_PLATFORM_ONE_TO_ONE'
               OR NEW.match_basis->>'platform' NOT IN ('CTRIP_EBOOKING','MEITUAN_MOBILE')
               OR coalesce(NEW.match_basis->>'subject_period_start', '')
                    !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
               OR coalesce(NEW.match_basis->>'subject_period_end', '')
                    !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
               OR coalesce(NEW.match_basis->>'evidence_date', '')
                    !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
               OR btrim(coalesce(NEW.match_basis->>'evidence_transaction_ref', '')) = '' THEN
                RAISE EXCEPTION 'candidate evidence link match basis is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT action, rule_version, payload
              INTO v_audit_action, v_audit_rule, v_audit_payload
              FROM public.audit_event WHERE id = NEW.audit_event_id;
            IF v_audit_action IS DISTINCT FROM 'candidate.evidence.match'
               OR v_audit_rule IS DISTINCT FROM 'ledgerbridge.candidate-evidence-link.v1'
               OR v_audit_payload->>'link_ref' IS DISTINCT FROM NEW.link_ref::text
               OR v_audit_payload->>'subject_candidate_id'
                    IS DISTINCT FROM NEW.subject_candidate_id::text
               OR v_audit_payload->>'evidence_candidate_id'
                    IS DISTINCT FROM NEW.evidence_candidate_id::text
               OR v_audit_payload->>'risk_code' IS DISTINCT FROM NEW.risk_code
               OR v_audit_payload->>'amount_minor' IS DISTINCT FROM NEW.amount_minor::text THEN
                RAISE EXCEPTION 'candidate evidence link audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE TRIGGER r1_validate_candidate_evidence_link_trigger
        BEFORE INSERT ON public.candidate_evidence_link
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_evidence_link();

        CREATE FUNCTION internal_read.list_candidate_evidence_satisfactions(
            p_entity_id uuid, p_business_unit_id uuid, p_candidate_ids uuid[],
            p_audit_horizon_sequence bigint, p_audit_horizon_hash bytea
        ) RETURNS TABLE(candidate_id uuid, risk_code varchar(64))
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        BEGIN
            IF p_entity_id IS NULL OR p_business_unit_id IS NULL
               OR p_candidate_ids IS NULL OR cardinality(p_candidate_ids) NOT BETWEEN 1 AND 101
               OR array_position(p_candidate_ids, NULL) IS NOT NULL
               OR p_audit_horizon_sequence IS NULL
               OR p_audit_horizon_hash IS NULL
               OR octet_length(p_audit_horizon_hash) <> 32 THEN
                RAISE EXCEPTION 'candidate evidence satisfaction scope is invalid'
                    USING ERRCODE = '22023';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.audit_event AS horizon
                 WHERE horizon.sequence = p_audit_horizon_sequence
                   AND horizon.hash = p_audit_horizon_hash
            ) THEN
                RAISE EXCEPTION 'audit horizon is not an exact chain row' USING ERRCODE = '22023';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.business_unit AS unit
                 WHERE unit.id = p_business_unit_id AND unit.entity_id = p_entity_id
            ) THEN
                RAISE EXCEPTION 'business unit does not belong to entity' USING ERRCODE = '22023';
            END IF;
            RETURN QUERY
            SELECT link.subject_candidate_id, link.risk_code
              FROM public.candidate_evidence_link AS link
              JOIN public.audit_event AS event ON event.id = link.audit_event_id
             WHERE link.entity_id = p_entity_id
               AND link.business_unit_id = p_business_unit_id
               AND link.subject_candidate_id = ANY(p_candidate_ids)
               AND event.sequence <= p_audit_horizon_sequence
             ORDER BY link.subject_candidate_id, link.risk_code;
        END
        $function$;

        REVOKE ALL ON TABLE public.candidate_evidence_link FROM PUBLIC;
        REVOKE ALL ON TABLE internal_import.hotel_payout_cutover_receipt FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.r1_candidate_evidence_link_append_only(),
            public.r1_validate_candidate_evidence_link(),
            internal_import.hotel_payout_cutover_receipt_append_only(),
            internal_read.list_candidate_evidence_satisfactions(uuid,uuid,uuid[],bigint,bytea)
            FROM PUBLIC;
        DO $acl$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY[
                'ledgerbridge_reader','ledgerbridge_api','ledgerbridge_worker',
                'ledgerbridge_app','ledgerbridge_backup'
            ] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format(
                        'REVOKE ALL ON TABLE public.candidate_evidence_link, '
                        'internal_import.hotel_payout_cutover_receipt FROM %I', role_name
                    );
                END IF;
            END LOOP;
        END
        $acl$;
        GRANT EXECUTE ON FUNCTION internal_read.list_candidate_evidence_satisfactions(
            uuid,uuid,uuid[],bigint,bytea
        ) TO ledgerbridge_reader;
        """
    )


def downgrade() -> None:
    if os.getenv("LEDGERBRIDGE_ENV", "development").lower() == "production":
        raise RuntimeError("candidate evidence links are irreversible in production")
    op.execute(
        "DROP FUNCTION IF EXISTS internal_read.list_candidate_evidence_satisfactions("
        "uuid,uuid,uuid[],bigint,bytea)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS internal_import.hotel_payout_cutover_receipt_append_only()"
    )
    op.execute("DROP FUNCTION IF EXISTS public.r1_validate_candidate_evidence_link()")
    op.execute("DROP FUNCTION IF EXISTS public.r1_candidate_evidence_link_append_only()")
    op.drop_table("hotel_payout_cutover_receipt", schema="internal_import")
    op.drop_table("candidate_evidence_link")
