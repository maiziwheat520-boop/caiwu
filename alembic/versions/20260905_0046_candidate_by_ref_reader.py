"""candidate by-reference reader

Revision ID: 20260905_0046
Revises: 20260904_0045
Create Date: 2026-09-05

"""

from __future__ import annotations

from alembic import op

revision = "20260905_0046"
down_revision = "20260904_0045"
branch_labels = None
depends_on = None

_UPGRADE_SQL = r"""
-- Resolving one candidate by reference used to page through the whole
-- collection: get_candidate walked list_candidates_as_of until the reference
-- appeared, measuring 115 ms for a first-page candidate and 1,551 ms for a
-- last-page one against 3,441 production candidates, growing with the ledger.
--
-- The projection is not duplicated. The existing base reader gains an optional
-- single-candidate filter, and the eight-argument signature becomes a thin
-- delegation so every existing caller keeps its exact behaviour.

CREATE FUNCTION internal_read.list_candidates_base_as_of(
    p_entity_id uuid,
    p_business_unit_id uuid,
    p_status varchar(16),
    p_audit_horizon_sequence bigint,
    p_audit_horizon_hash bytea,
    p_last_created_at timestamptz,
    p_last_candidate_id uuid,
    p_limit integer,
    p_candidate_ref uuid
) RETURNS TABLE (
    contract_version varchar(32), candidate_ref uuid, short_id varchar(10),
    revision integer, status varchar(16), entity_ref uuid,
    business_unit_ref varchar(100), business_unit_label varchar(200),
    category_code varchar(100), category_label varchar(200), amount_minor bigint,
    currency varchar(3), accounting_month varchar(7), summary varchar(500),
    confidence_basis_points smallint, source jsonb, evidence jsonb,
    blockers jsonb, review_summary jsonb, created_at timestamptz,
    updated_at timestamptz, supersedes_candidate_ref uuid,
    superseded_by_candidate_ref uuid
) LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $function$
        BEGIN
            IF p_entity_id IS NULL OR p_audit_horizon_sequence IS NULL
               OR p_audit_horizon_sequence <= 0 OR p_audit_horizon_hash IS NULL
               OR octet_length(p_audit_horizon_hash) <> 32
               OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
               OR (p_last_created_at IS NULL) <> (p_last_candidate_id IS NULL) THEN
                RAISE EXCEPTION 'invalid candidate read parameters' USING ERRCODE = '22023';
            END IF;
            IF p_status IS NOT NULL AND p_status NOT IN
               ('INCOMPLETE','CONFLICTED','PENDING','CONFIRMED','IGNORED','SUPERSEDED') THEN
                RAISE EXCEPTION 'invalid candidate status' USING ERRCODE = '22023';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.audit_event AS horizon
                 WHERE horizon.sequence = p_audit_horizon_sequence
                   AND horizon.hash = p_audit_horizon_hash
                   AND octet_length(horizon.hash) = 32
            ) THEN
                RAISE EXCEPTION 'audit horizon is not an exact chain row'
                    USING ERRCODE = '22023';
            END IF;
            IF p_business_unit_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.business_unit AS bu
                 WHERE bu.id = p_business_unit_id AND bu.entity_id = p_entity_id
            ) THEN
                RAISE EXCEPTION 'business unit does not belong to entity'
                    USING ERRCODE = '22023';
            END IF;
            IF p_last_created_at IS NOT NULL AND NOT EXISTS (
                SELECT 1
                  FROM public.candidate AS cursor_candidate
                  JOIN LATERAL (
                      SELECT cr.revision, cr.status, cr.business_unit_id
                        FROM public.candidate_revision AS cr
                        JOIN public.candidate_event AS ce
                         ON ce.candidate_id = cr.candidate_id
                         AND ce.to_revision = cr.revision
                         AND ce.to_status = cr.status
                        JOIN public.audit_event AS ae ON ae.id = ce.audit_event_id
                       WHERE cr.candidate_id = cursor_candidate.id
                         AND ae.sequence <= p_audit_horizon_sequence
                         AND EXISTS (
                             SELECT 1
                               FROM public.candidate_revision AS cr0
                               JOIN public.candidate_event AS ce0
                                 ON ce0.candidate_id = cr0.candidate_id
                                AND ce0.to_revision = cr0.revision
                                AND ce0.to_status = cr0.status
                               JOIN public.audit_event AS ae0 ON ae0.id = ce0.audit_event_id
                              WHERE cr0.candidate_id = cursor_candidate.id
                                AND cr0.revision = 1
                                AND ce0.event_type = 'CREATE'
                                AND ae0.sequence <= p_audit_horizon_sequence
                         )
                       ORDER BY cr.revision DESC LIMIT 1
                  ) AS cursor_tip ON TRUE
                 WHERE cursor_candidate.id = p_last_candidate_id
                   AND cursor_candidate.entity_id = p_entity_id
                   AND cursor_candidate.created_at = p_last_created_at
                   AND ((p_business_unit_id IS NULL AND cursor_tip.business_unit_id IS NULL)
                        OR cursor_tip.business_unit_id = p_business_unit_id)
                   AND (p_status IS NULL OR cursor_tip.status = p_status)
            ) THEN
                RAISE EXCEPTION 'candidate cursor is outside requested scope'
                    USING ERRCODE = '22023';
            END IF;
            RETURN QUERY
            SELECT c.contract_version, c.id, c.short_id, r.revision, r.status,
                   c.entity_id, r.business_unit_ref_snapshot,
                   r.business_unit_label_snapshot, r.category_code_snapshot,
                   r.category_label_snapshot, r.amount_minor, r.currency,
                   to_char(r.accounting_month, 'YYYY-MM')::varchar(7), r.summary,
                   r.confidence_basis_points,
                   jsonb_build_object(
                       'ingest_channel', cs.ingest_channel_id,
                       'source_system', cs.source_system_id,
                       'source_event_ref', cs.source_event_ref,
                       'display_label', cs.display_label
                   ),
                   CASE WHEN r.business_unit_id IS NULL THEN '[]'::jsonb ELSE coalesce((
                       SELECT jsonb_agg(jsonb_build_object(
                           'evidence_ref', ce.evidence_ref,
                           'kind', ce.kind,
                           'media_type', ce.media_type_snapshot,
                           'display_name', ce.display_name_snapshot,
                           'download_available', ce.download_available
                       ) ORDER BY ce.ordinal)
                       FROM public.candidate_evidence AS ce
                       WHERE ce.candidate_id = c.id
                         AND ce.evidence_entity_id = c.entity_id
                         AND ce.evidence_business_unit_id = r.business_unit_id
                   ), '[]'::jsonb) END,
                   coalesce((
                       SELECT jsonb_agg(jsonb_build_object(
                           'code', b.code, 'message', b.message, 'field', b.field,
                           'conflict_ref', b.conflict_ref, 'evidence_ref', b.evidence_ref
                       ) ORDER BY b.ordinal)
                       FROM public.candidate_blocker AS b
                       WHERE b.candidate_id = c.id AND b.revision = r.revision
                   ), '[]'::jsonb),
                   jsonb_build_object(
                       'event_count', greatest(r.revision - 1, 0),
                       'last_action', (
                           SELECT ce2.action FROM public.candidate_event AS ce2
                            WHERE ce2.candidate_id = c.id
                              AND ce2.to_revision = r.revision
                              AND ce2.event_type <> 'CREATE'
                       ),
                       'last_decided_at', (
                           SELECT ce3.occurred_at FROM public.candidate_event AS ce3
                            WHERE ce3.candidate_id = c.id
                              AND ce3.to_revision = r.revision
                              AND ce3.event_type <> 'CREATE'
                       ),
                       'current_revision', r.revision
                   ),
                   c.created_at, r.updated_at, c.supersedes_candidate_id,
                   CASE WHEN r.status = 'SUPERSEDED' THEN (
                       SELECT successor.id
                         FROM public.candidate AS successor
                        WHERE successor.supersedes_candidate_id = c.id
                          AND successor.entity_id = c.entity_id
                          AND EXISTS (
                              SELECT 1
                                FROM public.candidate_revision AS sr
                                JOIN public.candidate_event AS se
                                  ON se.candidate_id = sr.candidate_id
                                 AND se.to_revision = sr.revision
                                 AND se.to_status = sr.status
                                JOIN public.audit_event AS sae ON sae.id = se.audit_event_id
                               WHERE sr.candidate_id = successor.id
                                 AND sr.revision = 1
                                 AND se.event_type = 'CREATE'
                                 AND sae.sequence <= p_audit_horizon_sequence
                          )
                        LIMIT 1
                   ) END
              FROM public.candidate AS c
              JOIN public.candidate_source AS cs ON cs.candidate_id = c.id
              JOIN LATERAL (
                  SELECT cr.revision, cr.status, cr.business_unit_id,
                         cr.business_unit_ref_snapshot, cr.business_unit_label_snapshot,
                         cr.category_code_snapshot, cr.category_label_snapshot,
                         cr.amount_minor, cr.currency, cr.accounting_month, cr.summary,
                         cr.confidence_basis_points, cr.created_at, cr.updated_at
                    FROM public.candidate_revision AS cr
                    JOIN public.candidate_event AS ev
                      ON ev.candidate_id = cr.candidate_id
                     AND ev.to_revision = cr.revision
                     AND ev.to_status = cr.status
                    JOIN public.audit_event AS ae ON ae.id = ev.audit_event_id
                   WHERE cr.candidate_id = c.id
                     AND ae.sequence <= p_audit_horizon_sequence
                     AND EXISTS (
                         SELECT 1
                           FROM public.candidate_revision AS cr0
                           JOIN public.candidate_event AS ev0
                             ON ev0.candidate_id = cr0.candidate_id
                            AND ev0.to_revision = cr0.revision
                            AND ev0.to_status = cr0.status
                           JOIN public.audit_event AS ae0 ON ae0.id = ev0.audit_event_id
                          WHERE cr0.candidate_id = c.id
                            AND cr0.revision = 1
                            AND ev0.event_type = 'CREATE'
                            AND ae0.sequence <= p_audit_horizon_sequence
                     )
                   ORDER BY cr.revision DESC
                   LIMIT 1
              ) AS r ON TRUE
             WHERE c.entity_id = p_entity_id
               AND (p_candidate_ref IS NULL OR c.id = p_candidate_ref)
               AND ((p_business_unit_id IS NULL AND r.business_unit_id IS NULL)
                    OR r.business_unit_id = p_business_unit_id)
               AND (p_status IS NULL OR r.status = p_status)
               AND (p_last_created_at IS NULL
                    OR (c.created_at, c.id) > (p_last_created_at, p_last_candidate_id))
             ORDER BY c.created_at, c.id
             LIMIT (p_limit + 1);
        END
        $function$;

CREATE OR REPLACE FUNCTION internal_read.list_candidates_base_as_of(
    p_entity_id uuid,
    p_business_unit_id uuid,
    p_status varchar(16),
    p_audit_horizon_sequence bigint,
    p_audit_horizon_hash bytea,
    p_last_created_at timestamptz,
    p_last_candidate_id uuid,
    p_limit integer
) RETURNS TABLE (
    contract_version varchar(32), candidate_ref uuid, short_id varchar(10),
    revision integer, status varchar(16), entity_ref uuid,
    business_unit_ref varchar(100), business_unit_label varchar(200),
    category_code varchar(100), category_label varchar(200), amount_minor bigint,
    currency varchar(3), accounting_month varchar(7), summary varchar(500),
    confidence_basis_points smallint, source jsonb, evidence jsonb,
    blockers jsonb, review_summary jsonb, created_at timestamptz,
    updated_at timestamptz, supersedes_candidate_ref uuid,
    superseded_by_candidate_ref uuid
) LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $function$
BEGIN
    RETURN QUERY
    SELECT * FROM internal_read.list_candidates_base_as_of(
        p_entity_id, p_business_unit_id, p_status,
        p_audit_horizon_sequence, p_audit_horizon_hash,
        p_last_created_at, p_last_candidate_id, p_limit, NULL::uuid
    );
END
$function$;

-- Mirrors list_candidates_as_of: base rows plus projected evidence unlocks.
CREATE FUNCTION internal_read.get_candidate_as_of(
    p_entity_id uuid,
    p_business_unit_id uuid,
    p_candidate_ref uuid,
    p_audit_horizon_sequence bigint,
    p_audit_horizon_hash bytea
) RETURNS TABLE (
    contract_version varchar(32), candidate_ref uuid, short_id varchar(10),
    revision integer, status varchar(16), entity_ref uuid,
    business_unit_ref varchar(100), business_unit_label varchar(200),
    category_code varchar(100), category_label varchar(200), amount_minor bigint,
    currency varchar(3), accounting_month varchar(7), summary varchar(500),
    confidence_basis_points smallint, source jsonb, evidence jsonb,
    blockers jsonb, review_summary jsonb, created_at timestamptz,
    updated_at timestamptz, supersedes_candidate_ref uuid,
    superseded_by_candidate_ref uuid
) LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $function$
BEGIN
    IF p_candidate_ref IS NULL THEN
        RAISE EXCEPTION 'invalid candidate read parameters' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT base.contract_version, base.candidate_ref, base.short_id, base.revision,
           base.status, base.entity_ref, base.business_unit_ref,
           base.business_unit_label, base.category_code, base.category_label,
           base.amount_minor, base.currency, base.accounting_month, base.summary,
           base.confidence_basis_points, base.source,
           internal_read.project_evidence_unlocks(
               base.evidence, p_audit_horizon_sequence
           ),
           base.blockers, base.review_summary, base.created_at, base.updated_at,
           base.supersedes_candidate_ref, base.superseded_by_candidate_ref
      FROM internal_read.list_candidates_base_as_of(
          p_entity_id, p_business_unit_id, NULL::varchar(16),
          p_audit_horizon_sequence, p_audit_horizon_hash,
          NULL::timestamptz, NULL::uuid, 1, p_candidate_ref
      ) AS base;
END
$function$;

GRANT EXECUTE ON FUNCTION internal_read.get_candidate_as_of(
    uuid, uuid, uuid, bigint, bytea
) TO ledgerbridge_reader;
"""

