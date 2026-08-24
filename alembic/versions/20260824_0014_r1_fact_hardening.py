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
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
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
                        'public.reconciliation_leg, '
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
            public.reconciliation_leg,
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
                  FROM public.evidence_object AS eo
                  LEFT JOIN public.audit_event AS a ON a.id = eo.audit_event_id
                 WHERE a.id IS NULL
                    OR eo.xmin IS DISTINCT FROM a.xmin
                    OR a.action IS DISTINCT FROM 'evidence.object.create'
                    OR a.payload IS DISTINCT FROM jsonb_build_object(
                        'evidence_ref', eo.evidence_ref::text,
                        'entity_id', eo.entity_id::text,
                        'business_unit_id', eo.business_unit_id::text
                    )
            ) THEN
                RAISE EXCEPTION 'existing evidence audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.encrypted_blob_version AS b
                  LEFT JOIN public.encrypted_blob_version AS p
                    ON p.blob_ref = b.predecessor_blob_ref
                  LEFT JOIN public.audit_event AS a ON a.id = b.audit_event_id
                 WHERE (b.predecessor_blob_ref IS NOT NULL
                        AND p.evidence_ref IS DISTINCT FROM b.evidence_ref)
                     OR a.id IS NULL
                     OR b.xmin IS DISTINCT FROM a.xmin
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
                  LEFT JOIN public.audit_event AS mode_audit
                    ON mode_audit.id = b.audit_event_id
                 WHERE (b.predecessor_blob_ref IS NULL
                        AND (b.audit_event_id IS NULL
                             OR jsonb_typeof(mode_audit.payload -> 'rotation_mode')
                                   IS DISTINCT FROM 'string'
                             OR mode_audit.payload ->> 'rotation_mode'
                                   IS DISTINCT FROM 'GENESIS'))
                     OR (b.predecessor_blob_ref IS NOT NULL
                         AND (jsonb_typeof(mode_audit.payload -> 'rotation_mode')
                                   IS DISTINCT FROM 'string'
                              OR (mode_audit.payload ->> 'rotation_mode'
                                   IS DISTINCT FROM 'REWRAP'
                                  AND mode_audit.payload ->> 'rotation_mode'
                                   IS DISTINCT FROM 'REENCRYPT')))
            ) THEN
                RAISE EXCEPTION 'existing encrypted blob rotation mode is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.encrypted_blob_version AS b
                  JOIN public.encrypted_blob_version AS p
                    ON p.blob_ref = b.predecessor_blob_ref
                  JOIN public.audit_event AS a ON a.id = b.audit_event_id
                 WHERE a.payload ->> 'rotation_mode' = 'REWRAP'
                   AND (
                       b.evidence_ref IS DISTINCT FROM p.evidence_ref
                       OR b.object_ref IS DISTINCT FROM p.object_ref
                       OR b.envelope_schema IS DISTINCT FROM p.envelope_schema
                       OR b.algorithm IS DISTINCT FROM p.algorithm
                       OR b.chunk_size IS DISTINCT FROM p.chunk_size
                       OR b.stream_header IS DISTINCT FROM p.stream_header
                       OR b.purpose IS DISTINCT FROM p.purpose
                       OR NOT (b.wrapped_key_generation IS DISTINCT FROM p.wrapped_key_generation)
                       OR NOT (b.wrapped_key_nonce IS DISTINCT FROM p.wrapped_key_nonce)
                       OR NOT (b.wrapped_key_ciphertext IS DISTINCT FROM p.wrapped_key_ciphertext)
                       OR NOT (b.ciphertext_sha256 IS DISTINCT FROM p.ciphertext_sha256)
                       OR NOT (b.ciphertext_size IS DISTINCT FROM p.ciphertext_size)
                       OR NOT (b.storage_key IS DISTINCT FROM p.storage_key)
                   )
             ) THEN
                RAISE EXCEPTION
                    'existing encrypted blob rotation mode is invalid: REWRAP fields are not closed'
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
                 WHERE c.entity_id IS DISTINCT FROM e.entity_id
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
                     OR e.xmin IS DISTINCT FROM a.xmin
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
                         AND (e.to_revision IS DISTINCT FROM 1 OR e.from_revision IS NOT NULL
                              OR e.from_status IS NOT NULL OR e.action IS NOT NULL
                              OR e.derived_candidate_id IS NOT NULL
                              OR e.to_status NOT IN ('INCOMPLETE','CONFLICTED','PENDING')))
                    OR (e.event_type <> 'CREATE'
                        AND (e.from_revision IS NULL OR e.from_status IS NULL
                             OR e.action IS DISTINCT FROM e.event_type
                              OR e.to_revision IS DISTINCT FROM e.from_revision + 1
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
                  LEFT JOIN public.source_record AS sr ON sr.id = cs.source_record_id
                  LEFT JOIN public.raw_artifact AS ra ON ra.id = sr.artifact_id
                 WHERE cs.source_record_id IS NOT NULL
                   AND (sr.id IS NULL OR ra.id IS NULL
                        OR cs.source_system_id IS DISTINCT FROM sr.source
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
                 WHERE l.posting_id IS NULL
                    OR l.entity_id IS NULL OR l.business_unit_id IS NULL
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
                  LEFT JOIN public.posting AS p ON p.id = l.posting_id
                  LEFT JOIN public.journal_entry AS je ON je.id = p.entry_id
                  LEFT JOIN public.journal_entry_attribution AS ja ON ja.entry_id = je.id
                 WHERE p.id IS NULL OR je.id IS NULL OR ja.entry_id IS NULL
                    OR (l.is_primary IS TRUE
                        AND p.account_id IS DISTINCT FROM je.primary_account_id)
                     OR (p.account_id IS NULL
                         OR p.entry_id IS DISTINCT FROM je.id
                         OR je.entity_id IS DISTINCT FROM l.entity_id
                         OR ja.entity_id IS DISTINCT FROM l.entity_id
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
    # 0012 fresh installs already use VARCHAR(32).  Only rewrite an older
    # deployed VARCHAR(24) column when its actual typmod is narrower; an
    # unconditional ALTER TYPE rewrites every candidate row and changes xmin,
    # which would make the legacy same-transaction audit preflight appear
    # non-atomic.
    op.execute(
        """
        DO $contract_width$
        DECLARE
            v_typmod integer;
        BEGIN
            SELECT a.atttypmod INTO v_typmod
              FROM pg_attribute AS a
              JOIN pg_class AS c ON c.oid = a.attrelid
              JOIN pg_namespace AS n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public'
               AND c.relname = 'candidate'
               AND a.attname = 'contract_version'
               AND NOT a.attisdropped;
            IF v_typmod IS NOT NULL AND v_typmod > 0 AND v_typmod < 36 THEN
                EXECUTE 'ALTER TABLE public.candidate '
                     || 'ALTER COLUMN contract_version TYPE VARCHAR(32)';
            END IF;
        END
        $contract_width$;
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
    op.execute(
        """
        DO $evidence_lineage_preflight$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM public.evidence_object AS e
                  LEFT JOIN public.source_record AS sr
                    ON sr.id = e.source_record_id
                 WHERE (e.source_record_id IS NULL AND e.raw_artifact_id IS NOT NULL)
                    OR (e.source_record_id IS NOT NULL
                        AND (sr.id IS NULL
                             OR e.raw_artifact_id IS NULL
                             OR sr.artifact_id IS DISTINCT FROM e.raw_artifact_id))
            ) THEN
                RAISE EXCEPTION 'existing evidence provenance is incomplete or contradictory'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END
        $evidence_lineage_preflight$;
        """
    )
    op.execute(
        """
        DO $candidate_provenance_preflight$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM public.candidate AS c
                  JOIN public.candidate_source AS cs ON cs.candidate_id = c.id
                  JOIN public.candidate_evidence AS ce ON ce.candidate_id = c.id
                  JOIN public.evidence_object AS e ON e.evidence_ref = ce.evidence_ref
                  LEFT JOIN public.source_record AS source_sr
                    ON source_sr.id = cs.source_record_id
                  LEFT JOIN public.source_record AS evidence_sr
                    ON evidence_sr.id = e.source_record_id
                 WHERE (cs.source_record_id IS NULL
                        AND (e.source_record_id IS NOT NULL OR e.raw_artifact_id IS NOT NULL))
                    OR (cs.source_record_id IS NOT NULL
                        AND (source_sr.id IS NULL
                             OR e.source_record_id IS DISTINCT FROM cs.source_record_id
                             OR evidence_sr.artifact_id IS DISTINCT FROM e.raw_artifact_id
                             OR e.raw_artifact_id IS NULL
                             OR source_sr.artifact_id IS DISTINCT FROM e.raw_artifact_id))
            ) THEN
                RAISE EXCEPTION 'existing candidate source and evidence provenance is not closed'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END
        $candidate_provenance_preflight$;
        """
    )

    # Candidate evidence links carry the scope values used by deferred
    # composite FKs.  Backfill is deterministic; any missing parent aborts.
    op.add_column("candidate_evidence", sa.Column("candidate_entity_id", UUID, nullable=True))
    op.add_column("candidate_evidence", sa.Column("evidence_entity_id", UUID, nullable=True))
    op.add_column("candidate_evidence", sa.Column("evidence_business_unit_id", UUID, nullable=True))
    # The legacy table is append-only for application writes.  Temporarily
    # remove only that trigger while the migration owner backfills deterministic
    # scope copies; the trigger is restored before the migration returns.
    op.execute(
        "DROP TRIGGER r1_candidate_evidence_append_only_trigger ON public.candidate_evidence"
    )
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
    op.execute(
        """
        CREATE TRIGGER r1_candidate_evidence_append_only_trigger
        BEFORE UPDATE OR DELETE ON public.candidate_evidence
        FOR EACH ROW EXECUTE FUNCTION public.r1_candidate_evidence_append_only();
        """
    )
    op.alter_column("candidate_evidence", "candidate_entity_id", nullable=False)
    op.alter_column("candidate_evidence", "evidence_entity_id", nullable=False)
    op.alter_column("candidate_evidence", "evidence_business_unit_id", nullable=False)
    # Replace the 0012 trigger body only after the copied scope columns exist;
    # the function body intentionally reads those columns.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.r1_validate_revision_dimensions()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
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
                IF unit_entity IS NULL OR unit_entity IS DISTINCT FROM v_candidate_entity_id
                   OR unit_ref IS DISTINCT FROM NEW.business_unit_ref_snapshot
                   OR unit_label IS DISTINCT FROM NEW.business_unit_label_snapshot THEN
                    RAISE EXCEPTION 'candidate business unit scope or snapshot is invalid'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            IF NEW.category_id IS NOT NULL THEN
                SELECT r.entity_id, r.code, r.label INTO category_entity, category_code, category_label
                  FROM public.reporting_category AS r WHERE r.id = NEW.category_id;
                IF category_entity IS NULL OR category_entity IS DISTINCT FROM v_candidate_entity_id
                   OR category_code IS DISTINCT FROM NEW.category_code_snapshot
                   OR category_label IS DISTINCT FROM NEW.category_label_snapshot THEN
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
    # This is the single deferred Candidate closure validator.  The trigger
    # wrappers below call it for every creation/revision/event/evidence/typed
    # child write, while the migration preflight calls it with no focus event
    # to validate already-committed legacy rows without pretending their old
    # xmin is still current.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.r1_check_candidate_closure(
            p_candidate_id uuid,
            p_focus_event_ref uuid DEFAULT NULL
        )
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_candidate public.candidate%ROWTYPE;
            v_source public.candidate_source%ROWTYPE;
            v_revision public.candidate_revision%ROWTYPE;
            v_previous public.candidate_revision%ROWTYPE;
            v_derived_revision public.candidate_revision%ROWTYPE;
            v_event public.candidate_event%ROWTYPE;
            v_create_event public.candidate_event%ROWTYPE;
            v_focus_xid xid;
            v_create_xid xid;
            v_row_xid xid;
            v_successor uuid;
            v_successor_create uuid;
            v_source_record_id uuid;
            v_source_artifact_id uuid;
            v_source_system text;
            v_ingest_channel text;
            v_min_revision integer;
            v_max_revision integer;
            v_revision_count bigint;
            v_count bigint;
            v_expected_count bigint;
            v_normalized_changes bigint;
        BEGIN
            SELECT c.* INTO STRICT v_candidate
              FROM public.candidate AS c
             WHERE c.id = p_candidate_id;

            IF EXISTS (
                WITH RECURSIVE successor_walk(candidate_id, path, cycle) AS (
                    SELECT p_candidate_id, ARRAY[p_candidate_id]::uuid[], false
                    UNION ALL
                    SELECT successor.id,
                           successor_walk.path || successor.id,
                           successor.id = ANY(successor_walk.path)
                      FROM successor_walk
                      JOIN public.candidate AS successor
                        ON successor.supersedes_candidate_id = successor_walk.candidate_id
                     WHERE NOT successor_walk.cycle
                )
                SELECT 1 FROM successor_walk WHERE cycle
            ) THEN
                RAISE EXCEPTION 'candidate supersede graph contains a cycle'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            SELECT count(*) INTO v_count
              FROM public.candidate_source AS cs
             WHERE cs.candidate_id = p_candidate_id;
            IF v_count IS DISTINCT FROM 1 THEN
                RAISE EXCEPTION 'candidate requires exactly one source row'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT cs.* INTO STRICT v_source
              FROM public.candidate_source AS cs
             WHERE cs.candidate_id = p_candidate_id;

            SELECT count(*) INTO v_count
              FROM public.candidate_evidence AS ce
             WHERE ce.candidate_id = p_candidate_id;
            IF v_count < 1 THEN
                RAISE EXCEPTION 'candidate creation requires at least one evidence link'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            SELECT count(*) INTO v_count
              FROM public.candidate_event AS ce
             WHERE ce.candidate_id = p_candidate_id
               AND ce.event_type = 'CREATE'
               AND ce.to_revision = 1;
            IF v_count IS DISTINCT FROM 1 THEN
                RAISE EXCEPTION 'candidate requires exactly one CREATE event'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT ce.* INTO STRICT v_create_event
              FROM public.candidate_event AS ce
             WHERE ce.candidate_id = p_candidate_id
               AND ce.event_type = 'CREATE'
               AND ce.to_revision = 1;
            IF v_candidate.supersedes_candidate_id IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1
                     FROM public.candidate_event AS parent_event
                    WHERE parent_event.candidate_id = v_candidate.supersedes_candidate_id
                      AND parent_event.event_type = 'SUPERSEDE'
                      AND parent_event.to_status = 'SUPERSEDED'
                      AND parent_event.derived_candidate_id = p_candidate_id
               ) THEN
                RAISE EXCEPTION 'successor candidate must be linked by a SUPERSEDE event'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF p_focus_event_ref IS NOT NULL THEN
                SELECT a.xmin INTO v_focus_xid
                  FROM public.candidate_event AS ce
                  JOIN public.audit_event AS a ON a.id = ce.audit_event_id
                 WHERE ce.event_ref = p_focus_event_ref;
                IF v_focus_xid IS DISTINCT FROM pg_current_xact_id()::text::xid
                   OR pg_xact_status(v_focus_xid::text::xid8)
                        IS DISTINCT FROM 'in progress' THEN
                    RAISE EXCEPTION 'candidate event and its audited children must share one transaction'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;

            SELECT min(cr.revision), max(cr.revision), count(*)
              INTO v_min_revision, v_max_revision, v_revision_count
              FROM public.candidate_revision AS cr
             WHERE cr.candidate_id = p_candidate_id;
            IF v_min_revision IS DISTINCT FROM 1
               OR v_max_revision IS DISTINCT FROM v_revision_count::integer THEN
                RAISE EXCEPTION 'candidate revisions must be contiguous from one'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.candidate_event AS ce
                 WHERE ce.candidate_id = p_candidate_id
                 GROUP BY ce.to_revision
                HAVING count(*) IS DISTINCT FROM 1
            ) OR EXISTS (
                SELECT 1
                  FROM public.candidate_revision AS cr
                  LEFT JOIN public.candidate_event AS ce
                    ON ce.candidate_id = cr.candidate_id
                   AND ce.to_revision = cr.revision
                   AND ce.to_status = cr.status
                 WHERE cr.candidate_id = p_candidate_id
                   AND ce.event_ref IS NULL
            ) OR EXISTS (
                SELECT 1
                  FROM public.candidate_event AS ce
                  LEFT JOIN public.candidate_revision AS cr
                    ON cr.candidate_id = ce.candidate_id
                   AND cr.revision = ce.to_revision
                   AND cr.status = ce.to_status
                 WHERE ce.candidate_id = p_candidate_id
                   AND cr.candidate_id IS NULL
            ) THEN
                RAISE EXCEPTION 'candidate revisions and events must be a one-to-one closure'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            -- Source/evidence provenance is a closed chain.  A synthetic
            -- source may have no source_record, but neither side may then
            -- pretend to carry raw-artifact provenance.
            IF v_source.source_record_id IS NULL THEN
                IF EXISTS (
                    SELECT 1
                      FROM public.candidate_evidence AS ce
                      JOIN public.evidence_object AS e ON e.evidence_ref = ce.evidence_ref
                     WHERE ce.candidate_id = p_candidate_id
                       AND (e.source_record_id IS NOT NULL OR e.raw_artifact_id IS NOT NULL)
                ) THEN
                    RAISE EXCEPTION 'candidate source and evidence provenance are not closed'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            ELSE
                SELECT sr.source, sr.artifact_id, ra.source
                  INTO v_source_system, v_source_artifact_id, v_ingest_channel
                  FROM public.source_record AS sr
                  LEFT JOIN public.raw_artifact AS ra ON ra.id = sr.artifact_id
                 WHERE sr.id = v_source.source_record_id;
                IF v_source_system IS NULL OR v_source_artifact_id IS NULL
                   OR v_ingest_channel IS NULL
                   OR v_source_system IS DISTINCT FROM v_source.source_system_id
                   OR v_ingest_channel IS DISTINCT FROM v_source.ingest_channel_id THEN
                    RAISE EXCEPTION 'candidate source registry provenance is invalid'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF EXISTS (
                    SELECT 1
                      FROM public.candidate_evidence AS ce
                      JOIN public.evidence_object AS e ON e.evidence_ref = ce.evidence_ref
                      LEFT JOIN public.source_record AS sr ON sr.id = e.source_record_id
                     WHERE ce.candidate_id = p_candidate_id
                       AND (e.source_record_id IS DISTINCT FROM v_source.source_record_id
                            OR e.raw_artifact_id IS NULL
                            OR sr.artifact_id IS DISTINCT FROM v_source_artifact_id)
                ) THEN
                    RAISE EXCEPTION 'candidate source and evidence artifacts disagree'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;

            -- Every linked evidence row carries a copied scope, and every
            -- blocker evidence row is checked in the same direction.  The
            -- current tip is used so an unassigned candidate never becomes a
            -- wildcard for another business unit.
            IF EXISTS (
                SELECT 1
                  FROM public.candidate_evidence AS ce
                  JOIN public.evidence_object AS e ON e.evidence_ref = ce.evidence_ref
                 WHERE ce.candidate_id = p_candidate_id
                   AND (ce.candidate_entity_id IS DISTINCT FROM v_candidate.entity_id
                        OR ce.evidence_entity_id IS DISTINCT FROM e.entity_id
                        OR ce.evidence_business_unit_id IS DISTINCT FROM e.business_unit_id
                        OR e.entity_id IS DISTINCT FROM v_candidate.entity_id)
            ) THEN
                RAISE EXCEPTION 'candidate evidence scope is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT cr.* INTO STRICT v_revision
              FROM public.candidate_revision AS cr
             WHERE cr.candidate_id = p_candidate_id
             ORDER BY cr.revision DESC
             LIMIT 1;
            IF v_revision.business_unit_id IS NOT NULL
               AND EXISTS (
                   SELECT 1
                     FROM public.candidate_evidence AS ce
                     JOIN public.evidence_object AS e ON e.evidence_ref = ce.evidence_ref
                    WHERE ce.candidate_id = p_candidate_id
                      AND e.business_unit_id IS DISTINCT FROM v_revision.business_unit_id
               ) THEN
                RAISE EXCEPTION 'assigned candidate evidence must share the current business unit'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.candidate_blocker AS b
                  JOIN public.evidence_object AS e ON e.evidence_ref = b.evidence_ref
                 WHERE b.candidate_id = p_candidate_id
                   AND e.entity_id IS DISTINCT FROM v_candidate.entity_id
             ) THEN
                RAISE EXCEPTION 'candidate blocker evidence is outside candidate scope'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            FOR v_revision IN
                SELECT cr.*
                  FROM public.candidate_revision AS cr
                 WHERE cr.candidate_id = p_candidate_id
                 ORDER BY cr.revision
            LOOP
                IF v_revision.updated_at < v_revision.created_at THEN
                    RAISE EXCEPTION 'candidate revision timestamps are not monotonic'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                SELECT ce.* INTO STRICT v_event
                  FROM public.candidate_event AS ce
                 WHERE ce.candidate_id = p_candidate_id
                   AND ce.to_revision = v_revision.revision;
                IF v_event.occurred_at IS DISTINCT FROM v_revision.updated_at THEN
                    RAISE EXCEPTION 'candidate event timestamp must match revision timestamp'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF EXISTS (
                    SELECT 1
                      FROM public.candidate_blocker AS b
                      JOIN public.evidence_object AS e ON e.evidence_ref = b.evidence_ref
                     WHERE b.candidate_id = p_candidate_id
                       AND b.revision = v_revision.revision
                       AND (e.entity_id IS DISTINCT FROM v_candidate.entity_id
                            OR (v_revision.business_unit_id IS NOT NULL
                                AND e.business_unit_id IS DISTINCT FROM v_revision.business_unit_id))
                ) THEN
                    RAISE EXCEPTION 'candidate blocker evidence is outside revision scope'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;

                IF v_revision.revision = 1 THEN
                    IF v_revision.created_at IS DISTINCT FROM v_revision.updated_at
                       OR v_candidate.created_at IS DISTINCT FROM v_revision.created_at THEN
                        RAISE EXCEPTION 'initial candidate revision timestamps must be equal'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF v_event.event_type IS DISTINCT FROM 'CREATE'
                       OR v_event.to_revision IS DISTINCT FROM 1
                       OR v_event.from_revision IS DISTINCT FROM NULL
                       OR v_event.from_status IS DISTINCT FROM NULL
                       OR v_event.action IS DISTINCT FROM NULL
                       OR v_event.derived_candidate_id IS DISTINCT FROM NULL
                       OR v_event.to_status NOT IN ('INCOMPLETE','CONFLICTED','PENDING') THEN
                        RAISE EXCEPTION 'CREATE event has an invalid shape'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF EXISTS (
                        SELECT 1 FROM public.candidate_field_change AS fc
                         WHERE fc.event_ref = v_event.event_ref
                    ) OR EXISTS (
                        SELECT 1 FROM public.candidate_conflict_resolution AS cr
                         WHERE cr.event_ref = v_event.event_ref
                    ) THEN
                        RAISE EXCEPTION 'CREATE event cannot carry typed decision children'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                ELSE
                    IF NOT EXISTS (
                        SELECT 1
                          FROM public.candidate_event AS predecessor_event
                         WHERE predecessor_event.candidate_id = p_candidate_id
                           AND predecessor_event.to_revision = v_revision.revision - 1
                    ) THEN
                        RAISE EXCEPTION 'candidate transition has no unique predecessor event'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    SELECT previous.* INTO STRICT v_previous
                      FROM public.candidate_revision AS previous
                     WHERE previous.candidate_id = p_candidate_id
                       AND previous.revision = v_revision.revision - 1;
                    IF v_event.occurred_at < v_previous.updated_at THEN
                        RAISE EXCEPTION 'candidate event predates its predecessor revision'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF v_event.event_type IS DISTINCT FROM v_event.action
                       OR v_event.from_revision IS DISTINCT FROM v_revision.revision - 1
                       OR v_event.from_status IS DISTINCT FROM v_previous.status
                       OR v_event.to_revision IS DISTINCT FROM v_event.from_revision + 1
                       OR NOT (
                           (v_event.from_status = 'INCOMPLETE'
                            AND v_event.to_status = 'PENDING'
                            AND v_event.action = 'COMPLETE_FIELDS')
                        OR (v_event.from_status = 'CONFLICTED'
                            AND v_event.to_status = 'PENDING'
                            AND v_event.action = 'RESOLVE_CONFLICT')
                        OR (v_event.from_status = 'PENDING'
                            AND v_event.to_status = 'CONFIRMED'
                            AND v_event.action = 'CONFIRM')
                        OR (v_event.from_status IN ('INCOMPLETE','CONFLICTED','PENDING')
                            AND v_event.to_status = 'IGNORED'
                            AND v_event.action = 'IGNORE')
                        OR (v_event.from_status = 'CONFIRMED'
                            AND v_event.to_status = 'SUPERSEDED'
                            AND v_event.action = 'SUPERSEDE')
                       ) THEN
                        RAISE EXCEPTION 'candidate transition event has an invalid state edge'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF v_revision.currency IS DISTINCT FROM v_previous.currency
                       OR v_revision.summary IS DISTINCT FROM v_previous.summary
                       OR v_revision.confidence_basis_points
                            IS DISTINCT FROM v_previous.confidence_basis_points THEN
                        RAISE EXCEPTION 'candidate immutable revision fields changed'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF v_event.event_type = 'SUPERSEDE' THEN
                        IF v_event.derived_candidate_id IS NULL THEN
                            RAISE EXCEPTION 'SUPERSEDE requires a derived candidate'
                                USING ERRCODE = 'integrity_constraint_violation';
                        END IF;
                        SELECT count(*) INTO v_count
                          FROM public.candidate AS successor
                         WHERE successor.id = v_event.derived_candidate_id
                           AND successor.supersedes_candidate_id = p_candidate_id
                           AND successor.entity_id IS NOT DISTINCT FROM v_candidate.entity_id;
                        IF v_count IS DISTINCT FROM 1 THEN
                            RAISE EXCEPTION 'candidate supersede requires one same-entity successor'
                                USING ERRCODE = 'integrity_constraint_violation';
                        END IF;
                        v_successor := v_event.derived_candidate_id;
                        SELECT successor_revision.* INTO STRICT v_derived_revision
                          FROM public.candidate_revision AS successor_revision
                         WHERE successor_revision.candidate_id = v_successor
                           AND successor_revision.revision = 1;
                        IF v_derived_revision.status IS DISTINCT FROM 'PENDING'
                           OR EXISTS (
                               SELECT 1
                                 FROM public.candidate_revision AS successor_revision
                                WHERE successor_revision.candidate_id = v_successor
                                  AND successor_revision.revision <> 1
                           ) THEN
                            RAISE EXCEPTION 'SUPERSEDE successor must be revision-1 PENDING'
                                USING ERRCODE = 'integrity_constraint_violation';
                        END IF;
                    END IF;
                    WITH expected(field, previous_value, new_value) AS (
                        SELECT field, previous_value, new_value
                          FROM (VALUES
                              ('status'::text, to_jsonb(v_previous.status), to_jsonb(v_revision.status)),
                              ('business_unit_ref', to_jsonb(v_previous.business_unit_ref_snapshot), to_jsonb(CASE WHEN v_event.event_type = 'SUPERSEDE' THEN v_derived_revision.business_unit_ref_snapshot ELSE v_revision.business_unit_ref_snapshot END)),
                              ('business_unit_label', to_jsonb(v_previous.business_unit_label_snapshot), to_jsonb(CASE WHEN v_event.event_type = 'SUPERSEDE' THEN v_derived_revision.business_unit_label_snapshot ELSE v_revision.business_unit_label_snapshot END)),
                              ('category_code', to_jsonb(v_previous.category_code_snapshot), to_jsonb(CASE WHEN v_event.event_type = 'SUPERSEDE' THEN v_derived_revision.category_code_snapshot ELSE v_revision.category_code_snapshot END)),
                              ('category_label', to_jsonb(v_previous.category_label_snapshot), to_jsonb(CASE WHEN v_event.event_type = 'SUPERSEDE' THEN v_derived_revision.category_label_snapshot ELSE v_revision.category_label_snapshot END)),
                              ('amount_minor', to_jsonb(v_previous.amount_minor), to_jsonb(CASE WHEN v_event.event_type = 'SUPERSEDE' THEN v_derived_revision.amount_minor ELSE v_revision.amount_minor END)),
                              ('accounting_month', to_jsonb(v_previous.accounting_month), to_jsonb(CASE WHEN v_event.event_type = 'SUPERSEDE' THEN v_derived_revision.accounting_month ELSE v_revision.accounting_month END))
                          ) AS values_table(field, previous_value, new_value)
                         WHERE previous_value IS DISTINCT FROM new_value
                    ), actual AS (
                        SELECT fc.field, fc.previous_value, fc.new_value
                          FROM public.candidate_field_change AS fc
                         WHERE fc.event_ref = v_event.event_ref
                    )
                    SELECT 1 INTO v_count
                      FROM expected
                      FULL OUTER JOIN actual ON actual.field = expected.field
                     WHERE expected.field IS DISTINCT FROM actual.field
                        OR actual.previous_value IS DISTINCT FROM expected.previous_value
                        OR actual.new_value IS DISTINCT FROM expected.new_value
                     LIMIT 1;
                    IF FOUND THEN
                        RAISE EXCEPTION 'candidate field changes do not exactly match revisions'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF v_event.event_type = 'COMPLETE_FIELDS' AND EXISTS (
                        SELECT 1
                          FROM (VALUES
                              (to_jsonb(v_previous.business_unit_ref_snapshot), to_jsonb(v_revision.business_unit_ref_snapshot)),
                              (to_jsonb(v_previous.business_unit_label_snapshot), to_jsonb(v_revision.business_unit_label_snapshot)),
                              (to_jsonb(v_previous.category_code_snapshot), to_jsonb(v_revision.category_code_snapshot)),
                              (to_jsonb(v_previous.category_label_snapshot), to_jsonb(v_revision.category_label_snapshot)),
                              (to_jsonb(v_previous.amount_minor), to_jsonb(v_revision.amount_minor)),
                              (to_jsonb(v_previous.accounting_month), to_jsonb(v_revision.accounting_month))
                          ) AS normalized(previous_value, new_value)
                         WHERE previous_value IS NOT NULL
                           AND new_value IS DISTINCT FROM previous_value
                    ) THEN
                        RAISE EXCEPTION 'COMPLETE_FIELDS may only fill missing normalized fields'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    SELECT count(*) INTO v_normalized_changes
                      FROM (VALUES
                          (to_jsonb(v_previous.business_unit_ref_snapshot), to_jsonb(CASE WHEN v_event.event_type = 'SUPERSEDE' THEN v_derived_revision.business_unit_ref_snapshot ELSE v_revision.business_unit_ref_snapshot END)),
                          (to_jsonb(v_previous.business_unit_label_snapshot), to_jsonb(CASE WHEN v_event.event_type = 'SUPERSEDE' THEN v_derived_revision.business_unit_label_snapshot ELSE v_revision.business_unit_label_snapshot END)),
                          (to_jsonb(v_previous.category_code_snapshot), to_jsonb(CASE WHEN v_event.event_type = 'SUPERSEDE' THEN v_derived_revision.category_code_snapshot ELSE v_revision.category_code_snapshot END)),
                          (to_jsonb(v_previous.category_label_snapshot), to_jsonb(CASE WHEN v_event.event_type = 'SUPERSEDE' THEN v_derived_revision.category_label_snapshot ELSE v_revision.category_label_snapshot END)),
                          (to_jsonb(v_previous.amount_minor), to_jsonb(CASE WHEN v_event.event_type = 'SUPERSEDE' THEN v_derived_revision.amount_minor ELSE v_revision.amount_minor END)),
                          (to_jsonb(v_previous.accounting_month), to_jsonb(CASE WHEN v_event.event_type = 'SUPERSEDE' THEN v_derived_revision.accounting_month ELSE v_revision.accounting_month END))
                      ) AS normalized(previous_value, new_value)
                     WHERE previous_value IS DISTINCT FROM new_value;
                    IF v_event.event_type = 'COMPLETE_FIELDS'
                       AND v_normalized_changes < 1 THEN
                        RAISE EXCEPTION 'COMPLETE_FIELDS requires a normalized field change'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                     IF v_event.event_type IN ('CONFIRM','IGNORE')
                        AND v_normalized_changes IS DISTINCT FROM 0 THEN
                        RAISE EXCEPTION 'CONFIRM and IGNORE cannot change normalized fields'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF v_event.event_type = 'SUPERSEDE'
                       AND (v_normalized_changes < 1
                            OR v_revision.business_unit_id IS DISTINCT FROM v_previous.business_unit_id
                            OR v_revision.business_unit_ref_snapshot IS DISTINCT FROM v_previous.business_unit_ref_snapshot
                            OR v_revision.business_unit_label_snapshot IS DISTINCT FROM v_previous.business_unit_label_snapshot
                            OR v_revision.category_id IS DISTINCT FROM v_previous.category_id
                            OR v_revision.category_code_snapshot IS DISTINCT FROM v_previous.category_code_snapshot
                            OR v_revision.category_label_snapshot IS DISTINCT FROM v_previous.category_label_snapshot
                            OR v_revision.amount_minor IS DISTINCT FROM v_previous.amount_minor
                            OR v_revision.accounting_month IS DISTINCT FROM v_previous.accounting_month) THEN
                        RAISE EXCEPTION 'SUPERSEDE must preserve source and change successor fields'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF v_event.event_type = 'RESOLVE_CONFLICT' THEN
                        SELECT count(*) INTO v_count
                          FROM public.candidate_conflict_resolution AS cr
                         WHERE cr.event_ref = v_event.event_ref;
                        IF v_count < 1 OR EXISTS (
                            SELECT 1
                              FROM public.candidate_conflict_resolution AS cr
                             WHERE cr.event_ref = v_event.event_ref
                               AND NOT EXISTS (
                                   SELECT 1
                                     FROM public.candidate_blocker AS b
                                    WHERE b.candidate_id = p_candidate_id
                                      AND b.revision = v_event.from_revision
                                      AND b.conflict_ref IS NOT DISTINCT FROM cr.conflict_ref
                                       AND b.code IN ('AMBIGUOUS_EXTRACTION','EVIDENCE_INCOMPLETE',
                                                      'UNSUPPORTED_ATTACHMENT','DUPLICATE_MESSAGE',
                                                      'DUPLICATE_ATTACHMENT','BUSINESS_KEY_CONFLICT',
                                                      'CROSS_FORMAT_DUPLICATE')
                               )
                        ) OR EXISTS (
                            SELECT 1
                              FROM public.candidate_blocker AS b
                             WHERE b.candidate_id = p_candidate_id
                               AND b.revision = v_event.from_revision
                                AND b.code IN ('AMBIGUOUS_EXTRACTION','EVIDENCE_INCOMPLETE',
                                               'UNSUPPORTED_ATTACHMENT','DUPLICATE_MESSAGE',
                                               'DUPLICATE_ATTACHMENT','BUSINESS_KEY_CONFLICT',
                                               'CROSS_FORMAT_DUPLICATE')
                               AND NOT EXISTS (
                                   SELECT 1
                                     FROM public.candidate_conflict_resolution AS cr
                                    WHERE cr.event_ref = v_event.event_ref
                                      AND cr.conflict_ref IS NOT DISTINCT FROM b.conflict_ref
                               )
                        ) OR EXISTS (
                            SELECT 1
                              FROM public.candidate_blocker AS b
                             WHERE b.candidate_id = p_candidate_id
                               AND b.revision = v_revision.revision
                        ) THEN
                            RAISE EXCEPTION 'RESOLVE_CONFLICT typed children do not close conflicts'
                                USING ERRCODE = 'integrity_constraint_violation';
                        END IF;
                    END IF;
                    IF v_event.event_type IN ('COMPLETE_FIELDS','CONFIRM','IGNORE','SUPERSEDE')
                       AND EXISTS (
                           SELECT 1 FROM public.candidate_conflict_resolution AS cr
                            WHERE cr.event_ref = v_event.event_ref
                       ) THEN
                        RAISE EXCEPTION 'conflict resolutions are exclusive to RESOLVE_CONFLICT'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF v_event.event_type IN ('CONFIRM','IGNORE')
                       AND EXISTS (
                           SELECT 1 FROM public.candidate_field_change AS fc
                            WHERE fc.event_ref = v_event.event_ref
                              AND fc.field IS DISTINCT FROM 'status'
                       ) THEN
                        RAISE EXCEPTION 'typed field changes are not allowed for this action'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                END IF;

                IF v_revision.status = 'INCOMPLETE' THEN
                    SELECT count(*) INTO v_expected_count
                      FROM (VALUES
                          ('MISSING_BUSINESS_UNIT'::text, 'business_unit'::text, v_revision.business_unit_id IS NULL),
                          ('MISSING_CATEGORY'::text, 'category'::text, v_revision.category_id IS NULL),
                          ('MISSING_AMOUNT'::text, 'amount_minor'::text, v_revision.amount_minor IS NULL),
                          ('MISSING_ACCOUNTING_MONTH'::text, 'accounting_month'::text, v_revision.accounting_month IS NULL)
                      ) AS missing(code, field, is_missing)
                     WHERE is_missing;
                    SELECT count(*) INTO v_count
                      FROM public.candidate_blocker AS b
                     WHERE b.candidate_id = p_candidate_id
                       AND b.revision = v_revision.revision
                       AND b.code IN ('MISSING_BUSINESS_UNIT','MISSING_CATEGORY',
                                      'MISSING_AMOUNT','MISSING_ACCOUNTING_MONTH');
                     IF v_expected_count < 1
                         OR v_count IS DISTINCT FROM v_expected_count
                         OR EXISTS (
                             SELECT 1
                               FROM (VALUES
                                   ('MISSING_BUSINESS_UNIT'::text, 'business_unit'::text, v_revision.business_unit_id IS NULL),
                                   ('MISSING_CATEGORY'::text, 'category'::text, v_revision.category_id IS NULL),
                                   ('MISSING_AMOUNT'::text, 'amount_minor'::text, v_revision.amount_minor IS NULL),
                                   ('MISSING_ACCOUNTING_MONTH'::text, 'accounting_month'::text, v_revision.accounting_month IS NULL)
                               ) AS missing(code, field, is_missing)
                              WHERE missing.is_missing
                                AND NOT EXISTS (
                                    SELECT 1
                                      FROM public.candidate_blocker AS b
                                     WHERE b.candidate_id = p_candidate_id
                                       AND b.revision = v_revision.revision
                                       AND b.code = missing.code
                                       AND b.field IS NOT DISTINCT FROM missing.field
                                )
                         )
                         OR EXISTS (
                            SELECT 1
                             FROM public.candidate_blocker AS b
                             LEFT JOIN (VALUES
                                 ('MISSING_BUSINESS_UNIT'::text, 'business_unit'::text, v_revision.business_unit_id IS NULL),
                                 ('MISSING_CATEGORY'::text, 'category'::text, v_revision.category_id IS NULL),
                                 ('MISSING_AMOUNT'::text, 'amount_minor'::text, v_revision.amount_minor IS NULL),
                                 ('MISSING_ACCOUNTING_MONTH'::text, 'accounting_month'::text, v_revision.accounting_month IS NULL)
                             ) AS missing(code, field, is_missing)
                               ON missing.code = b.code
                            WHERE b.candidate_id = p_candidate_id
                              AND b.revision = v_revision.revision
                              AND b.code IN ('MISSING_BUSINESS_UNIT','MISSING_CATEGORY',
                                             'MISSING_AMOUNT','MISSING_ACCOUNTING_MONTH')
                              AND (missing.is_missing IS DISTINCT FROM true
                                   OR b.field IS DISTINCT FROM missing.field)
                        ) THEN
                         RAISE EXCEPTION 'INCOMPLETE blockers do not match null normalized fields'
                             USING ERRCODE = 'integrity_constraint_violation';
                     END IF;
                     IF EXISTS (
                         SELECT 1
                           FROM public.candidate_blocker AS b
                          WHERE b.candidate_id = p_candidate_id
                            AND b.revision = v_revision.revision
                            AND b.code NOT IN (
                                'MISSING_BUSINESS_UNIT','MISSING_CATEGORY',
                                'MISSING_AMOUNT','MISSING_ACCOUNTING_MONTH',
                                'PARSE_FAILED','DEPENDENCY_UNAVAILABLE'
                            )
                     ) OR EXISTS (
                         SELECT 1
                           FROM public.candidate_blocker AS b
                          WHERE b.candidate_id = p_candidate_id
                           AND b.revision = v_revision.revision
                             AND b.code IN ('AMBIGUOUS_EXTRACTION','EVIDENCE_INCOMPLETE',
                                            'UNSUPPORTED_ATTACHMENT','DUPLICATE_MESSAGE',
                                            'DUPLICATE_ATTACHMENT','BUSINESS_KEY_CONFLICT',
                                            'CROSS_FORMAT_DUPLICATE')
                    ) THEN
                        RAISE EXCEPTION 'INCOMPLETE cannot carry conflict blockers'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                ELSIF v_revision.status = 'CONFLICTED' THEN
                    IF v_revision.business_unit_id IS NULL
                       OR v_revision.category_id IS NULL
                       OR v_revision.amount_minor IS NULL
                       OR v_revision.accounting_month IS NULL
                       OR NOT EXISTS (
                            SELECT 1 FROM public.candidate_blocker AS b
                             WHERE b.candidate_id = p_candidate_id
                               AND b.revision = v_revision.revision
                               AND b.code IN ('AMBIGUOUS_EXTRACTION','EVIDENCE_INCOMPLETE',
                                              'UNSUPPORTED_ATTACHMENT','DUPLICATE_MESSAGE',
                                              'DUPLICATE_ATTACHMENT','BUSINESS_KEY_CONFLICT',
                                              'CROSS_FORMAT_DUPLICATE')
                       )
                       OR EXISTS (
                           SELECT 1 FROM public.candidate_blocker AS b
                            WHERE b.candidate_id = p_candidate_id
                              AND b.revision = v_revision.revision
                             AND (b.code NOT IN ('AMBIGUOUS_EXTRACTION','EVIDENCE_INCOMPLETE',
                                                 'UNSUPPORTED_ATTACHMENT','DUPLICATE_MESSAGE',
                                                 'DUPLICATE_ATTACHMENT','BUSINESS_KEY_CONFLICT',
                                                 'CROSS_FORMAT_DUPLICATE')
                                   OR b.conflict_ref IS NULL)
                       ) THEN
                        RAISE EXCEPTION 'CONFLICTED requires complete fields and typed conflict blockers'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                ELSIF v_revision.status IN ('PENDING','CONFIRMED','SUPERSEDED') THEN
                    IF EXISTS (
                        SELECT 1 FROM public.candidate_blocker AS b
                         WHERE b.candidate_id = p_candidate_id
                           AND b.revision = v_revision.revision
                    ) THEN
                        RAISE EXCEPTION 'complete candidate states cannot carry blockers'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                END IF;
                IF EXISTS (
                    SELECT 1
                      FROM public.candidate_blocker AS b
                     WHERE b.candidate_id = p_candidate_id
                       AND b.revision = v_revision.revision
                       AND ((b.code IN ('AMBIGUOUS_EXTRACTION','EVIDENCE_INCOMPLETE',
                                        'UNSUPPORTED_ATTACHMENT','DUPLICATE_MESSAGE',
                                        'DUPLICATE_ATTACHMENT','BUSINESS_KEY_CONFLICT',
                                        'CROSS_FORMAT_DUPLICATE')
                             AND b.conflict_ref IS NULL)
                            OR (b.code NOT IN ('AMBIGUOUS_EXTRACTION','EVIDENCE_INCOMPLETE',
                                               'UNSUPPORTED_ATTACHMENT','DUPLICATE_MESSAGE',
                                               'DUPLICATE_ATTACHMENT','BUSINESS_KEY_CONFLICT',
                                               'CROSS_FORMAT_DUPLICATE')
                                AND b.conflict_ref IS NOT NULL))
                ) THEN
                    RAISE EXCEPTION 'candidate blocker typed nullness is invalid'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF v_revision.status = 'IGNORED' THEN
                    IF v_revision.revision = 1 THEN
                        RAISE EXCEPTION 'IGNORED cannot be an initial candidate state'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF v_revision.business_unit_id IS DISTINCT FROM v_previous.business_unit_id
                       OR v_revision.business_unit_ref_snapshot IS DISTINCT FROM v_previous.business_unit_ref_snapshot
                       OR v_revision.business_unit_label_snapshot IS DISTINCT FROM v_previous.business_unit_label_snapshot
                       OR v_revision.category_id IS DISTINCT FROM v_previous.category_id
                       OR v_revision.category_code_snapshot IS DISTINCT FROM v_previous.category_code_snapshot
                       OR v_revision.category_label_snapshot IS DISTINCT FROM v_previous.category_label_snapshot
                       OR v_revision.amount_minor IS DISTINCT FROM v_previous.amount_minor
                       OR v_revision.accounting_month IS DISTINCT FROM v_previous.accounting_month
                       OR EXISTS (
                           SELECT 1
                             FROM public.candidate_blocker AS b
                             FULL OUTER JOIN public.candidate_blocker AS old_b
                               ON old_b.candidate_id = p_candidate_id
                              AND old_b.revision = v_previous.revision
                              AND old_b.ordinal = b.ordinal
                            WHERE b.candidate_id = p_candidate_id
                              AND b.revision = v_revision.revision
                              AND (b.code IS DISTINCT FROM old_b.code
                                   OR b.message IS DISTINCT FROM old_b.message
                                   OR b.field IS DISTINCT FROM old_b.field
                                   OR b.conflict_ref IS DISTINCT FROM old_b.conflict_ref
                                   OR b.evidence_ref IS DISTINCT FROM old_b.evidence_ref)
                       ) OR (
                           SELECT count(*) FROM public.candidate_blocker AS b
                            WHERE b.candidate_id = p_candidate_id
                              AND b.revision = v_revision.revision
                       ) IS DISTINCT FROM (
                           SELECT count(*) FROM public.candidate_blocker AS old_b
                            WHERE old_b.candidate_id = p_candidate_id
                              AND old_b.revision = v_previous.revision
                       ) THEN
                        RAISE EXCEPTION 'IGNORED must preserve normalized fields and blockers'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                END IF;
            END LOOP;

            -- Creation closure uses exact xmin equality.  This prevents a
            -- later transaction from attaching source/evidence/revision facts
            -- to an already-horizon-visible CREATE audit.
            IF p_focus_event_ref IS NOT NULL
               AND p_focus_event_ref = v_create_event.event_ref THEN
                SELECT a.xmin INTO v_create_xid
                  FROM public.audit_event AS a
                 WHERE a.id = v_create_event.audit_event_id;
                SELECT c.xmin INTO v_row_xid
                  FROM public.candidate AS c
                 WHERE c.id = p_candidate_id;
                IF v_row_xid IS DISTINCT FROM v_create_xid THEN
                    RAISE EXCEPTION 'candidate CREATE closure has a different transaction xmin'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                SELECT cs.xmin INTO v_row_xid
                  FROM public.candidate_source AS cs
                 WHERE cs.candidate_id = p_candidate_id;
                IF v_row_xid IS DISTINCT FROM v_create_xid THEN
                    RAISE EXCEPTION 'candidate source must be created with CREATE'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF EXISTS (
                    SELECT 1
                      FROM public.candidate_event AS ce
                     WHERE ce.event_ref = v_create_event.event_ref
                       AND ce.xmin IS DISTINCT FROM v_create_xid
                ) OR EXISTS (
                    SELECT 1
                      FROM public.candidate_revision AS cr
                     WHERE cr.candidate_id = p_candidate_id
                       AND cr.revision = 1
                       AND cr.xmin IS DISTINCT FROM v_create_xid
                ) OR EXISTS (
                    SELECT 1
                      FROM public.candidate_evidence AS ce
                     WHERE ce.candidate_id = p_candidate_id
                       AND ce.xmin IS DISTINCT FROM v_create_xid
                ) OR v_create_xid IS DISTINCT FROM pg_current_xact_id()::text::xid THEN
                    RAISE EXCEPTION 'candidate CREATE source/revision/event/evidence closure is not atomic'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;

            IF p_focus_event_ref IS NOT NULL THEN
                SELECT a.xmin INTO v_focus_xid
                  FROM public.candidate_event AS ce
                  JOIN public.audit_event AS a ON a.id = ce.audit_event_id
                 WHERE ce.event_ref = p_focus_event_ref;
                SELECT ce.xmin INTO v_row_xid
                  FROM public.candidate_event AS ce
                 WHERE ce.event_ref = p_focus_event_ref;
                IF v_focus_xid IS DISTINCT FROM pg_current_xact_id()::text::xid
                   OR v_row_xid IS DISTINCT FROM v_focus_xid
                   OR pg_xact_status(v_focus_xid::text::xid8)
                        IS DISTINCT FROM 'in progress' THEN
                    RAISE EXCEPTION 'candidate event and audit must share one transaction xmin'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                SELECT cr.xmin INTO v_row_xid
                  FROM public.candidate_revision AS cr
                  JOIN public.candidate_event AS ce
                    ON ce.candidate_id = cr.candidate_id
                   AND ce.to_revision = cr.revision
                 WHERE ce.event_ref = p_focus_event_ref;
                IF v_row_xid IS DISTINCT FROM v_focus_xid THEN
                    RAISE EXCEPTION 'candidate revision and event must share one transaction xmin'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;

            IF EXISTS (
                SELECT 1 FROM public.candidate_event AS ce
                 WHERE ce.candidate_id = p_candidate_id
                   AND ce.event_type = 'SUPERSEDE'
            ) THEN
                FOR v_event IN
                    SELECT ce.*
                      FROM public.candidate_event AS ce
                     WHERE ce.candidate_id = p_candidate_id
                       AND ce.event_type = 'SUPERSEDE'
                LOOP
                    SELECT count(*) INTO v_count
                      FROM public.candidate AS successor
                     WHERE successor.supersedes_candidate_id = p_candidate_id
                       AND successor.entity_id IS NOT DISTINCT FROM v_candidate.entity_id;
                    IF v_count IS DISTINCT FROM 1
                       OR v_event.derived_candidate_id IS NULL THEN
                        RAISE EXCEPTION 'candidate supersede requires one same-entity successor'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    SELECT successor.id INTO v_successor
                      FROM public.candidate AS successor
                     WHERE successor.supersedes_candidate_id = p_candidate_id
                       AND successor.entity_id IS NOT DISTINCT FROM v_candidate.entity_id;
                    SELECT count(*) INTO v_count
                      FROM public.candidate_event AS se
                     WHERE se.candidate_id = v_successor
                       AND se.event_type = 'CREATE'
                       AND se.to_revision = 1
                       AND se.to_status = 'PENDING';
                    IF v_count IS DISTINCT FROM 1
                       OR v_event.derived_candidate_id IS DISTINCT FROM v_successor
                       OR EXISTS (
                           SELECT 1
                             FROM public.candidate_source AS ss
                             WHERE ss.candidate_id = v_successor
                               AND (ss.ingest_channel_id IS DISTINCT FROM v_source.ingest_channel_id
                                    OR ss.source_system_id IS DISTINCT FROM v_source.source_system_id
                                    OR ss.source_event_ref IS DISTINCT FROM v_source.source_event_ref
                                    OR ss.source_record_id IS DISTINCT FROM v_source.source_record_id
                                    OR ss.display_label IS DISTINCT FROM v_source.display_label)
                       ) THEN
                        RAISE EXCEPTION 'SUPERSEDE successor does not inherit source provenance'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1
                          FROM public.candidate_revision AS source_revision
                         WHERE source_revision.candidate_id = p_candidate_id
                           AND source_revision.status = 'CONFIRMED'
                    ) OR EXISTS (
                        SELECT 1
                          FROM public.candidate_revision AS successor_revision
                          JOIN public.candidate_revision AS source_revision
                            ON source_revision.candidate_id = p_candidate_id
                           AND source_revision.status = 'CONFIRMED'
                         WHERE successor_revision.candidate_id = v_successor
                           AND successor_revision.revision = 1
                           AND (successor_revision.currency IS DISTINCT FROM source_revision.currency
                                OR successor_revision.summary IS DISTINCT FROM source_revision.summary
                                OR successor_revision.confidence_basis_points IS DISTINCT FROM source_revision.confidence_basis_points)
                    ) OR NOT EXISTS (
                        SELECT 1
                          FROM public.candidate_revision AS successor_revision
                          JOIN public.candidate_revision AS source_revision
                            ON source_revision.candidate_id = p_candidate_id
                           AND source_revision.status = 'CONFIRMED'
                         WHERE successor_revision.candidate_id = v_successor
                           AND successor_revision.revision = 1
                           AND (successor_revision.business_unit_ref_snapshot IS DISTINCT FROM source_revision.business_unit_ref_snapshot
                                OR successor_revision.business_unit_label_snapshot IS DISTINCT FROM source_revision.business_unit_label_snapshot
                                OR successor_revision.category_code_snapshot IS DISTINCT FROM source_revision.category_code_snapshot
                                OR successor_revision.category_label_snapshot IS DISTINCT FROM source_revision.category_label_snapshot
                                OR successor_revision.amount_minor IS DISTINCT FROM source_revision.amount_minor
                                OR successor_revision.accounting_month IS DISTINCT FROM source_revision.accounting_month)
                    ) THEN
                        RAISE EXCEPTION 'SUPERSEDE successor must inherit immutable fields and change a normalized field'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF EXISTS (
                        SELECT 1
                          FROM public.candidate_evidence AS se
                          FULL OUTER JOIN public.candidate_evidence AS src
                            ON src.candidate_id = p_candidate_id
                            AND src.ordinal = se.ordinal
                          WHERE se.candidate_id = v_successor
                            AND (se.evidence_ref IS DISTINCT FROM src.evidence_ref
                                 OR se.candidate_entity_id IS DISTINCT FROM src.candidate_entity_id
                                 OR se.evidence_entity_id IS DISTINCT FROM src.evidence_entity_id
                                 OR se.evidence_business_unit_id IS DISTINCT FROM src.evidence_business_unit_id
                                 OR se.kind IS DISTINCT FROM src.kind
                                OR se.media_type_snapshot IS DISTINCT FROM src.media_type_snapshot
                                OR se.display_name_snapshot IS DISTINCT FROM src.display_name_snapshot
                                OR se.download_available IS DISTINCT FROM src.download_available)
                    ) OR (
                        SELECT count(*) FROM public.candidate_evidence AS se
                         WHERE se.candidate_id = v_successor
                    ) IS DISTINCT FROM (
                        SELECT count(*) FROM public.candidate_evidence AS src
                         WHERE src.candidate_id = p_candidate_id
                    ) THEN
                        RAISE EXCEPTION 'SUPERSEDE successor does not inherit evidence links'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF p_focus_event_ref IS NOT NULL THEN
                        SELECT se.event_ref INTO v_successor_create
                          FROM public.candidate_event AS se
                         WHERE se.candidate_id = v_successor
                           AND se.event_type = 'CREATE'
                           AND se.to_revision = 1;
                        PERFORM public.r1_check_candidate_closure(v_successor, v_successor_create);
                    ELSE
                        PERFORM public.r1_check_candidate_closure(v_successor, NULL);
                    END IF;
                END LOOP;
            END IF;
        END
        $function$;
        """
    )
    op.execute(
        """
        DO $candidate_closure_preflight$
        DECLARE
            v_candidate_id uuid;
        BEGIN
            FOR v_candidate_id IN SELECT id FROM public.candidate LOOP
                PERFORM public.r1_check_candidate_closure(v_candidate_id, NULL);
            END LOOP;
        END
        $candidate_closure_preflight$;
        """
    )
    op.execute(
        """
        DO $candidate_xmin_preflight$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM public.candidate AS c
                  JOIN public.candidate_source AS cs ON cs.candidate_id = c.id
                  JOIN public.candidate_revision AS r
                    ON r.candidate_id = c.id AND r.revision = 1
                  JOIN public.candidate_event AS e
                    ON e.candidate_id = c.id
                   AND e.event_type = 'CREATE'
                   AND e.to_revision = 1
                  JOIN public.audit_event AS a ON a.id = e.audit_event_id
                 WHERE c.xmin IS DISTINCT FROM a.xmin
                    OR cs.xmin IS DISTINCT FROM a.xmin
                    OR r.xmin IS DISTINCT FROM a.xmin
                    OR e.xmin IS DISTINCT FROM a.xmin
            ) THEN
                RAISE EXCEPTION
                    'existing candidate CREATE source/revision/event/evidence is not atomic'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.candidate_field_change AS fc
                  JOIN public.candidate_event AS e ON e.event_ref = fc.event_ref
                  JOIN public.audit_event AS a ON a.id = e.audit_event_id
                 WHERE fc.xmin IS DISTINCT FROM a.xmin
            ) OR EXISTS (
                SELECT 1
                  FROM public.candidate_conflict_resolution AS cr
                  JOIN public.candidate_event AS e ON e.event_ref = cr.event_ref
                  JOIN public.audit_event AS a ON a.id = e.audit_event_id
                 WHERE cr.xmin IS DISTINCT FROM a.xmin
            ) OR EXISTS (
                SELECT 1
                  FROM public.candidate_blocker AS b
                  JOIN public.candidate_event AS e
                    ON e.candidate_id = b.candidate_id
                   AND e.to_revision = b.revision
                  JOIN public.audit_event AS a ON a.id = e.audit_event_id
                 WHERE b.xmin IS DISTINCT FROM a.xmin
            ) THEN
                RAISE EXCEPTION
                    'existing candidate audited child is not atomic with its event audit'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END
        $candidate_xmin_preflight$;
        """
    )
    # The scope columns now exist and have been deterministically backfilled.
    # Re-check both the copied values and the legacy parent relationship before
    # installing the deferred composite FKs; a bad legacy row must abort rather
    # than be hidden by a partial join or by the later constraint definition.
    op.execute(
        """
        DO $candidate_evidence_scope_preflight$
        BEGIN
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
        END
        $candidate_evidence_scope_preflight$;
        """
    )
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
                 WHERE je.status = 'POSTED'
                   AND (je.primary_account_id IS NULL
                        OR (SELECT count(*)
                              FROM public.posting AS p
                             WHERE p.entry_id = je.id
                               AND p.account_id IS NOT DISTINCT FROM je.primary_account_id)
                              IS DISTINCT FROM 1)
            ) THEN
                RAISE EXCEPTION
                    'existing POSTED journal entry lacks one non-null primary account posting'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
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
                WHERE ja.entity_id IS DISTINCT FROM je.entity_id
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
                WHERE rc.entity_id IS DISTINCT FROM je.entity_id
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
                WHERE posting_id IS NULL
                   OR entity_id IS NULL OR business_unit_id IS NULL
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
    op.alter_column("reconciliation_leg", "posting_id", nullable=False)
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
    op.execute(
        """
        DO $snapshot_revision_preflight$
        BEGIN
            IF EXISTS (
                SELECT entity_id, business_unit_id, accounting_month
                  FROM public.reconciliation_snapshot
                 GROUP BY entity_id, business_unit_id, accounting_month
                HAVING min(snapshot_revision) IS DISTINCT FROM 1
            ) THEN
                RAISE EXCEPTION 'snapshot revision history must start at one'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT entity_id, business_unit_id, accounting_month
                  FROM public.reconciliation_snapshot
                 GROUP BY entity_id, business_unit_id, accounting_month
                HAVING max(snapshot_revision)
                         IS DISTINCT FROM count(*)::integer
            ) THEN
                RAISE EXCEPTION 'snapshot revision history has a gap'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.reconciliation_snapshot AS s
                 WHERE NOT EXISTS (
                     SELECT 1
                        FROM public.audit_event AS watermark
                       WHERE watermark.sequence = s.ledger_audit_sequence
                         AND watermark.hash = s.ledger_audit_hash
                         AND octet_length(watermark.hash) = 32
                         AND watermark.sequence < (
                             SELECT snapshot_audit.sequence
                               FROM public.audit_event AS snapshot_audit
                              WHERE snapshot_audit.id = s.audit_event_id
                         )
                 )
            ) THEN
                RAISE EXCEPTION 'snapshot audit watermark is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END
        $snapshot_revision_preflight$;
        """
    )
    # The blocker child table is available only after the preceding DDL.  Run
    # the complete snapshot/audit preflight here, before the deferred audit
    # uniqueness and trigger surface is installed.  This keeps the migration
    # fail-closed without referring to a future table from the early legacy
    # preflight above.
    op.execute(
        """
        DO $snapshot_audit_preflight$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM public.reconciliation_snapshot AS s
                  LEFT JOIN public.audit_event AS a ON a.id = s.audit_event_id
                 WHERE a.id IS NULL
                    OR s.xmin IS DISTINCT FROM a.xmin
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
                        'blocker_count', (
                            SELECT count(*)
                              FROM public.reconciliation_snapshot_blocker AS b
                             WHERE b.snapshot_ref = s.snapshot_ref
                        ),
                        'proposal_count', (
                            SELECT count(*)
                              FROM public.reconciliation_snapshot_proposal AS p
                             WHERE p.snapshot_ref = s.snapshot_ref
                        ),
                        'suspense_count', (
                            SELECT count(*)
                              FROM public.reconciliation_snapshot_suspense AS x
                             WHERE x.snapshot_ref = s.snapshot_ref
                        )
                    )
                    OR NOT EXISTS (
                        SELECT 1
                          FROM public.audit_event AS watermark
                         WHERE watermark.sequence = s.ledger_audit_sequence
                            AND watermark.hash = s.ledger_audit_hash
                            AND watermark.sequence < (
                                SELECT snapshot_audit.sequence
                                  FROM public.audit_event AS snapshot_audit
                                 WHERE snapshot_audit.id = s.audit_event_id
                            )
                 )
            ) THEN
                RAISE EXCEPTION 'existing reconciliation snapshot audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.reconciliation_snapshot_blocker AS b
                  JOIN public.reconciliation_snapshot AS s
                    ON s.snapshot_ref = b.snapshot_ref
                  JOIN public.audit_event AS a ON a.id = s.audit_event_id
                 WHERE b.xmin IS DISTINCT FROM a.xmin
            ) OR EXISTS (
                SELECT 1
                  FROM public.reconciliation_snapshot_proposal AS p
                  JOIN public.reconciliation_snapshot AS s
                    ON s.snapshot_ref = p.snapshot_ref
                  JOIN public.audit_event AS a ON a.id = s.audit_event_id
                 WHERE p.xmin IS DISTINCT FROM a.xmin
            ) OR EXISTS (
                SELECT 1
                  FROM public.reconciliation_snapshot_suspense AS x
                  JOIN public.reconciliation_snapshot AS s
                    ON s.snapshot_ref = x.snapshot_ref
                  JOIN public.audit_event AS a ON a.id = s.audit_event_id
                 WHERE x.xmin IS DISTINCT FROM a.xmin
            ) THEN
                RAISE EXCEPTION
                    'existing reconciliation snapshot child is not atomic with its audit'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END
        $snapshot_audit_preflight$;
        """
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
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
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
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_artifact uuid;
            v_source_artifact uuid;
            v_audit_xid xid;
            v_action text;
            v_payload jsonb;
        BEGIN
            SELECT a.xmin, a.action, a.payload
              INTO v_audit_xid, v_action, v_payload
              FROM public.audit_event AS a
             WHERE a.id = NEW.audit_event_id;
            IF v_audit_xid IS NULL
               OR v_audit_xid IS DISTINCT FROM pg_current_xact_id()::text::xid
               OR pg_xact_status(v_audit_xid::text::xid8)
                    IS DISTINCT FROM 'in progress'
               OR v_action IS DISTINCT FROM 'evidence.object.create'
               OR v_payload IS DISTINCT FROM jsonb_build_object(
                   'evidence_ref', NEW.evidence_ref::text,
                   'entity_id', NEW.entity_id::text,
                   'business_unit_id', NEW.business_unit_id::text
               ) THEN
                RAISE EXCEPTION 'evidence audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.evidence_object AS e
                 WHERE e.evidence_ref = NEW.evidence_ref
                   AND e.xmin IS DISTINCT FROM v_audit_xid
            ) THEN
                RAISE EXCEPTION 'evidence row and audit must share one transaction'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.source_record_id IS NULL THEN
                IF NEW.raw_artifact_id IS NOT NULL THEN
                    RAISE EXCEPTION 'evidence raw artifact requires a source record'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END IF;
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
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
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
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
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
            v_blob_xid xid;
            v_cycle boolean;
        BEGIN
            SELECT e.xmin, e.action, e.payload
              INTO v_audit_xid, v_action, v_payload
              FROM public.audit_event AS e WHERE e.id = NEW.audit_event_id;
            SELECT b.xmin INTO v_blob_xid
              FROM public.encrypted_blob_version AS b
             WHERE b.blob_ref = NEW.blob_ref;
            IF v_action IS NULL THEN
                RAISE EXCEPTION 'blob audit evidence does not exist'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            v_mode := v_payload ->> 'rotation_mode';
            IF v_blob_xid IS DISTINCT FROM v_audit_xid
               OR v_audit_xid IS NULL
               OR v_audit_xid IS DISTINCT FROM pg_current_xact_id()::text::xid
               OR pg_xact_status(v_audit_xid::text::xid8) IS DISTINCT FROM 'in progress'
               OR v_action IS DISTINCT FROM 'evidence.blob.version'
               OR jsonb_typeof(v_payload -> 'rotation_mode') IS DISTINCT FROM 'string'
               OR (v_mode IS DISTINCT FROM 'GENESIS'
                   AND v_mode IS DISTINCT FROM 'REWRAP'
                   AND v_mode IS DISTINCT FROM 'REENCRYPT')
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
                IF v_mode = 'REWRAP' AND EXISTS (
                    SELECT 1
                      FROM public.encrypted_blob_version AS parent
                     WHERE parent.blob_ref = NEW.predecessor_blob_ref
                       AND (
                           NEW.evidence_ref IS DISTINCT FROM parent.evidence_ref
                           OR NEW.object_ref IS DISTINCT FROM parent.object_ref
                           OR NEW.envelope_schema IS DISTINCT FROM parent.envelope_schema
                           OR NEW.algorithm IS DISTINCT FROM parent.algorithm
                           OR NEW.chunk_size IS DISTINCT FROM parent.chunk_size
                           OR NEW.stream_header IS DISTINCT FROM parent.stream_header
                           OR NEW.purpose IS DISTINCT FROM parent.purpose
                           OR NOT (NEW.wrapped_key_generation IS DISTINCT FROM parent.wrapped_key_generation)
                           OR NOT (NEW.wrapped_key_nonce IS DISTINCT FROM parent.wrapped_key_nonce)
                           OR NOT (NEW.wrapped_key_ciphertext IS DISTINCT FROM parent.wrapped_key_ciphertext)
                           OR NOT (NEW.ciphertext_sha256 IS DISTINCT FROM parent.ciphertext_sha256)
                           OR NOT (NEW.ciphertext_size IS DISTINCT FROM parent.ciphertext_size)
                           OR NOT (NEW.storage_key IS DISTINCT FROM parent.storage_key)
                       )
                ) THEN
                    RAISE EXCEPTION 'REWRAP must preserve fixed fields and change wrapped envelope fields'
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
             SELECT EXISTS (
                 SELECT 1 FROM ancestors WHERE blob_ref = NEW.blob_ref
             ) INTO v_cycle;
             IF v_cycle THEN
                RAISE EXCEPTION 'encrypted blob predecessor chain contains a cycle'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.predecessor_blob_ref IS NULL THEN
                 IF v_mode IS DISTINCT FROM 'GENESIS' THEN
                    RAISE EXCEPTION 'blob genesis must use GENESIS mode'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                SELECT xmin INTO v_identity_xid
                  FROM public.encrypted_object_identity
                 WHERE object_ref = NEW.object_ref
                    AND evidence_ref = NEW.evidence_ref;
                 IF v_identity_xid IS NULL
                    OR v_identity_xid IS DISTINCT FROM pg_current_xact_id()::text::xid
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
                        OR v_identity_xid IS DISTINCT FROM pg_current_xact_id()::text::xid
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

        CREATE FUNCTION public.r1_validate_candidate_audited_child()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_event_ref uuid;
            v_audit_xid xid;
            v_child_xid xid;
        BEGIN
            IF TG_TABLE_NAME IN ('candidate_field_change', 'candidate_conflict_resolution') THEN
                v_event_ref := (to_jsonb(NEW) ->> 'event_ref')::uuid;
                IF TG_TABLE_NAME = 'candidate_field_change' THEN
                    SELECT xmin INTO v_child_xid
                      FROM public.candidate_field_change
                     WHERE event_ref = v_event_ref
                       AND field = (to_jsonb(NEW) ->> 'field');
                ELSE
                    SELECT xmin INTO v_child_xid
                      FROM public.candidate_conflict_resolution
                     WHERE event_ref = v_event_ref
                       AND conflict_ref = (to_jsonb(NEW) ->> 'conflict_ref')::uuid;
                END IF;
            ELSE
                SELECT ce.event_ref INTO v_event_ref
                 FROM public.candidate_event AS ce
                 WHERE ce.candidate_id = (to_jsonb(NEW) ->> 'candidate_id')::uuid
                   AND ce.to_revision = (to_jsonb(NEW) ->> 'revision')::integer;
                SELECT xmin INTO v_child_xid
                  FROM public.candidate_blocker
                 WHERE candidate_id = (to_jsonb(NEW) ->> 'candidate_id')::uuid
                   AND revision = (to_jsonb(NEW) ->> 'revision')::integer
                   AND ordinal = (to_jsonb(NEW) ->> 'ordinal')::integer;
            END IF;
            SELECT a.xmin INTO v_audit_xid
              FROM public.candidate_event AS ce
              JOIN public.audit_event AS a ON a.id = ce.audit_event_id
             WHERE ce.event_ref = v_event_ref;
            IF v_child_xid IS DISTINCT FROM v_audit_xid
               OR v_audit_xid IS DISTINCT FROM pg_current_xact_id()::text::xid
               OR pg_xact_status(v_audit_xid::text::xid8)
                    IS DISTINCT FROM 'in progress' THEN
                RAISE EXCEPTION 'candidate audited child must share its parent event transaction'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_candidate_field_change_audit_xmin
        AFTER INSERT ON public.candidate_field_change
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_audited_child();
        CREATE CONSTRAINT TRIGGER r1_candidate_conflict_resolution_audit_xmin
        AFTER INSERT ON public.candidate_conflict_resolution
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_audited_child();
        CREATE CONSTRAINT TRIGGER r1_candidate_blocker_audit_xmin
        AFTER INSERT ON public.candidate_blocker
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_audited_child();

        CREATE FUNCTION public.r1_validate_candidate_event_audit()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
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
            IF EXISTS (
                SELECT 1
                  FROM public.candidate_event AS event_row
                 WHERE event_row.event_ref = NEW.event_ref
                   AND event_row.xmin IS DISTINCT FROM v_audit_xid
            )
               OR v_audit_xid IS NULL
               OR v_audit_xid IS DISTINCT FROM pg_current_xact_id()::text::xid
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
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        BEGIN
            PERFORM public.r1_check_candidate_closure(NEW.candidate_id, NEW.event_ref);
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_candidate_history
        AFTER INSERT ON public.candidate_event
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_event_history();

        CREATE FUNCTION public.r1_validate_candidate_revision_history()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        BEGIN
            PERFORM public.r1_check_candidate_closure(NEW.candidate_id, NULL);
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_candidate_revision_history
        AFTER INSERT ON public.candidate_revision
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_revision_history();

        CREATE FUNCTION public.r1_validate_candidate_closure_trigger()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_candidate_id uuid;
            v_focus_event_ref uuid;
        BEGIN
            IF TG_TABLE_NAME = 'candidate' THEN
                v_candidate_id := (to_jsonb(NEW) ->> 'id')::uuid;
                SELECT ce.event_ref INTO v_focus_event_ref
                  FROM public.candidate_event AS ce
                 WHERE ce.candidate_id = v_candidate_id
                   AND ce.event_type = 'CREATE'
                   AND ce.to_revision = 1;
            ELSE
                v_candidate_id := (to_jsonb(NEW) ->> 'candidate_id')::uuid;
            END IF;
            IF TG_TABLE_NAME IN ('candidate_field_change', 'candidate_conflict_resolution') THEN
                SELECT ce.candidate_id INTO v_candidate_id
                  FROM public.candidate_event AS ce
                 WHERE ce.event_ref = (to_jsonb(NEW) ->> 'event_ref')::uuid;
                v_focus_event_ref := (to_jsonb(NEW) ->> 'event_ref')::uuid;
            ELSIF TG_TABLE_NAME IN ('candidate_source', 'candidate_evidence') THEN
                SELECT ce.event_ref INTO v_focus_event_ref
                  FROM public.candidate_event AS ce
                 WHERE ce.candidate_id = v_candidate_id
                   AND ce.event_type = 'CREATE'
                   AND ce.to_revision = 1;
            ELSIF TG_TABLE_NAME IN ('candidate_revision', 'candidate_blocker') THEN
                SELECT ce.event_ref INTO v_focus_event_ref
                 FROM public.candidate_event AS ce
                 WHERE ce.candidate_id = v_candidate_id
                   AND ce.to_revision = (to_jsonb(NEW) ->> 'revision')::integer;
            END IF;
            PERFORM public.r1_check_candidate_closure(v_candidate_id, v_focus_event_ref);
            RETURN NEW;
        END
         $function$;
         CREATE CONSTRAINT TRIGGER r1_candidate_closure
         AFTER INSERT ON public.candidate
         DEFERRABLE INITIALLY DEFERRED
         FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_closure_trigger();
         CREATE CONSTRAINT TRIGGER r1_candidate_source_closure
        AFTER INSERT ON public.candidate_source
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_closure_trigger();
        CREATE CONSTRAINT TRIGGER r1_candidate_revision_closure
        AFTER INSERT ON public.candidate_revision
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_closure_trigger();
        CREATE CONSTRAINT TRIGGER r1_candidate_evidence_closure
        AFTER INSERT ON public.candidate_evidence
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_closure_trigger();
        CREATE CONSTRAINT TRIGGER r1_candidate_blocker_closure
        AFTER INSERT ON public.candidate_blocker
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_closure_trigger();
        CREATE CONSTRAINT TRIGGER r1_candidate_field_change_closure
        AFTER INSERT ON public.candidate_field_change
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_closure_trigger();
        CREATE CONSTRAINT TRIGGER r1_candidate_conflict_resolution_closure
        AFTER INSERT ON public.candidate_conflict_resolution
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_closure_trigger();

        CREATE FUNCTION public.r1_validate_snapshot_blocker_scope()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
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
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
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
            IF v_count IS DISTINCT FROM 1
               OR v_entity_scope_count IS DISTINCT FROM 1
               OR v_business_unit_scope_count IS DISTINCT FROM 1
               OR v_month_scope_count IS DISTINCT FROM 1
               OR EXISTS (
                   SELECT 1 FROM public.reconciliation_leg
                    WHERE reconciliation_group_id = v_group
                     AND (posting_id IS NULL OR business_unit_id IS NULL OR accounting_month IS NULL)
               ) THEN
                RAISE EXCEPTION 'reconciliation group requires exactly one scoped primary leg'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.reconciliation_leg AS l
                  LEFT JOIN public.posting AS p ON p.id = l.posting_id
                  LEFT JOIN public.journal_entry AS je ON je.id = p.entry_id
                  LEFT JOIN public.journal_entry_attribution AS ja
                    ON ja.entry_id = je.id
                 WHERE l.reconciliation_group_id = v_group
                   AND (p.id IS NULL OR je.id IS NULL OR ja.entry_id IS NULL
                        OR l.posting_id IS NULL
                        OR (l.is_primary IS TRUE
                            AND p.account_id IS DISTINCT FROM je.primary_account_id)
                         OR p.entry_id IS DISTINCT FROM je.id
                         OR je.entity_id IS DISTINCT FROM l.entity_id
                         OR ja.entity_id IS DISTINCT FROM l.entity_id
                         OR ja.business_unit_id IS DISTINCT FROM l.business_unit_id
                        OR ja.accounting_month IS DISTINCT FROM l.accounting_month)
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
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
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
             IF EXISTS (
                 SELECT 1
                   FROM public.posting AS p
                   JOIN public.posting_attribution AS pa ON pa.posting_id = p.id
                   JOIN public.reporting_category AS rc
                     ON rc.id = pa.reporting_category_id
                   JOIN public.journal_entry AS je ON je.id = p.entry_id
                  WHERE p.entry_id = v_entry
                    AND (rc.entity_id IS DISTINCT FROM je.entity_id
                         OR pa.category_code_snapshot IS DISTINCT FROM rc.code
                         OR pa.category_label_snapshot IS DISTINCT FROM rc.label)
             ) THEN
                 RAISE EXCEPTION 'POSTED journal entry has contradictory posting category scope'
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
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE v_entry uuid;
        BEGIN
            v_entry := CASE WHEN TG_OP = 'DELETE' THEN OLD.entry_id ELSE NEW.entry_id END;
            IF EXISTS (
                SELECT 1 FROM public.journal_entry WHERE id = v_entry AND status = 'POSTED'
            ) THEN
                IF (SELECT count(*) FROM public.journal_entry_attribution WHERE entry_id = v_entry)
                       IS DISTINCT FROM 1
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
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE v_entry uuid; v_posting uuid;
        BEGIN
            v_posting := CASE WHEN TG_OP = 'DELETE' THEN OLD.posting_id ELSE NEW.posting_id END;
            SELECT entry_id INTO v_entry FROM public.posting WHERE id = v_posting;
            IF EXISTS (
                SELECT 1 FROM public.journal_entry WHERE id = v_entry AND status = 'POSTED'
            ) AND (SELECT count(*) FROM public.posting_attribution WHERE posting_id = v_posting)
                    IS DISTINCT FROM 1 THEN
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

        CREATE FUNCTION public.r1_validate_posted_primary_account()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_entry_id uuid;
            v_status public.journal_status;
            v_primary_account uuid;
            v_match_count bigint;
        BEGIN
            IF TG_TABLE_NAME = 'journal_entry' THEN
                v_entry_id := NEW.id;
            ELSIF TG_OP = 'DELETE' THEN
                v_entry_id := OLD.entry_id;
            ELSE
                v_entry_id := NEW.entry_id;
            END IF;
            SELECT je.status, je.primary_account_id
              INTO v_status, v_primary_account
              FROM public.journal_entry AS je
             WHERE je.id = v_entry_id;
             IF v_status = 'POSTED' THEN
                 SELECT count(*) INTO v_match_count
                   FROM public.posting AS p
                  WHERE p.entry_id = v_entry_id
                    AND p.account_id IS NOT DISTINCT FROM v_primary_account;
                 IF v_primary_account IS NULL OR v_match_count IS DISTINCT FROM 1 THEN
                     RAISE EXCEPTION 'POSTED journal entry requires one matching primary account posting'
                         USING ERRCODE = 'integrity_constraint_violation';
                 END IF;
             END IF;
             IF TG_OP = 'UPDATE' AND OLD.entry_id IS DISTINCT FROM NEW.entry_id THEN
                 SELECT je.status, je.primary_account_id
                   INTO v_status, v_primary_account
                   FROM public.journal_entry AS je
                  WHERE je.id = OLD.entry_id;
                 IF v_status = 'POSTED' THEN
                     SELECT count(*) INTO v_match_count
                       FROM public.posting AS p
                      WHERE p.entry_id = OLD.entry_id
                        AND p.account_id IS NOT DISTINCT FROM v_primary_account;
                     IF v_primary_account IS NULL OR v_match_count IS DISTINCT FROM 1 THEN
                         RAISE EXCEPTION
                             'POSTED journal entry requires one matching primary account posting'
                             USING ERRCODE = 'integrity_constraint_violation';
                     END IF;
                 END IF;
             END IF;
             IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_posted_primary_account_entry
        AFTER INSERT OR UPDATE OF status, primary_account_id ON public.journal_entry
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_posted_primary_account();
        CREATE CONSTRAINT TRIGGER r1_posted_primary_account_posting
        AFTER INSERT OR UPDATE OF entry_id, account_id OR DELETE ON public.posting
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_posted_primary_account();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.r1_validate_snapshot_audit()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_xid xid;
            v_snapshot_xid xid;
            v_action text;
            v_payload jsonb;
            v_expected jsonb;
            v_min_revision integer;
            v_max_revision integer;
            v_revision_count bigint;
        BEGIN
            SELECT a.xmin, a.action, a.payload INTO v_xid, v_action, v_payload
              FROM public.audit_event AS a WHERE a.id = NEW.audit_event_id;
            SELECT s.xmin INTO v_snapshot_xid
              FROM public.reconciliation_snapshot AS s
             WHERE s.snapshot_ref = NEW.snapshot_ref;
            PERFORM pg_advisory_xact_lock(hashtext('ledgerbridge.audit_event'));
            PERFORM pg_advisory_xact_lock(
                hashtext('ledgerbridge.reconciliation.snapshot'),
                hashtext(NEW.entity_id::text || ':' || NEW.business_unit_id::text || ':'
                         || NEW.accounting_month::text)
            );
            SELECT min(s.snapshot_revision), max(s.snapshot_revision), count(*)
              INTO v_min_revision, v_max_revision, v_revision_count
              FROM public.reconciliation_snapshot AS s
              WHERE s.entity_id = NEW.entity_id
                AND s.business_unit_id = NEW.business_unit_id
                AND s.accounting_month = NEW.accounting_month;
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
               OR v_xid IS DISTINCT FROM pg_current_xact_id()::text::xid
               OR v_snapshot_xid IS DISTINCT FROM v_xid
               OR pg_xact_status(v_xid::text::xid8) IS DISTINCT FROM 'in progress'
               OR v_action IS DISTINCT FROM 'reconciliation.snapshot'
               OR v_payload IS DISTINCT FROM v_expected THEN
                RAISE EXCEPTION 'snapshot audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF v_min_revision IS DISTINCT FROM 1
               OR v_max_revision IS DISTINCT FROM v_revision_count::integer THEN
                RAISE EXCEPTION 'reconciliation snapshot revisions must be contiguous per scope'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                  FROM public.audit_event AS watermark
                 WHERE watermark.sequence = NEW.ledger_audit_sequence
                   AND watermark.hash = NEW.ledger_audit_hash
                   AND octet_length(watermark.hash) = 32
                   AND watermark.sequence < (
                       SELECT snapshot_audit.sequence
                         FROM public.audit_event AS snapshot_audit
                        WHERE snapshot_audit.id = NEW.audit_event_id
                   )
             ) THEN
                RAISE EXCEPTION 'snapshot watermark is not a real audit sequence and hash'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_snapshot_audit_binding
        AFTER INSERT ON public.reconciliation_snapshot
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_snapshot_audit();

        CREATE FUNCTION public.r1_validate_snapshot_audited_child()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_snapshot_ref uuid;
            v_snapshot_xid xid;
            v_audit_xid xid;
            v_child_xid xid;
        BEGIN
            v_snapshot_ref := (to_jsonb(NEW) ->> 'snapshot_ref')::uuid;
            IF TG_TABLE_NAME = 'reconciliation_snapshot_blocker' THEN
                SELECT xmin INTO v_child_xid
                  FROM public.reconciliation_snapshot_blocker
                 WHERE snapshot_ref = v_snapshot_ref
                   AND ordinal = (to_jsonb(NEW) ->> 'ordinal')::integer;
            ELSIF TG_TABLE_NAME = 'reconciliation_snapshot_proposal' THEN
                SELECT xmin INTO v_child_xid
                  FROM public.reconciliation_snapshot_proposal
                 WHERE snapshot_ref = v_snapshot_ref
                   AND proposal_ref = (to_jsonb(NEW) ->> 'proposal_ref')::uuid;
            ELSE
                SELECT xmin INTO v_child_xid
                  FROM public.reconciliation_snapshot_suspense
                 WHERE snapshot_ref = v_snapshot_ref
                   AND suspense_ref = (to_jsonb(NEW) ->> 'suspense_ref')::uuid;
            END IF;
            SELECT s.xmin, a.xmin
              INTO v_snapshot_xid, v_audit_xid
              FROM public.reconciliation_snapshot AS s
              JOIN public.audit_event AS a ON a.id = s.audit_event_id
             WHERE s.snapshot_ref = v_snapshot_ref;
            IF v_child_xid IS DISTINCT FROM v_snapshot_xid
               OR v_snapshot_xid IS DISTINCT FROM pg_current_xact_id()::text::xid
               OR v_audit_xid IS DISTINCT FROM v_snapshot_xid
               OR pg_xact_status(v_audit_xid::text::xid8)
                    IS DISTINCT FROM 'in progress' THEN
                RAISE EXCEPTION 'snapshot child must share its parent audit transaction'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_snapshot_blocker_audit_xmin
        AFTER INSERT ON public.reconciliation_snapshot_blocker
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_snapshot_audited_child();
        CREATE CONSTRAINT TRIGGER r1_snapshot_proposal_audit_xmin
        AFTER INSERT ON public.reconciliation_snapshot_proposal
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_snapshot_audited_child();
        CREATE CONSTRAINT TRIGGER r1_snapshot_suspense_audit_xmin
        AFTER INSERT ON public.reconciliation_snapshot_suspense
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_snapshot_audited_child();
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
        DO $downgrade_fact_acl$
        DECLARE role_name text;
        BEGIN
            REVOKE ALL ON TABLE
                public.encrypted_object_identity, public.reconciliation_snapshot_blocker,
                public.reconciliation_leg
            FROM PUBLIC;
            FOREACH role_name IN ARRAY ARRAY[
                'ledgerbridge_reader', 'ledgerbridge_api',
                'ledgerbridge_worker', 'ledgerbridge_app'
            ] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format(
                        'REVOKE ALL ON TABLE '
                        'public.encrypted_object_identity, '
                        'public.reconciliation_snapshot_blocker, '
                        'public.reconciliation_leg FROM %I', role_name
                    );
                END IF;
            END LOOP;
        END
        $downgrade_fact_acl$;
        DROP TRIGGER IF EXISTS r1_snapshot_audit_binding ON public.reconciliation_snapshot;
        DROP FUNCTION IF EXISTS public.r1_validate_snapshot_audit();
        DROP TRIGGER IF EXISTS r1_snapshot_blocker_audit_xmin
            ON public.reconciliation_snapshot_blocker;
        DROP TRIGGER IF EXISTS r1_snapshot_proposal_audit_xmin
            ON public.reconciliation_snapshot_proposal;
        DROP TRIGGER IF EXISTS r1_snapshot_suspense_audit_xmin
            ON public.reconciliation_snapshot_suspense;
        DROP FUNCTION IF EXISTS public.r1_validate_snapshot_audited_child();
        DROP TRIGGER IF EXISTS r1_posted_posting_attribution_complete ON public.posting_attribution;
        DROP TRIGGER IF EXISTS r1_posted_entry_attribution_complete ON public.journal_entry_attribution;
        DROP TRIGGER IF EXISTS r1_posted_entry_complete ON public.journal_entry;
        DROP TRIGGER IF EXISTS r1_posted_primary_account_entry ON public.journal_entry;
        DROP TRIGGER IF EXISTS r1_posted_primary_account_posting ON public.posting;
        DROP FUNCTION IF EXISTS public.r1_validate_posted_primary_account();
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
         DROP TRIGGER IF EXISTS r1_candidate_closure ON public.candidate;
         DROP TRIGGER IF EXISTS r1_candidate_source_closure ON public.candidate_source;
        DROP TRIGGER IF EXISTS r1_candidate_revision_closure ON public.candidate_revision;
        DROP TRIGGER IF EXISTS r1_candidate_evidence_closure ON public.candidate_evidence;
        DROP TRIGGER IF EXISTS r1_candidate_blocker_closure ON public.candidate_blocker;
        DROP TRIGGER IF EXISTS r1_candidate_field_change_closure ON public.candidate_field_change;
        DROP TRIGGER IF EXISTS r1_candidate_conflict_resolution_closure
            ON public.candidate_conflict_resolution;
        DROP FUNCTION IF EXISTS public.r1_validate_candidate_closure_trigger();
        DROP TRIGGER IF EXISTS r1_candidate_field_change_audit_xmin
            ON public.candidate_field_change;
        DROP TRIGGER IF EXISTS r1_candidate_conflict_resolution_audit_xmin
            ON public.candidate_conflict_resolution;
        DROP TRIGGER IF EXISTS r1_candidate_blocker_audit_xmin ON public.candidate_blocker;
        DROP FUNCTION IF EXISTS public.r1_validate_candidate_audited_child();
        DROP FUNCTION IF EXISTS public.r1_check_candidate_closure(uuid, uuid);
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
    op.alter_column("reconciliation_leg", "posting_id", nullable=True)
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
    op.alter_column(
        "candidate",
        "contract_version",
        existing_type=sa.String(32),
        type_=sa.String(24),
        schema="public",
    )
