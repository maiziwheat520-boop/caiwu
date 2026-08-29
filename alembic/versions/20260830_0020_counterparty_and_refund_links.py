"""Persist registry-backed counterparties and unique partial-refund links.

Revision ID: 20260830_0020
Revises: 20260829_0019
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260830_0020"
down_revision: str | None = "20260829_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "counterparty_identity",
        sa.Column("entity_id", UUID, nullable=False),
        sa.Column("counterparty_ref", sa.String(99), nullable=False),
        sa.Column("audit_event_id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "counterparty_ref ~ '^cp_[a-z0-9_]{1,96}$'",
            name="counterparty_identity_ref_format",
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["entity.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["audit_event_id"], ["audit_event.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("entity_id", "counterparty_ref", name="pk_counterparty_identity"),
        sa.UniqueConstraint("audit_event_id", name="uq_counterparty_identity_audit"),
    )
    op.create_table(
        "counterparty_classification",
        sa.Column("entity_id", UUID, nullable=False),
        sa.Column("counterparty_ref", sa.String(99), nullable=False),
        sa.Column("classification_revision", sa.Integer(), nullable=False),
        sa.Column("counterparty_class", sa.String(32), nullable=False),
        sa.Column("audit_event_id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "classification_revision > 0",
            name="counterparty_classification_revision_positive",
        ),
        sa.CheckConstraint(
            "counterparty_class IN ('self_managed','related_party','known_business','unknown')",
            name="counterparty_classification_class_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["entity_id", "counterparty_ref"],
            ["counterparty_identity.entity_id", "counterparty_identity.counterparty_ref"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["audit_event_id"], ["audit_event.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint(
            "entity_id",
            "counterparty_ref",
            "classification_revision",
            name="pk_counterparty_classification",
        ),
        sa.UniqueConstraint("audit_event_id", name="uq_counterparty_classification_audit"),
    )
    op.create_table(
        "candidate_counterparty",
        sa.Column("candidate_id", UUID, nullable=False),
        sa.Column("entity_id", UUID, nullable=False),
        sa.Column("counterparty_ref", sa.String(99), nullable=False),
        sa.Column("audit_event_id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidate.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["entity_id", "counterparty_ref"],
            ["counterparty_identity.entity_id", "counterparty_identity.counterparty_ref"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["audit_event_id"], ["audit_event.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("candidate_id", name="pk_candidate_counterparty"),
        sa.UniqueConstraint("audit_event_id", name="uq_candidate_counterparty_audit"),
    )

    op.drop_constraint(
        "candidate_evidence_link_risk_allowed",
        "candidate_evidence_link",
        type_="check",
    )
    op.create_check_constraint(
        "candidate_evidence_link_risk_allowed",
        "candidate_evidence_link",
        "risk_code IN ('HOTEL_PAYOUT_STATEMENT_REQUIRED','REVERSAL_MATCH_REQUIRED')",
    )
    op.drop_constraint(
        "candidate_evidence_link_relation_allowed",
        "candidate_evidence_link",
        type_="check",
    )
    op.create_check_constraint(
        "candidate_evidence_link_relation_allowed",
        "candidate_evidence_link",
        "relation IN ('SAME_ECONOMIC_TRANSACTION','PARTIAL_REFUND')",
    )

    op.execute(
        r"""
        CREATE FUNCTION public.r1_counterparty_append_only()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION 'counterparty facts are append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $function$;
        CREATE TRIGGER r1_counterparty_identity_append_only_trigger
        BEFORE UPDATE OR DELETE ON public.counterparty_identity
        FOR EACH ROW EXECUTE FUNCTION public.r1_counterparty_append_only();
        CREATE TRIGGER r1_counterparty_classification_append_only_trigger
        BEFORE UPDATE OR DELETE ON public.counterparty_classification
        FOR EACH ROW EXECUTE FUNCTION public.r1_counterparty_append_only();
        CREATE TRIGGER r1_candidate_counterparty_append_only_trigger
        BEFORE UPDATE OR DELETE ON public.candidate_counterparty
        FOR EACH ROW EXECUTE FUNCTION public.r1_counterparty_append_only();

        CREATE FUNCTION public.r1_validate_counterparty_identity()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE v_action text; v_rule text; v_payload jsonb;
        BEGIN
            SELECT action, rule_version, payload
              INTO v_action, v_rule, v_payload
              FROM public.audit_event WHERE id = NEW.audit_event_id;
            IF v_action IS DISTINCT FROM 'counterparty.identity.register'
               OR v_rule IS DISTINCT FROM 'ledgerbridge.controlled-review-import.v1'
               OR v_payload->>'entity_id' IS DISTINCT FROM NEW.entity_id::text
               OR v_payload->>'counterparty_ref' IS DISTINCT FROM NEW.counterparty_ref THEN
                RAISE EXCEPTION 'counterparty identity audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE TRIGGER r1_validate_counterparty_identity_trigger
        BEFORE INSERT ON public.counterparty_identity
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_counterparty_identity();

        CREATE FUNCTION public.r1_validate_counterparty_classification()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE v_expected_revision integer;
                v_action text; v_rule text; v_payload jsonb;
        BEGIN
            SELECT coalesce(max(classification_revision), 0) + 1
              INTO v_expected_revision
              FROM public.counterparty_classification
             WHERE entity_id = NEW.entity_id
               AND counterparty_ref = NEW.counterparty_ref;
            SELECT action, rule_version, payload
              INTO v_action, v_rule, v_payload
              FROM public.audit_event WHERE id = NEW.audit_event_id;
            IF NEW.classification_revision IS DISTINCT FROM v_expected_revision
               OR v_action IS DISTINCT FROM 'counterparty.classify'
               OR v_rule IS DISTINCT FROM 'ledgerbridge.controlled-review-import.v1'
               OR v_payload->>'entity_id' IS DISTINCT FROM NEW.entity_id::text
               OR v_payload->>'counterparty_ref' IS DISTINCT FROM NEW.counterparty_ref
               OR v_payload->>'classification_revision'
                    IS DISTINCT FROM NEW.classification_revision::text
               OR v_payload->>'counterparty_class'
                    IS DISTINCT FROM NEW.counterparty_class THEN
                RAISE EXCEPTION 'counterparty classification audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE TRIGGER r1_validate_counterparty_classification_trigger
        BEFORE INSERT ON public.counterparty_classification
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_counterparty_classification();

        CREATE FUNCTION public.r1_validate_candidate_counterparty()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE v_candidate_entity uuid; v_class text;
                v_action text; v_rule text; v_payload jsonb;
        BEGIN
            SELECT entity_id INTO v_candidate_entity
              FROM public.candidate WHERE id = NEW.candidate_id;
            SELECT counterparty_class INTO v_class
              FROM public.counterparty_classification
             WHERE entity_id = NEW.entity_id
               AND counterparty_ref = NEW.counterparty_ref
             ORDER BY classification_revision DESC LIMIT 1;
            SELECT action, rule_version, payload
              INTO v_action, v_rule, v_payload
              FROM public.audit_event WHERE id = NEW.audit_event_id;
            IF v_candidate_entity IS DISTINCT FROM NEW.entity_id OR v_class IS NULL
               OR v_action IS DISTINCT FROM 'candidate.counterparty.link'
               OR v_rule IS DISTINCT FROM 'ledgerbridge.controlled-review-import.v1'
               OR v_payload->>'candidate_id' IS DISTINCT FROM NEW.candidate_id::text
               OR v_payload->>'entity_id' IS DISTINCT FROM NEW.entity_id::text
               OR v_payload->>'counterparty_ref' IS DISTINCT FROM NEW.counterparty_ref
               OR v_payload->>'counterparty_class' IS DISTINCT FROM v_class THEN
                RAISE EXCEPTION 'candidate counterparty binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE TRIGGER r1_validate_candidate_counterparty_trigger
        BEFORE INSERT ON public.candidate_counterparty
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_counterparty();

        CREATE OR REPLACE FUNCTION public.r1_validate_candidate_evidence_link()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_subject_entity uuid; v_subject_unit uuid; v_subject_amount bigint;
            v_subject_source text; v_subject_status text;
            v_evidence_entity uuid; v_evidence_unit uuid; v_evidence_amount bigint;
            v_evidence_source text; v_evidence_status text;
            v_audit_action text; v_audit_rule text; v_audit_payload jsonb;
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
               OR v_subject_status <> 'PENDING' OR v_evidence_status <> 'PENDING'
               OR v_subject_entity IS DISTINCT FROM NEW.entity_id
               OR v_evidence_entity IS DISTINCT FROM NEW.entity_id
               OR v_subject_unit IS DISTINCT FROM NEW.business_unit_id
               OR v_evidence_unit IS DISTINCT FROM NEW.business_unit_id THEN
                RAISE EXCEPTION 'candidate evidence link facts do not match current candidates'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.risk_code = 'HOTEL_PAYOUT_STATEMENT_REQUIRED' THEN
                IF NEW.relation <> 'SAME_ECONOMIC_TRANSACTION'
                   OR v_subject_source <> 'hotel_bill_ocr'
                   OR v_evidence_source <> 'boc_mail_derived_review'
                   OR v_subject_amount IS DISTINCT FROM NEW.amount_minor
                   OR v_evidence_amount IS DISTINCT FROM NEW.amount_minor
                   OR NEW.match_basis->>'method' <> 'EXACT_AMOUNT_DATE_PLATFORM_ONE_TO_ONE'
                   OR NEW.match_basis->>'platform' NOT IN ('CTRIP_EBOOKING','MEITUAN_MOBILE')
                   OR coalesce(
                        NEW.match_basis->>'subject_period_start', ''
                   ) !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                   OR coalesce(
                        NEW.match_basis->>'subject_period_end', ''
                   ) !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                   OR coalesce(
                        NEW.match_basis->>'evidence_date', ''
                   ) !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                   OR btrim(coalesce(
                        NEW.match_basis->>'evidence_transaction_ref', ''
                   )) = '' THEN
                    RAISE EXCEPTION 'hotel candidate evidence match is invalid'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            ELSIF NEW.risk_code = 'REVERSAL_MATCH_REQUIRED' THEN
                IF NEW.relation <> 'PARTIAL_REFUND'
                   OR v_subject_source <> 'wechat_pay_export'
                   OR v_evidence_source <> 'wechat_pay_export'
                   OR v_subject_amount >= 0
                   OR v_evidence_amount IS DISTINCT FROM NEW.amount_minor
                   OR NEW.amount_minor >= abs(v_subject_amount)
                   OR NEW.match_basis->>'method' <> 'UNIQUE_PLATFORM_PARTIAL_REFUND'
                   OR coalesce(
                        NEW.match_basis->>'original_record_id', ''
                   ) !~ '^WX-[0-9a-f]{12}$'
                   OR coalesce(
                        NEW.match_basis->>'refund_record_id', ''
                   ) !~ '^WX-[0-9a-f]{12}$'
                   OR coalesce(
                        NEW.match_basis->>'original_date', ''
                   ) !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                   OR coalesce(
                        NEW.match_basis->>'refund_date', ''
                   ) !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN
                    RAISE EXCEPTION 'partial refund candidate match is invalid'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            ELSE
                RAISE EXCEPTION 'candidate evidence risk is unsupported'
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
               OR v_audit_payload->>'amount_minor' IS DISTINCT FROM NEW.amount_minor::text
               OR v_audit_payload->'match_basis' IS DISTINCT FROM NEW.match_basis THEN
                RAISE EXCEPTION 'candidate evidence link audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE OR REPLACE FUNCTION internal_read.list_candidate_evidence_satisfactions(
            p_entity_id uuid, p_business_unit_id uuid, p_candidate_ids uuid[],
            p_audit_horizon_sequence bigint, p_audit_horizon_hash bytea
        ) RETURNS TABLE(candidate_id uuid, risk_code varchar(64))
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        BEGIN
            IF p_entity_id IS NULL OR p_business_unit_id IS NULL
               OR p_candidate_ids IS NULL OR cardinality(p_candidate_ids) NOT BETWEEN 1 AND 101
               OR array_position(p_candidate_ids, NULL) IS NOT NULL
               OR p_audit_horizon_sequence IS NULL OR p_audit_horizon_hash IS NULL
               OR octet_length(p_audit_horizon_hash) <> 32 THEN
                RAISE EXCEPTION 'candidate evidence satisfaction scope is invalid'
                    USING ERRCODE = '22023';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.audit_event AS horizon
                 WHERE horizon.sequence = p_audit_horizon_sequence
                   AND horizon.hash = p_audit_horizon_hash
            ) OR NOT EXISTS (
                SELECT 1 FROM public.business_unit AS unit
                 WHERE unit.id = p_business_unit_id AND unit.entity_id = p_entity_id
            ) THEN
                RAISE EXCEPTION 'candidate evidence satisfaction scope is invalid'
                    USING ERRCODE = '22023';
            END IF;
            RETURN QUERY
            SELECT DISTINCT endpoint.candidate_id, link.risk_code
              FROM public.candidate_evidence_link AS link
              JOIN public.audit_event AS event ON event.id = link.audit_event_id
              CROSS JOIN LATERAL (
                    VALUES (link.subject_candidate_id),
                           (CASE WHEN link.risk_code = 'REVERSAL_MATCH_REQUIRED'
                                 THEN link.evidence_candidate_id END)
              ) AS endpoint(candidate_id)
             WHERE link.entity_id = p_entity_id
               AND link.business_unit_id = p_business_unit_id
               AND endpoint.candidate_id = ANY(p_candidate_ids)
               AND event.sequence <= p_audit_horizon_sequence
             ORDER BY endpoint.candidate_id, link.risk_code;
        END
        $function$;

        CREATE FUNCTION internal_read.list_candidate_counterparty_facts(
            p_entity_id uuid, p_business_unit_id uuid, p_candidate_ids uuid[],
            p_audit_horizon_sequence bigint, p_audit_horizon_hash bytea
        ) RETURNS TABLE(
            candidate_id uuid, counterparty_ref varchar(99),
            counterparty_class varchar(32)
        )
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        BEGIN
            IF p_entity_id IS NULL OR p_business_unit_id IS NULL
               OR p_candidate_ids IS NULL OR cardinality(p_candidate_ids) NOT BETWEEN 1 AND 101
               OR array_position(p_candidate_ids, NULL) IS NOT NULL
               OR p_audit_horizon_sequence IS NULL OR p_audit_horizon_hash IS NULL
               OR octet_length(p_audit_horizon_hash) <> 32 THEN
                RAISE EXCEPTION 'candidate counterparty scope is invalid'
                    USING ERRCODE = '22023';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.audit_event AS horizon
                 WHERE horizon.sequence = p_audit_horizon_sequence
                   AND horizon.hash = p_audit_horizon_hash
            ) OR NOT EXISTS (
                SELECT 1 FROM public.business_unit AS unit
                 WHERE unit.id = p_business_unit_id AND unit.entity_id = p_entity_id
            ) THEN
                RAISE EXCEPTION 'candidate counterparty scope is invalid'
                    USING ERRCODE = '22023';
            END IF;
            RETURN QUERY
            SELECT cc.candidate_id, cc.counterparty_ref, classification.counterparty_class
              FROM public.candidate_counterparty AS cc
              JOIN public.counterparty_identity AS identity
                ON identity.entity_id = cc.entity_id
               AND identity.counterparty_ref = cc.counterparty_ref
              JOIN LATERAL (
                    SELECT fact.counterparty_class
                      FROM public.counterparty_classification AS fact
                      JOIN public.audit_event AS fact_audit
                        ON fact_audit.id = fact.audit_event_id
                     WHERE fact.entity_id = cc.entity_id
                       AND fact.counterparty_ref = cc.counterparty_ref
                       AND fact_audit.sequence <= p_audit_horizon_sequence
                     ORDER BY classification_revision DESC LIMIT 1
              ) AS classification ON true
              JOIN public.candidate AS candidate ON candidate.id = cc.candidate_id
              JOIN LATERAL (
                    SELECT revision.business_unit_id
                      FROM public.candidate_revision AS revision
                     WHERE revision.candidate_id = candidate.id
                     ORDER BY revision.revision DESC LIMIT 1
              ) AS current_revision ON true
              JOIN public.audit_event AS cc_audit ON cc_audit.id = cc.audit_event_id
              JOIN public.audit_event AS identity_audit
                ON identity_audit.id = identity.audit_event_id
             WHERE cc.entity_id = p_entity_id
               AND current_revision.business_unit_id = p_business_unit_id
               AND cc.candidate_id = ANY(p_candidate_ids)
               AND cc_audit.sequence <= p_audit_horizon_sequence
               AND identity_audit.sequence <= p_audit_horizon_sequence
             ORDER BY cc.candidate_id;
        END
        $function$;

        REVOKE ALL ON TABLE public.counterparty_identity,
            public.counterparty_classification, public.candidate_counterparty FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.r1_counterparty_append_only(),
            public.r1_validate_counterparty_identity(),
            public.r1_validate_counterparty_classification(),
            public.r1_validate_candidate_counterparty(),
            internal_read.list_candidate_counterparty_facts(uuid,uuid,uuid[],bigint,bytea)
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
                        'REVOKE ALL ON TABLE public.counterparty_identity, '
                        'public.counterparty_classification, '
                        'public.candidate_counterparty FROM %I', role_name
                    );
                END IF;
            END LOOP;
        END
        $acl$;
        GRANT EXECUTE ON FUNCTION internal_read.list_candidate_counterparty_facts(
            uuid,uuid,uuid[],bigint,bytea
        ) TO ledgerbridge_reader;
        """
    )


def downgrade() -> None:
    if os.getenv("LEDGERBRIDGE_ENV", "development").lower() == "production":
        raise RuntimeError("counterparty and partial-refund facts are irreversible in production")
    connection = op.get_bind()
    if connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM public.counterparty_identity) "
            "OR EXISTS (SELECT 1 FROM public.candidate_evidence_link "
            "WHERE risk_code = 'REVERSAL_MATCH_REQUIRED')"
        )
    ).scalar_one():
        raise RuntimeError("development downgrade would discard persisted review facts")
    op.execute(
        "DROP FUNCTION IF EXISTS internal_read.list_candidate_counterparty_facts("
        "uuid,uuid,uuid[],bigint,bytea)"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS r1_validate_candidate_counterparty_trigger "
        "ON public.candidate_counterparty"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS r1_validate_counterparty_classification_trigger "
        "ON public.counterparty_classification"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS r1_validate_counterparty_identity_trigger "
        "ON public.counterparty_identity"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS r1_candidate_counterparty_append_only_trigger "
        "ON public.candidate_counterparty"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS r1_counterparty_classification_append_only_trigger "
        "ON public.counterparty_classification"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS r1_counterparty_identity_append_only_trigger "
        "ON public.counterparty_identity"
    )
    op.execute("DROP FUNCTION IF EXISTS public.r1_validate_candidate_counterparty()")
    op.execute("DROP FUNCTION IF EXISTS public.r1_validate_counterparty_classification()")
    op.execute("DROP FUNCTION IF EXISTS public.r1_validate_counterparty_identity()")
    op.execute("DROP FUNCTION IF EXISTS public.r1_counterparty_append_only()")
    op.drop_table("candidate_counterparty")
    op.drop_table("counterparty_classification")
    op.drop_table("counterparty_identity")
    op.drop_constraint(
        "candidate_evidence_link_risk_allowed",
        "candidate_evidence_link",
        type_="check",
    )
    op.create_check_constraint(
        "candidate_evidence_link_risk_allowed",
        "candidate_evidence_link",
        "risk_code = 'HOTEL_PAYOUT_STATEMENT_REQUIRED'",
    )
    op.drop_constraint(
        "candidate_evidence_link_relation_allowed",
        "candidate_evidence_link",
        type_="check",
    )
    op.create_check_constraint(
        "candidate_evidence_link_relation_allowed",
        "candidate_evidence_link",
        "relation = 'SAME_ECONOMIC_TRANSACTION'",
    )