_DOWNGRADE_SQL = r"""
REVOKE ALL ON FUNCTION internal_read.get_candidate_as_of(
    uuid, uuid, uuid, bigint, bytea
) FROM ledgerbridge_reader;
DROP FUNCTION internal_read.get_candidate_as_of(uuid, uuid, uuid, bigint, bytea);

CREATE OR REPLACE FUNCTION internal_read.list_candidates_base_as_of(
    p_entity_id uuid,
    p_business_unit_id uuid,
    p_status varchar(16),
    p_audit_horizon_sequence bigint,
    p_audit_horizon_hash bytea,
    p_last_created_at timestamptz,
    p_last_candidate_id uuid,
    p_limit integer
) RETURNS TABLE (
    contract_version varchar(32), candidate_ref uuid, short_id varchar(10),
    revision integer, status varchar(16), entity_ref uuid,
    business_unit_ref varchar(100), business_unit_label varchar(200),
    category_code varchar(100), category_label varchar(200), amount_minor bigint,
    currency varchar(3), accounting_month varchar(7), summary varchar(500),
    confidence_basis_points smallint, source jsonb, evidence jsonb,
    blockers jsonb, review_summary jsonb, created_at timestamptz,
    updated_at timestamptz, supersedes_candidate_ref uuid,
    superseded_by_candidate_ref uuid
) LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $function$
        BEGIN
            IF p_entity_id IS NULL OR p_audit_horizon_sequence IS NULL
               OR p_audit_horizon_sequence <= 0 OR p_audit_horizon_hash IS NULL
               OR octet_length(p_audit_horizon_hash) <> 32
               OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
               OR (p_last_created_at IS NULL) <> (p_last_candidate_id IS NULL) THEN
                RAISE EXCEPTION 'invalid candidate read parameters' USING ERRCODE = '22023';
            END IF;
            IF p_status IS NOT NULL AND p_status NOT IN
               ('INCOMPLETE','CONFLICTED','PENDING','CONFIRMED','IGNORED','SUPERSEDED') THEN
                RAISE EXCEPTION 'invalid candidate status' USING ERRCODE = '22023';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.audit_event AS horizon
                 WHERE horizon.sequence = p_audit_horizon_sequence
                   AND horizon.hash = p_audit_horizon_hash
                   AND octet_length(horizon.hash) = 32
            ) THEN
                RAISE EXCEPTION 'audit horizon is not an exact chain row'
                    USING ERRCODE = '22023';
            END IF;
            IF p_business_unit_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.business_unit AS bu
                 WHERE bu.id = p_business_unit_id AND bu.entity_id = p_entity_id
            ) THEN
                RAISE EXCEPTION 'business unit does not belong to entity'
                    USING ERRCODE = '22023';
            END IF;
            IF p_last_created_at IS NOT NULL AND NOT EXISTS (
                SELECT 1
                  FROM public.candidate AS cursor_candidate
                  JOIN LATERAL (
                      SELECT cr.revision, cr.status, cr.business_unit_id
                        FROM public.candidate_revision AS cr
                        JOIN public.candidate_event AS ce
                         ON ce.candidate_id = cr.candidate_id
                         AND ce.to_revision = cr.revision
                         AND ce.to_status = cr.status
                        JOIN public.audit_event AS ae ON ae.id = ce.audit_event_id
                       WHERE cr.candidate_id = cursor_candidate.id
                         AND ae.sequence <= p_audit_horizon_sequence
                         AND EXISTS (
                             SELECT 1
                               FROM public.candidate_revision AS cr0
                               JOIN public.candidate_event AS ce0
                                 ON ce0.candidate_id = cr0.candidate_id
                                AND ce0.to_revision = cr0.revision
                                AND ce0.to_status = cr0.status
                               JOIN public.audit_event AS ae0 ON ae0.id = ce0.audit_event_id
                              WHERE cr0.candidate_id = cursor_candidate.id
                                AND cr0.revision = 1
                                AND ce0.event_type = 'CREATE'
                                AND ae0.sequence <= p_audit_horizon_sequence
                         )
                       ORDER BY cr.revision DESC LIMIT 1
                  ) AS cursor_tip ON TRUE
                 WHERE cursor_candidate.id = p_last_candidate_id
                   AND cursor_candidate.entity_id = p_entity_id
                   AND cursor_candidate.created_at = p_last_created_at
                   AND ((p_business_unit_id IS NULL AND cursor_tip.business_unit_id IS NULL)
                        OR cursor_tip.business_unit_id = p_business_unit_id)
                   AND (p_status IS NULL OR cursor_tip.status = p_status)
            ) THEN
                RAISE EXCEPTION 'candidate cursor is outside requested scope'
                    USING ERRCODE = '22023';
            END IF;
            RETURN QUERY
            SELECT c.contract_version, c.id, c.short_id, r.revision, r.status,
                   c.entity_id, r.business_unit_ref_snapshot,
                   r.business_unit_label_snapshot, r.category_code_snapshot,
                   r.category_label_snapshot, r.amount_minor, r.currency,
                   to_char(r.accounting_month, 'YYYY-MM')::varchar(7), r.summary,
                   r.confidence_basis_points,
                   jsonb_build_object(
                       'ingest_channel', cs.ingest_channel_id,
                       'source_system', cs.source_system_id,
                       'source_event_ref', cs.source_event_ref,
                       'display_label', cs.display_label
                   ),
                   CASE WHEN r.business_unit_id IS NULL THEN '[]'::jsonb ELSE coalesce((
                       SELECT jsonb_agg(jsonb_build_object(
                           'evidence_ref', ce.evidence_ref,
                           'kind', ce.kind,
                           'media_type', ce.media_type_snapshot,
                           'display_name', ce.display_name_snapshot,
                           'download_available', ce.download_available
                       ) ORDER BY ce.ordinal)
                       FROM public.candidate_evidence AS ce
                       WHERE ce.candidate_id = c.id
                         AND ce.evidence_entity_id = c.entity_id
                         AND ce.evidence_business_unit_id = r.business_unit_id
                   ), '[]'::jsonb) END,
                   coalesce((
                       SELECT jsonb_agg(jsonb_build_object(
                           'code', b.code, 'message', b.message, 'field', b.field,
                           'conflict_ref', b.conflict_ref, 'evidence_ref', b.evidence_ref
                       ) ORDER BY b.ordinal)
                       FROM public.candidate_blocker AS b
                       WHERE b.candidate_id = c.id AND b.revision = r.revision
                   ), '[]'::jsonb),
                   jsonb_build_object(
                       'event_count', greatest(r.revision - 1, 0),
                       'last_action', (
                           SELECT ce2.action FROM public.candidate_event AS ce2
                            WHERE ce2.candidate_id = c.id
                              AND ce2.to_revision = r.revision
                              AND ce2.event_type <> 'CREATE'
                       ),
                       'last_decided_at', (
                           SELECT ce3.occurred_at FROM public.candidate_event AS ce3
                            WHERE ce3.candidate_id = c.id
                              AND ce3.to_revision = r.revision
                              AND ce3.event_type <> 'CREATE'
                       ),
                       'current_revision', r.revision
                   ),
                   c.created_at, r.updated_at, c.supersedes_candidate_id,
                   CASE WHEN r.status = 'SUPERSEDED' THEN (
                       SELECT successor.id
                         FROM public.candidate AS successor
                        WHERE successor.supersedes_candidate_id = c.id
                          AND successor.entity_id = c.entity_id
                          AND EXISTS (
                              SELECT 1
                                FROM public.candidate_revision AS sr
                                JOIN public.candidate_event AS se
                                  ON se.candidate_id = sr.candidate_id
                                 AND se.to_revision = sr.revision
                                 AND se.to_status = sr.status
                                JOIN public.audit_event AS sae ON sae.id = se.audit_event_id
                               WHERE sr.candidate_id = successor.id
                                 AND sr.revision = 1
                                 AND se.event_type = 'CREATE'
                                 AND sae.sequence <= p_audit_horizon_sequence
                          )
                        LIMIT 1
                   ) END
              FROM public.candidate AS c
              JOIN public.candidate_source AS cs ON cs.candidate_id = c.id
              JOIN LATERAL (
                  SELECT cr.revision, cr.status, cr.business_unit_id,
                         cr.business_unit_ref_snapshot, cr.business_unit_label_snapshot,
                         cr.category_code_snapshot, cr.category_label_snapshot,
                         cr.amount_minor, cr.currency, cr.accounting_month, cr.summary,
                         cr.confidence_basis_points, cr.created_at, cr.updated_at
                    FROM public.candidate_revision AS cr
                    JOIN public.candidate_event AS ev
                      ON ev.candidate_id = cr.candidate_id
                     AND ev.to_revision = cr.revision
                     AND ev.to_status = cr.status
                    JOIN public.audit_event AS ae ON ae.id = ev.audit_event_id
                   WHERE cr.candidate_id = c.id
                     AND ae.sequence <= p_audit_horizon_sequence
                     AND EXISTS (
                         SELECT 1
                           FROM public.candidate_revision AS cr0
                           JOIN public.candidate_event AS ev0
                             ON ev0.candidate_id = cr0.candidate_id
                            AND ev0.to_revision = cr0.revision
                            AND ev0.to_status = cr0.status
                           JOIN public.audit_event AS ae0 ON ae0.id = ev0.audit_event_id
                          WHERE cr0.candidate_id = c.id
                            AND cr0.revision = 1
                            AND ev0.event_type = 'CREATE'
                            AND ae0.sequence <= p_audit_horizon_sequence
                     )
                   ORDER BY cr.revision DESC
                   LIMIT 1
              ) AS r ON TRUE
             WHERE c.entity_id = p_entity_id
               AND ((p_business_unit_id IS NULL AND r.business_unit_id IS NULL)
                    OR r.business_unit_id = p_business_unit_id)
               AND (p_status IS NULL OR r.status = p_status)
               AND (p_last_created_at IS NULL
                    OR (c.created_at, c.id) > (p_last_created_at, p_last_candidate_id))
             ORDER BY c.created_at, c.id
             LIMIT (p_limit + 1);
        END
        $function$;

DROP FUNCTION internal_read.list_candidates_base_as_of(
    uuid, uuid, varchar, bigint, bytea, timestamptz, uuid, integer, uuid
);
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
