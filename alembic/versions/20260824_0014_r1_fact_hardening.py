# ruff: noqa: E501

"""Harden the R1 fact tables before installing the reader surface.

Migration A/B were intentionally small schema foundations.  This forward-only
correction adds the composite identity and scope facts that the read surface
relies on, validates existing POSTED ownership before making it mandatory, and
installs deferred checks for new canonical writes.  The migration never creates
roles or credentials; all runtime roles remain owner-written only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260824_0014"
down_revision: str | None = "20260824_0013"
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


def _revoke_fact_writes() -> None:
    op.execute(
        """
        DO $grant$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY[
                'ledgerbridge_reader', 'ledgerbridge_api',
                'ledgerbridge_worker', 'ledgerbridge_app'
            ] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format(
                        'REVOKE ALL ON TABLE '
                        'public.encrypted_object_identity, '
                        'public.reconciliation_snapshot_blocker, '
                        'public.business_unit, public.reporting_category, '
                        'public.evidence_object, public.encrypted_blob_version, '
                        'public.candidate, public.candidate_source, '
                        'public.candidate_revision, public.candidate_blocker, '
                        'public.candidate_event, public.candidate_field_change, '
                        'public.candidate_conflict_resolution, '
                        'public.candidate_evidence, '
                        'public.journal_entry_attribution, public.posting_attribution, '
                        'public.reconciliation_snapshot, '
                        'public.reconciliation_snapshot_proposal, '
                        'public.reconciliation_snapshot_suspense FROM %I',
                        role_name
                    );
                END IF;
            END LOOP;
        END
        $grant$;
        REVOKE ALL ON TABLE
            public.encrypted_object_identity,
            public.reconciliation_snapshot_blocker,
            public.business_unit, public.reporting_category,
            public.evidence_object, public.encrypted_blob_version,
            public.candidate, public.candidate_source,
            public.candidate_revision, public.candidate_blocker,
            public.candidate_event, public.candidate_field_change,
            public.candidate_conflict_resolution, public.candidate_evidence,
            public.journal_entry_attribution, public.posting_attribution,
            public.reconciliation_snapshot,
            public.reconciliation_snapshot_proposal,
            public.reconciliation_snapshot_suspense
        FROM PUBLIC;
        """
    )


def upgrade() -> None:
    # The old 0012 table used a global object_ref UNIQUE.  Keep the upgrade
    # safe for an already populated database, but refuse contradictory legacy
    # ownership instead of guessing which evidence owns a reference.
    op.execute(
        """
        DO $preflight$
        BEGIN
            IF EXISTS (
                SELECT object_ref
                FROM public.encrypted_blob_version
                GROUP BY object_ref
                HAVING count(DISTINCT evidence_ref) > 1
            ) THEN
                RAISE EXCEPTION
                    'encrypted object identity has contradictory legacy evidence ownership'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END
        $preflight$;
        """
    )
    # Do not install a stricter FK/trigger surface over an already-corrupt
    # database.  This preflight is deliberately read-only and checks the
    # complete legacy graph before any 0014 DDL runs.
    op.execute(
        """
        DO $r1_existing_preflight$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM public.encrypted_blob_version AS b
                  LEFT JOIN public.encrypted_blob_version AS p
                    ON p.blob_ref = b.predecessor_blob_ref
                  LEFT JOIN public.audit_event AS a ON a.id = b.audit_event_id
                 WHERE (b.predecessor_blob_ref IS NOT NULL
                        AND p.evidence_ref IS DISTINCT FROM b.evidence_ref)
                    OR a.id IS NULL
                    OR a.action IS DISTINCT FROM 'evidence.blob.version'
                    OR a.payload IS DISTINCT FROM jsonb_build_object(
                        'rotation_mode', a.payload ->> 'rotation_mode',
                        'blob_ref', b.blob_ref::text,
                        'evidence_ref', b.evidence_ref::text,
                        'predecessor_blob_ref', b.predecessor_blob_ref::text,
                        'object_ref', b.object_ref,
                        'ciphertext_sha256', encode(b.ciphertext_sha256, 'hex'),
                        'ciphertext_size', b.ciphertext_size,
                        'storage_key', b.storage_key,
                        'envelope_schema', b.envelope_schema,
                        'algorithm', b.algorithm,
                        'chunk_size', b.chunk_size,
                        'stream_header', encode(b.stream_header, 'hex'),
                        'wrapped_key_generation', b.wrapped_key_generation,
                        'wrapped_key_nonce', encode(b.wrapped_key_nonce, 'hex'),
                        'wrapped_key_ciphertext', encode(b.wrapped_key_ciphertext, 'hex'),
                        'purpose', b.purpose
                    )
            ) THEN
                RAISE EXCEPTION 'existing encrypted blob audit or lineage is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.encrypted_blob_version
                 WHERE predecessor_blob_ref IS NOT NULL
                 GROUP BY predecessor_blob_ref
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION 'existing encrypted blob chain branches'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.encrypted_blob_version AS b
                 WHERE (b.predecessor_blob_ref IS NULL
                        AND (b.audit_event_id IS NULL
                             OR (SELECT payload ->> 'rotation_mode'
                                   FROM public.audit_event
                                  WHERE id = b.audit_event_id) IS DISTINCT FROM 'GENESIS'))
                    OR (b.predecessor_blob_ref IS NOT NULL
                        AND (SELECT payload ->> 'rotation_mode'
                               FROM public.audit_event
                              WHERE id = b.audit_event_id) NOT IN ('REWRAP','REENCRYPT'))
            ) THEN
                RAISE EXCEPTION 'existing encrypted blob rotation mode is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.encrypted_blob_version AS b
                 WHERE b.evidence_ref IS NOT NULL
                 GROUP BY b.evidence_ref
                HAVING count(*) FILTER (WHERE b.predecessor_blob_ref IS NULL) <> 1
                    OR count(*) FILTER (WHERE NOT EXISTS (
                        SELECT 1 FROM public.encrypted_blob_version AS child
                         WHERE child.predecessor_blob_ref = b.blob_ref
                    )) <> 1
            ) THEN
                RAISE EXCEPTION 'existing encrypted evidence chain lacks one genesis or tip'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                WITH RECURSIVE walk(start_ref, node_ref, path, cycle) AS (
                    SELECT b.blob_ref, b.predecessor_blob_ref,
                           ARRAY[b.blob_ref], false
                      FROM public.encrypted_blob_version AS b
                     WHERE b.predecessor_blob_ref IS NOT NULL
                    UNION ALL
                    SELECT w.start_ref, b.predecessor_blob_ref,
                           w.path || b.blob_ref,
                           b.blob_ref = ANY(w.path)
                      FROM walk AS w
                      JOIN public.encrypted_blob_version AS b
                        ON b.blob_ref = w.node_ref
                     WHERE w.node_ref IS NOT NULL AND NOT w.cycle
                )
                SELECT 1 FROM walk WHERE cycle
            ) THEN
                RAISE EXCEPTION 'existing encrypted blob chain contains a cycle'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM public.candidate_evidence AS ce
                  JOIN public.candidate AS c ON c.id = ce.candidate_id
                  JOIN public.evidence_object AS e ON e.evidence_ref = ce.evidence_ref
                  LEFT JOIN LATERAL (
                      SELECT cr.business_unit_id
                        FROM public.candidate_revision AS cr
                       WHERE cr.candidate_id = c.id
                       ORDER BY cr.revision DESC LIMIT 1
                  ) AS tip ON TRUE
                 WHERE ce.candidate_entity_id IS DISTINCT FROM c.entity_id
                    OR ce.evidence_entity_id IS DISTINCT FROM e.entity_id
                    OR ce.evidence_business_unit_id IS DISTINCT FROM e.business_unit_id
                    OR c.entity_id IS DISTINCT FROM e.entity_id
                    OR (tip.business_unit_id IS NOT NULL
                        AND tip.business_unit_id IS DISTINCT FROM e.business_unit_id)
            ) THEN
                RAISE EXCEPTION 'existing candidate evidence scope is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.candidate AS c
                  LEFT JOIN public.candidate_source AS cs ON cs.candidate_id = c.id
                  LEFT JOIN (
                      SELECT candidate_id, count(*) AS evidence_count
                        FROM public.candidate_evidence GROUP BY candidate_id
                  ) AS ec ON ec.candidate_id = c.id
                 WHERE cs.candidate_id IS NULL OR coalesce(ec.evidence_count, 0) < 1
            ) THEN
                RAISE EXCEPTION 'existing candidate creation lacks source or evidence'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.candidate_revision AS r
                  LEFT JOIN public.candidate_event AS e
                    ON e.candidate_id = r.candidate_id
                   AND e.to_revision = r.revision
                   AND e.to_status = r.status
                 GROUP BY r.candidate_id, r.revision
                HAVING count(e.event_ref) <> 1
            ) THEN
                RAISE EXCEPTION 'existing candidate revision lacks exactly one typed event'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.candidate_event AS e
                  LEFT JOIN public.candidate_revision AS r
                    ON r.candidate_id = e.candidate_id
                   AND r.revision = e.to_revision
                   AND r.status = e.to_status
                 WHERE r.candidate_id IS NULL
            ) THEN
                RAISE EXCEPTION 'existing candidate event has no typed revision'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.candidate_event AS e
                  LEFT JOIN public.audit_event AS a ON a.id = e.audit_event_id
                 WHERE a.id IS NULL
                    OR a.action IS DISTINCT FROM CASE WHEN e.event_type = 'CREATE'
                                                       THEN 'candidate.create'
                                                       ELSE 'candidate.transition' END
                    OR a.payload IS DISTINCT FROM jsonb_build_object(
                        'event_ref', e.event_ref::text,
                        'candidate_id', e.candidate_id::text,
                        'candidate_ref', e.candidate_id::text,
                        'operation_id', e.operation_id::text,
                        'command_fingerprint', encode(e.command_fingerprint, 'hex'),
                        'event_type', e.event_type,
                        'action', e.action,
                        'from_revision', e.from_revision,
                        'to_revision', e.to_revision,
                        'from_status', e.from_status,
                        'to_status', e.to_status,
                        'field_changes', coalesce((
                            SELECT jsonb_agg(jsonb_build_object(
                                'field', fc.field,
                                'previous_value', fc.previous_value,
                                'new_value', fc.new_value
                            ) ORDER BY fc.field)
                              FROM public.candidate_field_change AS fc
                             WHERE fc.event_ref = e.event_ref
                        ), '[]'::jsonb),
                        'conflict_resolutions', coalesce((
                            SELECT jsonb_agg(jsonb_build_object(
                                'conflict_ref', cr.conflict_ref,
                                'resolution', cr.resolution
                            ) ORDER BY cr.conflict_ref)
                              FROM public.candidate_conflict_resolution AS cr
                             WHERE cr.event_ref = e.event_ref
                        ), '[]'::jsonb),
                        'actor_ref', e.actor_ref,
                        'reason', e.reason,
                        'derived_candidate_id', e.derived_candidate_id::text
                    )
            ) THEN
                RAISE EXCEPTION 'existing candidate event audit payload is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.candidate_event AS e
                 WHERE (e.event_type = 'CREATE'
                        AND (e.to_revision <> 1 OR e.from_revision IS NOT NULL
                             OR e.from_status IS NOT NULL OR e.action IS NOT NULL
                             OR e.derived_candidate_id IS NOT NULL))
                    OR (e.event_type <> 'CREATE'
                        AND (e.from_revision IS NULL OR e.from_status IS NULL
                             OR e.action IS DISTINCT FROM e.event_type
                             OR e.to_revision <> e.from_revision + 1
                             OR NOT (
                                 (e.from_status = 'INCOMPLETE' AND e.to_status = 'PENDING'
                                  AND e.action = 'COMPLETE_FIELDS')
                              OR (e.from_status = 'CONFLICTED' AND e.to_status = 'PENDING'
                                  AND e.action = 'RESOLVE_CONFLICT')
                              OR (e.from_status = 'PENDING' AND e.to_status = 'CONFIRMED'
                                  AND e.action = 'CONFIRM')
                              OR (e.from_status IN ('INCOMPLETE','CONFLICTED','PENDING')
                                  AND e.to_status = 'IGNORED' AND e.action = 'IGNORE')
                              OR (e.from_status = 'CONFIRMED' AND e.to_status = 'SUPERSEDED'
                                  AND e.action = 'SUPERSEDE')
                             )))
            ) THEN
                RAISE EXCEPTION 'existing candidate event history has an invalid edge'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.candidate_source AS cs
                  JOIN public.source_record AS sr ON sr.id = cs.source_record_id
                  JOIN public.raw_artifact AS ra ON ra.id = sr.artifact_id
                 WHERE cs.source_record_id IS NOT NULL
                   AND (cs.source_system_id IS DISTINCT FROM sr.source
                        OR cs.ingest_channel_id IS DISTINCT FROM ra.source)
            ) THEN
                RAISE EXCEPTION 'existing candidate source provenance is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.candidate_revision AS r
                  JOIN public.candidate AS c ON c.id = r.candidate_id
                  LEFT JOIN public.business_unit AS b ON b.id = r.business_unit_id
                  LEFT JOIN public.reporting_category AS rc ON rc.id = r.category_id
                 WHERE (r.business_unit_id IS NOT NULL
                        AND (b.entity_id IS DISTINCT FROM c.entity_id
                             OR b.ref IS DISTINCT FROM r.business_unit_ref_snapshot
                             OR b.label IS DISTINCT FROM r.business_unit_label_snapshot))
                    OR (r.category_id IS NOT NULL
                        AND (rc.entity_id IS DISTINCT FROM c.entity_id
                             OR rc.code IS DISTINCT FROM r.category_code_snapshot
                             OR rc.label IS DISTINCT FROM r.category_label_snapshot))
            ) THEN
                RAISE EXCEPTION 'existing candidate revision dimensions are invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.candidate_blocker AS cb
                  JOIN public.candidate_revision AS r
                    ON r.candidate_id = cb.candidate_id AND r.revision = cb.revision
                  JOIN public.candidate AS c ON c.id = cb.candidate_id
                  JOIN public.evidence_object AS e ON e.evidence_ref = cb.evidence_ref
                 WHERE e.entity_id IS DISTINCT FROM c.entity_id
                    OR (r.business_unit_id IS NOT NULL
                        AND e.business_unit_id IS DISTINCT FROM r.business_unit_id)
            ) THEN
                RAISE EXCEPTION 'existing candidate blocker evidence is outside scope'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM public.reconciliation_snapshot AS s
                  LEFT JOIN public.audit_event AS a ON a.id = s.audit_event_id
                 WHERE a.id IS NULL
                    OR a.action IS DISTINCT FROM 'reconciliation.snapshot'
                    OR a.payload IS DISTINCT FROM jsonb_build_object(
                        'snapshot_ref', s.snapshot_ref::text,
                        'entity_id', s.entity_id::text,
                        'business_unit_id', s.business_unit_id::text,
                        'accounting_month', to_char(s.accounting_month, 'YYYY-MM-DD'),
                        'snapshot_revision', s.snapshot_revision,
                        'ledger_audit_sequence', s.ledger_audit_sequence,
                        'ledger_audit_hash', encode(s.ledger_audit_hash, 'hex'),
                        'posted_amount_minor', s.posted_amount_minor,
                        'currency', s.currency,
                        'blocker_count', (SELECT count(*) FROM public.reconciliation_snapshot_blocker b WHERE b.snapshot_ref = s.snapshot_ref),
                        'proposal_count', (SELECT count(*) FROM public.reconciliation_snapshot_proposal p WHERE p.snapshot_ref = s.snapshot_ref),
                        'suspense_count', (SELECT count(*) FROM public.reconciliation_snapshot_suspense x WHERE x.snapshot_ref = s.snapshot_ref)
                    )
                    OR NOT EXISTS (
                        SELECT 1 FROM public.audit_event AS watermark
                         WHERE watermark.sequence = s.ledger_audit_sequence
                           AND watermark.hash = s.ledger_audit_hash
                    )
            ) THEN
                RAISE EXCEPTION 'existing reconciliation snapshot audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM public.journal_entry AS je
                  LEFT JOIN public.journal_entry_attribution AS ja ON ja.entry_id = je.id
                 WHERE je.status = 'POSTED'
                 GROUP BY je.id HAVING count(ja.entry_id) <> 1
            ) OR EXISTS (
                SELECT 1
                  FROM public.posting AS p
                  JOIN public.journal_entry AS je ON je.id = p.entry_id
                  LEFT JOIN public.posting_attribution AS pa ON pa.posting_id = p.id
                 WHERE je.status = 'POSTED'
                 GROUP BY p.id HAVING count(pa.posting_id) <> 1
            ) OR EXISTS (
                SELECT 1
                  FROM public.journal_entry_attribution AS ja
                  JOIN public.journal_entry AS je ON je.id = ja.entry_id
                 WHERE ja.entity_id IS DISTINCT FROM je.entity_id
            ) OR EXISTS (
                SELECT 1
                  FROM public.posting_attribution AS pa
                  JOIN public.reporting_category AS rc ON rc.id = pa.reporting_category_id
                  JOIN public.posting AS p ON p.id = pa.posting_id
                  JOIN public.journal_entry AS je ON je.id = p.entry_id
                 WHERE rc.entity_id IS DISTINCT FROM je.entity_id
                    OR pa.category_code_snapshot IS DISTINCT FROM rc.code
                    OR pa.category_label_snapshot IS DISTINCT FROM rc.label
            ) THEN
                RAISE EXCEPTION 'existing POSTED attribution or category scope is incomplete'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.reconciliation_leg AS l
                 WHERE l.entity_id IS NULL OR l.business_unit_id IS NULL
                    OR l.accounting_month IS NULL OR l.is_primary IS NULL
            ) OR EXISTS (
                SELECT reconciliation_group_id
                  FROM public.reconciliation_leg
                 GROUP BY reconciliation_group_id
                HAVING count(*) FILTER (WHERE is_primary IS TRUE) <> 1
                    OR count(DISTINCT entity_id) <> 1
                    OR count(DISTINCT business_unit_id) <> 1
                    OR count(DISTINCT accounting_month) <> 1
            ) OR EXISTS (
                SELECT 1
                  FROM public.reconciliation_leg AS l
                  JOIN public.posting AS p ON p.id = l.posting_id
                  JOIN public.journal_entry AS je ON je.id = p.entry_id
                  LEFT JOIN public.journal_entry_attribution AS ja ON ja.entry_id = je.id
                 WHERE (l.is_primary IS TRUE OR l.posting_id IS NOT NULL)
                   AND (p.account_id IS DISTINCT FROM je.primary_account_id
                        OR je.entity_id IS DISTINCT FROM l.entity_id
                        OR ja.business_unit_id IS DISTINCT FROM l.business_unit_id
                        OR ja.accounting_month IS DISTINCT FROM l.accounting_month)
            ) THEN
                RAISE EXCEPTION 'existing reconciliation scope or primary posting is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END
        $r1_existing_preflight$;
        """
    )
    # 0012's trigger used a local variable named candidate_entity_id.  Once
    # 0014 adds the same-named scope column to candidate_evidence, PostgreSQL
    # resolves that unqualified reference as ambiguous.  Replace the trigger
    # body with an explicitly named local so existing revision inserts remain
    # valid under the hardened schema.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.r1_validate_revision_dimensions()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_candidate_entity_id uuid;
            unit_entity uuid;
            category_entity uuid;
            unit_ref text;
            unit_label text;
            category_code text;
            category_label text;
        BEGIN
            SELECT c.entity_id INTO v_candidate_entity_id
              FROM public.candidate AS c
             WHERE c.id = NEW.candidate_id;
            IF NEW.business_unit_id IS NOT NULL THEN
                SELECT b.entity_id, b.ref, b.label INTO unit_entity, unit_ref, unit_label
                  FROM public.business_unit AS b WHERE b.id = NEW.business_unit_id;
                IF unit_entity IS NULL OR unit_entity <> v_candidate_entity_id
                   OR unit_ref <> NEW.business_unit_ref_snapshot
                   OR unit_label <> NEW.business_unit_label_snapshot THEN
                    RAISE EXCEPTION 'candidate business unit scope or snapshot is invalid'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            IF NEW.category_id IS NOT NULL THEN
                SELECT r.entity_id, r.code, r.label INTO category_entity, category_code, category_label
                  FROM public.reporting_category AS r WHERE r.id = NEW.category_id;
                IF category_entity IS NULL OR category_entity <> v_candidate_entity_id
                   OR category_code <> NEW.category_code_snapshot
                   OR category_label <> NEW.category_label_snapshot THEN
                    RAISE EXCEPTION 'candidate reporting category scope or snapshot is invalid'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            IF EXISTS (
                SELECT 1
                 FROM public.candidate_evidence AS ce
                  JOIN public.evidence_object AS eo ON eo.evidence_ref = ce.evidence_ref
                 WHERE ce.candidate_id = NEW.candidate_id
                   AND (eo.entity_id IS DISTINCT FROM v_candidate_entity_id
                        OR ce.candidate_entity_id IS DISTINCT FROM v_candidate_entity_id
                        OR ce.evidence_entity_id IS DISTINCT FROM eo.entity_id
                        OR ce.evidence_business_unit_id IS DISTINCT FROM eo.business_unit_id
                        OR (NEW.business_unit_id IS NOT NULL
                            AND eo.business_unit_id IS DISTINCT FROM NEW.business_unit_id))
            ) THEN
                RAISE EXCEPTION 'candidate revision conflicts with linked evidence scope'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.candidate_blocker AS cb
                  JOIN public.evidence_object AS eo ON eo.evidence_ref = cb.evidence_ref
                 WHERE cb.candidate_id = NEW.candidate_id
                   AND cb.revision = NEW.revision
                   AND (eo.entity_id IS DISTINCT FROM v_candidate_entity_id
                        OR (NEW.business_unit_id IS NOT NULL
                            AND eo.business_unit_id IS DISTINCT FROM NEW.business_unit_id))
            ) THEN
                RAISE EXCEPTION 'candidate blocker evidence is outside revision scope'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        """
    )
    op.create_table(
        "encrypted_object_identity",
        sa.Column("object_ref", sa.String(64), nullable=False),
        sa.Column("evidence_ref", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "object_ref ~ '^[0-9a-f]{64}$'",
            name="encrypted_object_identity_ref_shape",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_ref"], ["evidence_object.evidence_ref"], ondelete="NO ACTION"
        ),
        sa.PrimaryKeyConstraint("object_ref", name="pk_encrypted_object_identity"),
        sa.UniqueConstraint(
            "object_ref", "evidence_ref", name="uq_encrypted_object_identity_ref_evidence"
        ),
    )
    op.execute(
        """
        INSERT INTO public.encrypted_object_identity (object_ref, evidence_ref, created_at)
        SELECT object_ref, evidence_ref, min(created_at)
        FROM public.encrypted_blob_version
        GROUP BY object_ref, evidence_ref;
        """
    )
    op.drop_constraint("uq_encrypted_blob_object_ref", "encrypted_blob_version", type_="unique")
    op.create_foreign_key(
        "fk_encrypted_blob_object_identity",
        "encrypted_blob_version",
        "encrypted_object_identity",
        ["object_ref", "evidence_ref"],
        ["object_ref", "evidence_ref"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_check_constraint(
        "encrypted_blob_ciphertext_size_positive",
        "encrypted_blob_version",
        "ciphertext_size BETWEEN 1 AND 268435456",
    )
    op.execute(
        """
        ALTER TABLE public.encrypted_blob_version
            ADD CONSTRAINT uq_encrypted_blob_evidence_predecessor
            UNIQUE NULLS NOT DISTINCT (evidence_ref, predecessor_blob_ref)
            DEFERRABLE INITIALLY DEFERRED;
        """
    )

    # These lineage columns are nullable only for old, already accepted rows;
    # new canonical evidence writes must provide a closed provenance chain.
    op.add_column("evidence_object", sa.Column("raw_artifact_id", UUID, nullable=True))
    op.add_column("evidence_object", sa.Column("source_record_id", UUID, nullable=True))
    op.create_foreign_key(
        "fk_evidence_object_raw_artifact",
        "evidence_object",
        "raw_artifact",
        ["raw_artifact_id"],
        ["id"],
        ondelete="NO ACTION",
    )
    op.create_foreign_key(
        "fk_evidence_object_source_record",
        "evidence_object",
        "source_record",
        ["source_record_id"],
        ["id"],
        ondelete="NO ACTION",
    )
    op.create_check_constraint(
        "evidence_object_lineage_shape",
        "evidence_object",
        "source_record_id IS NULL OR raw_artifact_id IS NOT NULL",
    )
    op.create_check_constraint(
        "evidence_object_display_name_safe",
        "evidence_object",
        "display_name IS NULL OR display_name !~ '[/\\\\]|[[:cntrl:]]'",
    )
    op.create_unique_constraint(
        "uq_evidence_object_ref_entity_business_unit",
        "evidence_object",
        ["evidence_ref", "entity_id", "business_unit_id"],
    )

    # Candidate evidence links carry the scope values used by deferred
    # composite FKs.  Backfill is deterministic; any missing parent aborts.
    op.add_column("candidate_evidence", sa.Column("candidate_entity_id", UUID, nullable=True))
    op.add_column("candidate_evidence", sa.Column("evidence_entity_id", UUID, nullable=True))
    op.add_column("candidate_evidence", sa.Column("evidence_business_unit_id", UUID, nullable=True))
    op.execute(
        """
        UPDATE public.candidate_evidence AS ce
        SET candidate_entity_id = c.entity_id,
            evidence_entity_id = e.entity_id,
            evidence_business_unit_id = e.business_unit_id
        FROM public.candidate AS c, public.evidence_object AS e
        WHERE c.id = ce.candidate_id
          AND e.evidence_ref = ce.evidence_ref;
        DO $scope$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.candidate_evidence
                WHERE candidate_entity_id IS NULL
                   OR evidence_entity_id IS NULL
                   OR evidence_business_unit_id IS NULL
            ) THEN
                RAISE EXCEPTION 'candidate evidence scope cannot be inferred'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END
        $scope$;
        """
    )
    op.alter_column("candidate_evidence", "candidate_entity_id", nullable=False)
    op.alter_column("candidate_evidence", "evidence_entity_id", nullable=False)
    op.alter_column("candidate_evidence", "evidence_business_unit_id", nullable=False)
    op.create_foreign_key(
        "fk_candidate_evidence_candidate_scope",
        "candidate_evidence",
        "candidate",
        ["candidate_id", "candidate_entity_id"],
        ["id", "entity_id"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_candidate_evidence_evidence_scope",
        "candidate_evidence",
        "evidence_object",
        ["evidence_ref", "evidence_entity_id", "evidence_business_unit_id"],
        ["evidence_ref", "entity_id", "business_unit_id"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
    )
    op.drop_constraint("uq_candidate_evidence_link", "candidate_evidence", type_="unique")
    op.create_unique_constraint(
        "uq_candidate_evidence_link_deferred",
        "candidate_evidence",
        ["candidate_id", "evidence_ref"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_check_constraint(
        "candidate_evidence_kind_allowed",
        "candidate_evidence",
        "kind IN ('MESSAGE_ENVELOPE','MAIL_ENVELOPE','ATTACHMENT')",
    )
    op.create_check_constraint(
        "candidate_evidence_display_name_safe",
        "candidate_evidence",
        "display_name_snapshot IS NULL OR display_name_snapshot !~ '[/\\\\]|[[:cntrl:]]'",
    )

    # Candidate revision/event pairs are closed by deferred composite FKs.
    op.create_unique_constraint(
        "uq_candidate_revision_candidate_revision_status",
        "candidate_revision",
        ["candidate_id", "revision", "status"],
    )
    op.create_unique_constraint(
        "uq_candidate_event_candidate_revision",
        "candidate_event",
        ["candidate_id", "to_revision"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_candidate_event_to_revision_status",
        "candidate_event",
        "candidate_revision",
        ["candidate_id", "to_revision", "to_status"],
        ["candidate_id", "revision", "status"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_candidate_event_from_revision_status",
        "candidate_event",
        "candidate_revision",
        ["candidate_id", "from_revision", "from_status"],
        ["candidate_id", "revision", "status"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
    )
    op.drop_constraint("uq_candidate_event_operation", "candidate_event", type_="unique")
    op.create_unique_constraint(
        "uq_candidate_event_operation_deferred",
        "candidate_event",
        ["operation_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.drop_constraint("uq_candidate_event_audit_event", "candidate_event", type_="unique")
    op.create_unique_constraint(
        "uq_candidate_event_audit_event_deferred",
        "candidate_event",
        ["audit_event_id"],
        deferrable=True,
        initially="DEFERRED",
    )

    # Existing data must have complete, unique POSTED attribution before the
    # stricter deferred write checks are installed.  Never use an inner join
    # that silently drops an unattributed fact.
    op.execute(
        """
        DO $posted$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.journal_entry AS je
                LEFT JOIN public.journal_entry_attribution AS ja
                  ON ja.entry_id = je.id
                WHERE je.status = 'POSTED'
                GROUP BY je.id
                HAVING count(ja.entry_id) <> 1
            ) THEN
                RAISE EXCEPTION
                    'existing POSTED journal entry lacks exactly one scope attribution'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM public.posting AS p
                JOIN public.journal_entry AS je ON je.id = p.entry_id
                LEFT JOIN public.posting_attribution AS pa ON pa.posting_id = p.id
                WHERE je.status = 'POSTED'
                GROUP BY p.id
                HAVING count(pa.posting_id) <> 1
            ) THEN
                RAISE EXCEPTION
                    'existing POSTED posting lacks exactly one category attribution'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM public.journal_entry_attribution AS ja
                JOIN public.journal_entry AS je ON je.id = ja.entry_id
                WHERE ja.entity_id <> je.entity_id
            ) THEN
                RAISE EXCEPTION 'journal attribution entity is contradictory'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM public.posting_attribution AS pa
                JOIN public.posting AS p ON p.id = pa.posting_id
                JOIN public.journal_entry AS je ON je.id = p.entry_id
                JOIN public.reporting_category AS rc
                  ON rc.id = pa.reporting_category_id
                WHERE rc.entity_id <> je.entity_id
            ) THEN
                RAISE EXCEPTION 'posting category attribution entity is contradictory'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END
        $posted$;
        """
    )

    # Migration B left these nullable to remain backward compatible.  R1
    # cannot expose an ambiguous reconciliation leg, so the upgrade stops if a
    # legacy row cannot prove its complete scope and primary designation.
    op.execute(
        """
        DO $reconciliation$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.reconciliation_leg
                WHERE entity_id IS NULL OR business_unit_id IS NULL
                   OR accounting_month IS NULL OR is_primary IS NULL
            ) THEN
                RAISE EXCEPTION
                    'existing reconciliation leg lacks reliable scope or primary flag'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT reconciliation_group_id
                FROM public.reconciliation_leg
                GROUP BY reconciliation_group_id
                HAVING count(*) FILTER (WHERE is_primary IS TRUE) <> 1
            ) THEN
                RAISE EXCEPTION
                    'existing reconciliation group does not have exactly one primary leg'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT reconciliation_group_id
                FROM public.reconciliation_leg
                GROUP BY reconciliation_group_id
                HAVING count(DISTINCT entity_id) <> 1
                    OR count(DISTINCT business_unit_id) <> 1
                    OR count(DISTINCT accounting_month) <> 1
            ) THEN
                RAISE EXCEPTION 'existing reconciliation group has contradictory scope'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END
        $reconciliation$;
        """
    )
    op.alter_column("reconciliation_leg", "entity_id", nullable=False)
    op.alter_column("reconciliation_leg", "business_unit_id", nullable=False)
    op.alter_column("reconciliation_leg", "accounting_month", nullable=False)
    op.alter_column("reconciliation_leg", "is_primary", nullable=False)
    op.create_foreign_key(
        "fk_reconciliation_leg_scope",
        "reconciliation_leg",
        "business_unit",
        ["entity_id", "business_unit_id"],
        ["entity_id", "id"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_index(
        "ix_reconciliation_leg_scope_month",
        "reconciliation_leg",
        ["entity_id", "business_unit_id", "accounting_month", "reconciliation_group_id"],
    )

    op.create_table(
        "reconciliation_snapshot_blocker",
        sa.Column("snapshot_ref", UUID, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("message", sa.String(300), nullable=False),
        sa.Column("field", sa.String(64), nullable=True),
        sa.Column("conflict_ref", UUID, nullable=True),
        sa.Column("evidence_ref", UUID, nullable=True),
        sa.CheckConstraint("ordinal >= 0", name="snapshot_blocker_ordinal_nonnegative"),
        sa.CheckConstraint(
            "btrim(code) <> '' AND char_length(code) <= 64 "
            "AND btrim(message) <> '' AND char_length(message) <= 300",
            name="snapshot_blocker_text_bounded",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_ref"],
            ["reconciliation_snapshot.snapshot_ref"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_ref"],
            ["evidence_object.evidence_ref"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("snapshot_ref", "ordinal", name="pk_snapshot_blocker"),
    )
    op.create_index(
        "ix_snapshot_blocker_snapshot",
        "reconciliation_snapshot_blocker",
        ["snapshot_ref", "ordinal"],
    )

    op.drop_constraint("uq_snapshot_audit_event", "reconciliation_snapshot", type_="unique")
    op.create_unique_constraint(
        "uq_snapshot_audit_event_deferred",
        "reconciliation_snapshot",
        ["audit_event_id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_index("ix_candidate_keyset", "candidate", ["entity_id", "created_at", "id"])
    op.create_index(
        "ix_candidate_event_asof",
        "candidate_event",
        ["candidate_id", "to_revision", "to_status", "audit_event_id"],
    )
    op.create_index(
        "ix_candidate_revision_month_status",
        "candidate_revision",
        ["accounting_month", "status", "candidate_id", sa.text("revision DESC")],
    )
    op.create_index(
        "ix_candidate_evidence_lookup", "candidate_evidence", ["evidence_ref", "candidate_id"]
    )
    op.create_index(
        "ix_evidence_scope_lookup",
        "evidence_object",
        ["entity_id", "business_unit_id", "evidence_ref"],
    )
    op.create_index(
        "ix_encrypted_object_identity_evidence",
        "encrypted_object_identity",
        ["evidence_ref", "object_ref"],
    )
    op.create_index(
        "ix_reconciliation_snapshot_scope",
        "reconciliation_snapshot",
        ["entity_id", "business_unit_id", "accounting_month", sa.text("snapshot_revision DESC")],
    )
    op.create_index(
        "ix_journal_posted_scope",
        "journal_entry",
        ["entity_id", "id"],
        postgresql_where=sa.text("status = 'POSTED'"),
    )
    op.create_index(
        "ix_posting_category", "posting_attribution", ["reporting_category_id", "posting_id"]
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.r1_validate_candidate_scope()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_candidate_entity uuid;
            v_evidence_entity uuid;
            v_evidence_unit uuid;
            v_revision_unit uuid;
            v_create_xid xid;
        BEGIN
            SELECT entity_id INTO v_candidate_entity
              FROM public.candidate WHERE id = NEW.candidate_id;
            SELECT entity_id, business_unit_id
              INTO v_evidence_entity, v_evidence_unit
              FROM public.evidence_object WHERE evidence_ref = NEW.evidence_ref;
            IF v_candidate_entity IS NULL OR v_evidence_entity IS NULL
               OR v_candidate_entity IS DISTINCT FROM v_evidence_entity
               OR NEW.candidate_entity_id IS DISTINCT FROM v_candidate_entity
               OR NEW.evidence_entity_id IS DISTINCT FROM v_evidence_entity
               OR NEW.evidence_business_unit_id IS DISTINCT FROM v_evidence_unit THEN
                RAISE EXCEPTION 'candidate and evidence must belong to the same entity'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT business_unit_id INTO v_revision_unit
              FROM public.candidate_revision
             WHERE candidate_id = NEW.candidate_id
             ORDER BY revision DESC LIMIT 1;
            IF v_revision_unit IS NOT NULL AND v_revision_unit IS DISTINCT FROM v_evidence_unit THEN
                RAISE EXCEPTION 'assigned candidate evidence must share its business unit'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT a.xmin INTO v_create_xid
              FROM public.candidate_event AS e
              JOIN public.audit_event AS a ON a.id = e.audit_event_id
             WHERE e.candidate_id = NEW.candidate_id
               AND e.event_type = 'CREATE'
               AND e.to_revision = 1;
            IF v_create_xid IS NULL
               OR pg_xact_status(v_create_xid::text::xid8) IS DISTINCT FROM 'in progress' THEN
                RAISE EXCEPTION 'candidate evidence links are CREATE-transaction-only'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE FUNCTION public.r1_validate_evidence_provenance()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE v_artifact uuid; v_source_artifact uuid;
        BEGIN
            IF NEW.source_record_id IS NULL THEN RETURN NEW; END IF;
            IF NEW.raw_artifact_id IS NULL THEN
                RAISE EXCEPTION 'evidence source record requires raw artifact'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT artifact_id INTO v_source_artifact
              FROM public.source_record WHERE id = NEW.source_record_id;
            v_artifact := NEW.raw_artifact_id;
            IF v_source_artifact IS DISTINCT FROM v_artifact THEN
                RAISE EXCEPTION 'evidence source record and raw artifact disagree'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_evidence_provenance
        AFTER INSERT ON public.evidence_object
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_evidence_provenance();

        CREATE FUNCTION public.r1_validate_candidate_source_provenance()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE v_channel text; v_source text;
        BEGIN
            IF NEW.source_record_id IS NULL THEN RETURN NEW; END IF;
            SELECT ra.source, sr.source
              INTO v_channel, v_source
              FROM public.source_record AS sr
              JOIN public.raw_artifact AS ra ON ra.id = sr.artifact_id
             WHERE sr.id = NEW.source_record_id;
            IF v_channel IS DISTINCT FROM NEW.ingest_channel_id
               OR v_source IS DISTINCT FROM NEW.source_system_id THEN
                RAISE EXCEPTION 'candidate source registry provenance disagrees'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_candidate_source_provenance
        AFTER INSERT ON public.candidate_source
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_source_provenance();

        CREATE FUNCTION public.r1_validate_blob_lineage()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_parent_evidence uuid;
            v_action text;
            v_payload jsonb;
            v_mode text;
            v_audit_xid xid;
            v_tip_count bigint;
            v_genesis_count bigint;
            v_identity_xid xid;
        BEGIN
            SELECT e.xmin, e.action, e.payload
              INTO v_audit_xid, v_action, v_payload
              FROM public.audit_event AS e WHERE e.id = NEW.audit_event_id;
            IF v_action IS NULL THEN
                RAISE EXCEPTION 'blob audit evidence does not exist'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            v_mode := v_payload ->> 'rotation_mode';
            IF v_audit_xid IS NULL
               OR pg_xact_status(v_audit_xid::text::xid8) IS DISTINCT FROM 'in progress'
               OR v_action IS DISTINCT FROM 'evidence.blob.version'
               OR v_mode NOT IN ('GENESIS', 'REWRAP', 'REENCRYPT')
               OR v_payload IS DISTINCT FROM jsonb_build_object(
                   'rotation_mode', v_mode,
                   'blob_ref', NEW.blob_ref::text,
                   'evidence_ref', NEW.evidence_ref::text,
                   'predecessor_blob_ref', NEW.predecessor_blob_ref::text,
                   'object_ref', NEW.object_ref,
                   'ciphertext_sha256', encode(NEW.ciphertext_sha256, 'hex'),
                   'ciphertext_size', NEW.ciphertext_size,
                   'storage_key', NEW.storage_key,
                   'envelope_schema', NEW.envelope_schema,
                   'algorithm', NEW.algorithm,
                   'chunk_size', NEW.chunk_size,
                   'stream_header', encode(NEW.stream_header, 'hex'),
                   'wrapped_key_generation', NEW.wrapped_key_generation,
                   'wrapped_key_nonce', encode(NEW.wrapped_key_nonce, 'hex'),
                   'wrapped_key_ciphertext', encode(NEW.wrapped_key_ciphertext, 'hex'),
                   'purpose', NEW.purpose
               ) THEN
                RAISE EXCEPTION 'blob audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF NEW.predecessor_blob_ref IS NOT NULL THEN
                IF NEW.predecessor_blob_ref = NEW.blob_ref THEN
                    RAISE EXCEPTION 'encrypted blob cannot reference itself'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                SELECT evidence_ref INTO v_parent_evidence
                  FROM public.encrypted_blob_version
                 WHERE blob_ref = NEW.predecessor_blob_ref;
                IF v_parent_evidence IS DISTINCT FROM NEW.evidence_ref THEN
                    RAISE EXCEPTION 'blob predecessor belongs to another evidence object'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF (
                    SELECT count(*) FROM public.encrypted_blob_version
                    WHERE predecessor_blob_ref = NEW.predecessor_blob_ref
                ) > 1 THEN
                    RAISE EXCEPTION 'encrypted blob predecessor would branch'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            WITH RECURSIVE ancestors(blob_ref, path) AS (
                SELECT b.predecessor_blob_ref, ARRAY[b.blob_ref, b.predecessor_blob_ref]
                  FROM public.encrypted_blob_version AS b
                 WHERE b.blob_ref = NEW.blob_ref
                   AND b.predecessor_blob_ref IS NOT NULL
                UNION ALL
                SELECT b.predecessor_blob_ref, a.path || b.predecessor_blob_ref
                  FROM ancestors AS a
                  JOIN public.encrypted_blob_version AS b ON b.blob_ref = a.blob_ref
                 WHERE a.blob_ref IS NOT NULL
                   AND a.blob_ref <> NEW.blob_ref
                   AND (NOT (b.predecessor_blob_ref = ANY(a.path))
                        OR b.predecessor_blob_ref = NEW.blob_ref)
            )
            SELECT 1 INTO v_parent_evidence FROM ancestors
             WHERE blob_ref = NEW.blob_ref;
            IF FOUND THEN
                RAISE EXCEPTION 'encrypted blob predecessor chain contains a cycle'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.predecessor_blob_ref IS NULL THEN
                IF v_mode <> 'GENESIS' THEN
                    RAISE EXCEPTION 'blob genesis must use GENESIS mode'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                SELECT xmin INTO v_identity_xid
                  FROM public.encrypted_object_identity
                 WHERE object_ref = NEW.object_ref
                   AND evidence_ref = NEW.evidence_ref;
                IF v_identity_xid IS NULL
                   OR pg_xact_status(v_identity_xid::text::xid8)
                        IS DISTINCT FROM 'in progress' THEN
                    RAISE EXCEPTION 'GENESIS identity must be created in this transaction'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            ELSIF NEW.predecessor_blob_ref IS NOT NULL THEN
                IF v_mode = 'REWRAP' THEN
                    IF NEW.object_ref IS DISTINCT FROM (
                        SELECT object_ref FROM public.encrypted_blob_version
                        WHERE blob_ref = NEW.predecessor_blob_ref
                    ) THEN
                        RAISE EXCEPTION 'REWRAP must preserve object_ref'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM public.encrypted_object_identity AS oi
                         WHERE oi.object_ref = NEW.object_ref
                           AND oi.evidence_ref = NEW.evidence_ref
                    ) THEN
                        RAISE EXCEPTION 'REWRAP identity must belong to predecessor evidence'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                ELSIF v_mode = 'REENCRYPT' THEN
                    IF NEW.object_ref = (
                        SELECT object_ref FROM public.encrypted_blob_version
                        WHERE blob_ref = NEW.predecessor_blob_ref
                    ) THEN
                        RAISE EXCEPTION 'REENCRYPT must use a new object_ref'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    SELECT xmin INTO v_identity_xid
                      FROM public.encrypted_object_identity
                     WHERE object_ref = NEW.object_ref
                       AND evidence_ref = NEW.evidence_ref;
                    IF v_identity_xid IS NULL
                       OR pg_xact_status(v_identity_xid::text::xid8)
                            IS DISTINCT FROM 'in progress' THEN
                        RAISE EXCEPTION 'REENCRYPT identity must be created in this transaction'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                END IF;
            END IF;
            SELECT count(*) INTO v_genesis_count
              FROM public.encrypted_blob_version
             WHERE evidence_ref = NEW.evidence_ref AND predecessor_blob_ref IS NULL;
            IF v_genesis_count <> 1 THEN
                RAISE EXCEPTION 'encrypted evidence must have exactly one genesis'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT count(*) INTO v_tip_count
              FROM public.encrypted_blob_version AS b
             WHERE b.evidence_ref = NEW.evidence_ref
               AND NOT EXISTS (
                   SELECT 1 FROM public.encrypted_blob_version AS child
                   WHERE child.predecessor_blob_ref = b.blob_ref
               );
            IF v_tip_count <> 1 THEN
                RAISE EXCEPTION 'encrypted evidence must have exactly one active tip'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_encrypted_blob_lineage
        AFTER INSERT ON public.encrypted_blob_version
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_blob_lineage();

        CREATE FUNCTION public.r1_validate_candidate_event_audit()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_action text; v_payload jsonb; v_expected jsonb; v_audit_xid xid;
            v_expected_action text;
        BEGIN
            SELECT e.xmin, e.action, e.payload
              INTO v_audit_xid, v_action, v_payload
              FROM public.audit_event AS e WHERE e.id = NEW.audit_event_id;
            v_expected_action := CASE WHEN NEW.event_type = 'CREATE'
                                      THEN 'candidate.create' ELSE 'candidate.transition' END;
            v_expected := jsonb_build_object(
                'event_ref', NEW.event_ref::text,
                'candidate_id', NEW.candidate_id::text,
                'candidate_ref', NEW.candidate_id::text,
                'operation_id', NEW.operation_id::text,
                'command_fingerprint', encode(NEW.command_fingerprint, 'hex'),
                'event_type', NEW.event_type,
                'action', NEW.action,
                'from_revision', NEW.from_revision,
                'to_revision', NEW.to_revision,
                'from_status', NEW.from_status,
                'to_status', NEW.to_status,
                'field_changes', coalesce((
                    SELECT jsonb_agg(jsonb_build_object(
                        'field', fc.field,
                        'previous_value', fc.previous_value,
                        'new_value', fc.new_value
                    ) ORDER BY fc.field)
                      FROM public.candidate_field_change AS fc
                     WHERE fc.event_ref = NEW.event_ref
                ), '[]'::jsonb),
                'conflict_resolutions', coalesce((
                    SELECT jsonb_agg(jsonb_build_object(
                        'conflict_ref', cr.conflict_ref,
                        'resolution', cr.resolution
                    ) ORDER BY cr.conflict_ref)
                      FROM public.candidate_conflict_resolution AS cr
                     WHERE cr.event_ref = NEW.event_ref
                ), '[]'::jsonb),
                'actor_ref', NEW.actor_ref,
                'reason', NEW.reason,
                'derived_candidate_id', NEW.derived_candidate_id::text
            );
            IF v_audit_xid IS NULL
               OR pg_xact_status(v_audit_xid::text::xid8) IS DISTINCT FROM 'in progress'
               OR v_action IS DISTINCT FROM v_expected_action
               OR v_payload IS DISTINCT FROM v_expected THEN
                RAISE EXCEPTION 'candidate event audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_candidate_event_audit
        AFTER INSERT ON public.candidate_event
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_event_audit();

        CREATE FUNCTION public.r1_validate_candidate_event_history()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE v_action text; v_count bigint; v_status text;
        BEGIN
            SELECT a.action INTO v_action
              FROM public.audit_event AS a
              JOIN public.candidate_event AS e ON e.audit_event_id = a.id
             WHERE e.candidate_id = NEW.candidate_id
               AND e.to_revision = NEW.to_revision;
            IF NEW.event_type = 'CREATE' THEN
                IF NEW.to_revision <> 1 OR NEW.from_revision IS NOT NULL
                   OR NEW.from_status IS NOT NULL OR NEW.action IS NOT NULL
                   OR NEW.derived_candidate_id IS NOT NULL
                   OR NEW.to_status NOT IN ('INCOMPLETE','CONFLICTED','PENDING') THEN
                    RAISE EXCEPTION 'CREATE event has an invalid shape'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                SELECT count(*) INTO v_count
                  FROM public.candidate_evidence AS ce
                 WHERE ce.candidate_id = NEW.candidate_id;
                IF v_count < 1 THEN
                    RAISE EXCEPTION 'candidate creation requires at least one evidence link'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM public.candidate_source
                    WHERE candidate_id = NEW.candidate_id
                ) THEN
                    RAISE EXCEPTION 'candidate creation requires a source row'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            ELSE
                IF NEW.from_revision IS NULL OR NEW.from_status IS NULL
                   OR NEW.action IS DISTINCT FROM NEW.event_type
                   OR NEW.to_revision <> NEW.from_revision + 1 THEN
                    RAISE EXCEPTION 'candidate transition event has an invalid edge'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                SELECT count(*) INTO v_count
                  FROM public.candidate_event
                 WHERE candidate_id = NEW.candidate_id
                   AND to_revision = NEW.from_revision;
                IF v_count <> 1 THEN
                    RAISE EXCEPTION 'candidate transition has no unique predecessor event'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                SELECT to_status INTO v_status
                  FROM public.candidate_event
                 WHERE candidate_id = NEW.candidate_id
                   AND to_revision = NEW.from_revision;
                IF v_status IS DISTINCT FROM NEW.from_status THEN
                    RAISE EXCEPTION 'candidate transition predecessor status disagrees'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NOT (
                    (NEW.from_status = 'INCOMPLETE' AND NEW.to_status = 'PENDING'
                     AND NEW.action = 'COMPLETE_FIELDS')
                 OR (NEW.from_status = 'CONFLICTED' AND NEW.to_status = 'PENDING'
                     AND NEW.action = 'RESOLVE_CONFLICT')
                 OR (NEW.from_status = 'PENDING' AND NEW.to_status = 'CONFIRMED'
                     AND NEW.action = 'CONFIRM')
                 OR (NEW.from_status IN ('INCOMPLETE','CONFLICTED','PENDING')
                     AND NEW.to_status = 'IGNORED' AND NEW.action = 'IGNORE')
                 OR (NEW.from_status = 'CONFIRMED' AND NEW.to_status = 'SUPERSEDED'
                     AND NEW.action = 'SUPERSEDE')
                ) THEN
                    RAISE EXCEPTION 'candidate transition state edge is invalid'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.event_type = 'COMPLETE_FIELDS' AND NOT EXISTS (
                    SELECT 1 FROM public.candidate_field_change
                     WHERE event_ref = NEW.event_ref
                ) THEN
                    RAISE EXCEPTION 'candidate completion requires a field change'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.event_type IN ('CONFIRM','IGNORE') AND EXISTS (
                    SELECT 1 FROM public.candidate_field_change
                     WHERE event_ref = NEW.event_ref
                ) THEN
                    RAISE EXCEPTION 'candidate confirm or ignore cannot change fields'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.event_type = 'RESOLVE_CONFLICT' AND NOT EXISTS (
                    SELECT 1 FROM public.candidate_conflict_resolution
                     WHERE event_ref = NEW.event_ref
                ) THEN
                    RAISE EXCEPTION 'candidate conflict resolution requires resolutions'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.event_type = 'SUPERSEDE' AND (
                    NEW.derived_candidate_id IS NULL
                    OR (SELECT count(*) FROM public.candidate AS successor
                         WHERE successor.id = NEW.derived_candidate_id
                           AND successor.entity_id = (
                               SELECT entity_id FROM public.candidate
                                WHERE id = NEW.candidate_id
                           )
                           AND successor.supersedes_candidate_id = NEW.candidate_id) <> 1
                ) THEN
                    RAISE EXCEPTION 'candidate supersede requires one same-entity successor'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_candidate_history
        AFTER INSERT ON public.candidate_event
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_event_history();

        CREATE FUNCTION public.r1_validate_candidate_revision_history()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE v_action text; v_count bigint;
        BEGIN
            SELECT a.action INTO v_action
              FROM public.audit_event AS a
              JOIN public.candidate_event AS e ON e.audit_event_id = a.id
             WHERE e.candidate_id = NEW.candidate_id
               AND e.to_revision = NEW.revision;
            SELECT count(*) INTO v_count
              FROM public.candidate_event
             WHERE candidate_id = NEW.candidate_id
               AND to_revision = NEW.revision
               AND to_status = NEW.status;
            IF v_count <> 1 THEN
                RAISE EXCEPTION 'candidate revision must have exactly one typed event'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_candidate_revision_history
        AFTER INSERT ON public.candidate_revision
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_revision_history();

        CREATE FUNCTION public.r1_validate_snapshot_blocker_scope()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_entity_id uuid;
            v_business_unit_id uuid;
            v_evidence_entity_id uuid;
            v_evidence_business_unit_id uuid;
        BEGIN
            IF NEW.evidence_ref IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT entity_id, business_unit_id
              INTO v_entity_id, v_business_unit_id
              FROM public.reconciliation_snapshot
             WHERE snapshot_ref = NEW.snapshot_ref;
            SELECT entity_id, business_unit_id
              INTO v_evidence_entity_id, v_evidence_business_unit_id
              FROM public.evidence_object
             WHERE evidence_ref = NEW.evidence_ref;
            IF v_entity_id IS NULL OR v_evidence_entity_id IS NULL
               OR v_entity_id IS DISTINCT FROM v_evidence_entity_id
               OR v_business_unit_id IS DISTINCT FROM v_evidence_business_unit_id THEN
                RAISE EXCEPTION 'snapshot blocker evidence is outside snapshot scope'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_snapshot_blocker_scope
        AFTER INSERT ON public.reconciliation_snapshot_blocker
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_snapshot_blocker_scope();

        CREATE FUNCTION public.r1_validate_reconciliation_leg()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_group uuid;
            v_count bigint;
            v_entity_scope_count bigint;
            v_business_unit_scope_count bigint;
            v_month_scope_count bigint;
        BEGIN
            v_group := CASE WHEN TG_OP = 'DELETE' THEN OLD.reconciliation_group_id
                            ELSE NEW.reconciliation_group_id END;
            PERFORM pg_advisory_xact_lock(
                hashtext('ledgerbridge.reconciliation.primary'), hashtext(v_group::text)
            );
            PERFORM 1 FROM public.reconciliation_group
             WHERE id = v_group FOR KEY SHARE;
            SELECT count(*) FILTER (WHERE is_primary IS TRUE),
                   count(DISTINCT entity_id),
                   count(DISTINCT business_unit_id),
                   count(DISTINCT accounting_month)
              INTO v_count, v_entity_scope_count,
                   v_business_unit_scope_count, v_month_scope_count
              FROM public.reconciliation_leg WHERE reconciliation_group_id = v_group;
            IF v_count <> 1
               OR v_entity_scope_count <> 1
               OR v_business_unit_scope_count <> 1
               OR v_month_scope_count <> 1
               OR EXISTS (
                   SELECT 1 FROM public.reconciliation_leg
                    WHERE reconciliation_group_id = v_group
                     AND (business_unit_id IS NULL OR accounting_month IS NULL)
               ) THEN
                RAISE EXCEPTION 'reconciliation group requires exactly one scoped primary leg'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.reconciliation_leg AS l
                  JOIN public.posting AS p ON p.id = l.posting_id
                  JOIN public.journal_entry AS je ON je.id = p.entry_id
                  LEFT JOIN public.journal_entry_attribution AS ja
                    ON ja.entry_id = je.id
                 WHERE l.reconciliation_group_id = v_group
                   AND (l.is_primary IS TRUE OR l.posting_id IS NOT NULL)
                   AND (
                       p.account_id IS DISTINCT FROM je.primary_account_id
                       OR je.entity_id IS DISTINCT FROM l.entity_id
                       OR ja.business_unit_id IS DISTINCT FROM l.business_unit_id
                       OR ja.accounting_month IS DISTINCT FROM l.accounting_month
                   )
            ) THEN
                RAISE EXCEPTION 'reconciliation leg posting or scope is inconsistent'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_reconciliation_leg_exactly_one_primary
        AFTER INSERT OR UPDATE OR DELETE ON public.reconciliation_leg
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_reconciliation_leg();

        CREATE FUNCTION public.r1_validate_posted_entry_completeness()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE v_entry uuid; v_count bigint;
        BEGIN
            v_entry := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
            IF NOT EXISTS (
                SELECT 1 FROM public.journal_entry WHERE id = v_entry AND status = 'POSTED'
            ) THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            SELECT count(*) INTO v_count
              FROM public.journal_entry_attribution WHERE entry_id = v_entry;
            IF v_count <> 1 THEN
                RAISE EXCEPTION 'POSTED journal entry requires exactly one attribution'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.posting AS p
                LEFT JOIN public.posting_attribution AS pa ON pa.posting_id = p.id
                WHERE p.entry_id = v_entry
                GROUP BY p.id HAVING count(pa.posting_id) <> 1
            ) THEN
                RAISE EXCEPTION 'POSTED journal entry requires complete posting attribution'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_posted_entry_complete
        AFTER INSERT OR UPDATE OF status ON public.journal_entry
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_posted_entry_completeness();

        CREATE FUNCTION public.r1_validate_posted_entry_attribution()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE v_entry uuid;
        BEGIN
            v_entry := CASE WHEN TG_OP = 'DELETE' THEN OLD.entry_id ELSE NEW.entry_id END;
            IF EXISTS (
                SELECT 1 FROM public.journal_entry WHERE id = v_entry AND status = 'POSTED'
            ) THEN
                IF (SELECT count(*) FROM public.journal_entry_attribution WHERE entry_id = v_entry) <> 1
                   OR EXISTS (
                       SELECT 1 FROM public.posting AS p
                       LEFT JOIN public.posting_attribution AS pa ON pa.posting_id = p.id
                       WHERE p.entry_id = v_entry
                       GROUP BY p.id HAVING count(pa.posting_id) <> 1
                   ) THEN
                    RAISE EXCEPTION 'POSTED journal entry attribution is incomplete'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_posted_entry_attribution_complete
        AFTER INSERT OR UPDATE OR DELETE ON public.journal_entry_attribution
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_posted_entry_attribution();

        CREATE FUNCTION public.r1_validate_posted_posting_attribution()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE v_entry uuid; v_posting uuid;
        BEGIN
            v_posting := CASE WHEN TG_OP = 'DELETE' THEN OLD.posting_id ELSE NEW.posting_id END;
            SELECT entry_id INTO v_entry FROM public.posting WHERE id = v_posting;
            IF EXISTS (
                SELECT 1 FROM public.journal_entry WHERE id = v_entry AND status = 'POSTED'
            ) AND (
                SELECT count(*) FROM public.posting_attribution WHERE posting_id = v_posting
            ) <> 1 THEN
                RAISE EXCEPTION 'POSTED posting requires exactly one category attribution'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.posting_attribution AS pa
                  JOIN public.reporting_category AS rc
                    ON rc.id = pa.reporting_category_id
                  JOIN public.posting AS p ON p.id = pa.posting_id
                  JOIN public.journal_entry AS je ON je.id = p.entry_id
                 WHERE pa.posting_id = v_posting
                   AND (
                       rc.entity_id IS DISTINCT FROM je.entity_id
                       OR pa.category_code_snapshot IS DISTINCT FROM rc.code
                       OR pa.category_label_snapshot IS DISTINCT FROM rc.label
                   )
            ) THEN
                RAISE EXCEPTION 'posting attribution category entity or snapshot is inconsistent'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_posted_posting_attribution_complete
        AFTER INSERT OR UPDATE OR DELETE ON public.posting_attribution
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_posted_posting_attribution();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.r1_validate_snapshot_audit()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE v_xid xid; v_action text; v_payload jsonb; v_expected jsonb;
        BEGIN
            SELECT a.xmin, a.action, a.payload INTO v_xid, v_action, v_payload
              FROM public.audit_event AS a WHERE a.id = NEW.audit_event_id;
            v_expected := jsonb_build_object(
                'snapshot_ref', NEW.snapshot_ref::text,
                'entity_id', NEW.entity_id::text,
                'business_unit_id', NEW.business_unit_id::text,
                'accounting_month', to_char(NEW.accounting_month, 'YYYY-MM-DD'),
                'snapshot_revision', NEW.snapshot_revision,
                'ledger_audit_sequence', NEW.ledger_audit_sequence,
                'ledger_audit_hash', encode(NEW.ledger_audit_hash, 'hex'),
                'posted_amount_minor', NEW.posted_amount_minor,
                'currency', NEW.currency,
                'blocker_count', (
                    SELECT count(*) FROM public.reconciliation_snapshot_blocker
                     WHERE snapshot_ref = NEW.snapshot_ref
                ),
                'proposal_count', (
                    SELECT count(*) FROM public.reconciliation_snapshot_proposal
                     WHERE snapshot_ref = NEW.snapshot_ref
                ),
                'suspense_count', (
                    SELECT count(*) FROM public.reconciliation_snapshot_suspense
                     WHERE snapshot_ref = NEW.snapshot_ref
                )
            );
            IF v_xid IS NULL
               OR pg_xact_status(v_xid::text::xid8) IS DISTINCT FROM 'in progress'
               OR v_action IS DISTINCT FROM 'reconciliation.snapshot'
               OR v_payload IS DISTINCT FROM v_expected THEN
                RAISE EXCEPTION 'snapshot audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_snapshot_audit_binding
        AFTER INSERT ON public.reconciliation_snapshot
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_snapshot_audit();
        """
    )

    for table in (
        "encrypted_object_identity",
        "reconciliation_snapshot_blocker",
        "reconciliation_leg",
    ):
        _append_only(table)
    _revoke_fact_writes()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            """
            SELECT EXISTS (SELECT 1 FROM public.encrypted_object_identity)
                OR EXISTS (SELECT 1 FROM public.encrypted_blob_version)
                OR EXISTS (SELECT 1 FROM public.evidence_object)
                OR EXISTS (SELECT 1 FROM public.candidate)
                OR EXISTS (SELECT 1 FROM public.candidate_event)
                OR EXISTS (SELECT 1 FROM public.reconciliation_snapshot)
                OR EXISTS (SELECT 1 FROM public.reconciliation_snapshot_blocker)
                OR EXISTS (SELECT 1 FROM public.journal_entry_attribution)
                OR EXISTS (SELECT 1 FROM public.posting_attribution)
                OR EXISTS (SELECT 1 FROM public.reconciliation_leg)
            """
        )
    ).scalar_one():
        raise RuntimeError("R1 fact hardening data prevents destructive downgrade")

    op.execute(
        """
        REVOKE ALL ON TABLE
            public.encrypted_object_identity, public.reconciliation_snapshot_blocker
        FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;
        DROP TRIGGER IF EXISTS r1_snapshot_audit_binding ON public.reconciliation_snapshot;
        DROP FUNCTION IF EXISTS public.r1_validate_snapshot_audit();
        DROP TRIGGER IF EXISTS r1_posted_posting_attribution_complete ON public.posting_attribution;
        DROP TRIGGER IF EXISTS r1_posted_entry_attribution_complete ON public.journal_entry_attribution;
        DROP TRIGGER IF EXISTS r1_posted_entry_complete ON public.journal_entry;
        DROP FUNCTION IF EXISTS public.r1_validate_posted_posting_attribution();
        DROP FUNCTION IF EXISTS public.r1_validate_posted_entry_attribution();
        DROP FUNCTION IF EXISTS public.r1_validate_posted_entry_completeness();
        DROP TRIGGER IF EXISTS r1_reconciliation_leg_exactly_one_primary
            ON public.reconciliation_leg;
        DROP FUNCTION IF EXISTS public.r1_validate_reconciliation_leg();
        DROP TRIGGER IF EXISTS r1_reconciliation_leg_append_only_trigger
            ON public.reconciliation_leg;
        DROP FUNCTION IF EXISTS public.r1_reconciliation_leg_append_only();
        DROP TRIGGER IF EXISTS r1_candidate_revision_history ON public.candidate_revision;
        DROP TRIGGER IF EXISTS r1_candidate_history ON public.candidate_event;
        DROP FUNCTION IF EXISTS public.r1_validate_candidate_revision_history();
        DROP TRIGGER IF EXISTS r1_snapshot_blocker_scope
            ON public.reconciliation_snapshot_blocker;
        DROP FUNCTION IF EXISTS public.r1_validate_snapshot_blocker_scope();
        DROP FUNCTION IF EXISTS public.r1_validate_candidate_event_history();
        DROP TRIGGER IF EXISTS r1_candidate_event_audit ON public.candidate_event;
        DROP FUNCTION IF EXISTS public.r1_validate_candidate_event_audit();
        DROP TRIGGER IF EXISTS r1_encrypted_blob_lineage ON public.encrypted_blob_version;
        DROP FUNCTION IF EXISTS public.r1_validate_blob_lineage();
        DROP TRIGGER IF EXISTS r1_candidate_source_provenance ON public.candidate_source;
        DROP FUNCTION IF EXISTS public.r1_validate_candidate_source_provenance();
        DROP TRIGGER IF EXISTS r1_evidence_provenance ON public.evidence_object;
        DROP FUNCTION IF EXISTS public.r1_validate_evidence_provenance();
        DROP TRIGGER IF EXISTS r1_encrypted_object_identity_append_only_trigger
            ON public.encrypted_object_identity;
        DROP FUNCTION IF EXISTS public.r1_encrypted_object_identity_append_only();
        DROP TRIGGER IF EXISTS r1_reconciliation_snapshot_blocker_append_only_trigger
            ON public.reconciliation_snapshot_blocker;
        DROP FUNCTION IF EXISTS public.r1_reconciliation_snapshot_blocker_append_only();
        """
    )
    for index_name, table_name in (
        ("ix_posting_category", "posting_attribution"),
        ("ix_journal_posted_scope", "journal_entry"),
        ("ix_reconciliation_snapshot_scope", "reconciliation_snapshot"),
        ("ix_reconciliation_leg_scope_month", "reconciliation_leg"),
        ("ix_encrypted_object_identity_evidence", "encrypted_object_identity"),
        ("ix_evidence_scope_lookup", "evidence_object"),
        ("ix_candidate_evidence_lookup", "candidate_evidence"),
        ("ix_candidate_revision_month_status", "candidate_revision"),
        ("ix_candidate_event_asof", "candidate_event"),
        ("ix_candidate_keyset", "candidate"),
        ("ix_snapshot_blocker_snapshot", "reconciliation_snapshot_blocker"),
    ):
        op.drop_index(index_name, table_name=table_name)
    op.drop_table("reconciliation_snapshot_blocker")
    op.drop_constraint("fk_reconciliation_leg_scope", "reconciliation_leg", type_="foreignkey")
    op.alter_column("reconciliation_leg", "is_primary", nullable=True)
    op.alter_column("reconciliation_leg", "accounting_month", nullable=True)
    op.alter_column("reconciliation_leg", "business_unit_id", nullable=True)
    op.alter_column("reconciliation_leg", "entity_id", nullable=True)
    op.drop_constraint(
        "uq_snapshot_audit_event_deferred", "reconciliation_snapshot", type_="unique"
    )
    op.create_unique_constraint(
        "uq_snapshot_audit_event", "reconciliation_snapshot", ["audit_event_id"]
    )
    op.drop_constraint(
        "fk_candidate_event_from_revision_status", "candidate_event", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_candidate_event_to_revision_status", "candidate_event", type_="foreignkey"
    )
    op.drop_constraint("uq_candidate_event_audit_event_deferred", "candidate_event", type_="unique")
    op.create_unique_constraint(
        "uq_candidate_event_audit_event", "candidate_event", ["audit_event_id"]
    )
    op.drop_constraint("uq_candidate_event_operation_deferred", "candidate_event", type_="unique")
    op.create_unique_constraint("uq_candidate_event_operation", "candidate_event", ["operation_id"])
    op.drop_constraint("uq_candidate_event_candidate_revision", "candidate_event", type_="unique")
    op.drop_constraint(
        "uq_candidate_revision_candidate_revision_status", "candidate_revision", type_="unique"
    )
    op.drop_constraint("candidate_evidence_display_name_safe", "candidate_evidence", type_="check")
    op.drop_constraint("candidate_evidence_kind_allowed", "candidate_evidence", type_="check")
    op.drop_constraint("uq_candidate_evidence_link_deferred", "candidate_evidence", type_="unique")
    op.create_unique_constraint(
        "uq_candidate_evidence_link", "candidate_evidence", ["candidate_id", "evidence_ref"]
    )
    op.drop_constraint(
        "fk_candidate_evidence_evidence_scope", "candidate_evidence", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_candidate_evidence_candidate_scope", "candidate_evidence", type_="foreignkey"
    )
    op.drop_column("candidate_evidence", "evidence_business_unit_id")
    op.drop_column("candidate_evidence", "evidence_entity_id")
    op.drop_column("candidate_evidence", "candidate_entity_id")
    op.drop_constraint(
        "uq_evidence_object_ref_entity_business_unit", "evidence_object", type_="unique"
    )
    op.drop_constraint("evidence_object_display_name_safe", "evidence_object", type_="check")
    op.drop_constraint("evidence_object_lineage_shape", "evidence_object", type_="check")
    op.drop_constraint("fk_evidence_object_source_record", "evidence_object", type_="foreignkey")
    op.drop_constraint("fk_evidence_object_raw_artifact", "evidence_object", type_="foreignkey")
    op.drop_column("evidence_object", "source_record_id")
    op.drop_column("evidence_object", "raw_artifact_id")
    op.drop_constraint(
        "encrypted_blob_ciphertext_size_positive", "encrypted_blob_version", type_="check"
    )
    op.drop_constraint(
        "uq_encrypted_blob_evidence_predecessor", "encrypted_blob_version", type_="unique"
    )
    op.drop_constraint(
        "fk_encrypted_blob_object_identity", "encrypted_blob_version", type_="foreignkey"
    )
    op.create_unique_constraint(
        "uq_encrypted_blob_object_ref", "encrypted_blob_version", ["object_ref"]
    )
    op.drop_table("encrypted_object_identity")
