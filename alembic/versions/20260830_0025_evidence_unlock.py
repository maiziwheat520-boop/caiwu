"""Add authoritative reviewed evidence unlock facts and projection.

Revision ID: 20260830_0025
Revises: 20260830_0024
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_0025"
down_revision: str | None = "20260830_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    if os.getenv("LEDGERBRIDGE_ENV", "development").strip().lower() == "production":
        raise RuntimeError("production evidence unlock downgrade is forbidden")
    connection = op.get_bind()
    has_facts = connection.execute(
        sa.text(
            """
            SELECT EXISTS (SELECT 1 FROM internal_import.evidence_unlock_source)
                OR EXISTS (SELECT 1 FROM internal_command.evidence_unlock_operation)
                OR EXISTS (SELECT 1 FROM internal_command.evidence_unlock_receipt)
                OR EXISTS (SELECT 1 FROM internal_command.evidence_unlock_output)
            """
        )
    ).scalar_one()
    if has_facts:
        raise RuntimeError("evidence unlock facts prevent destructive downgrade")
    op.execute(_DOWNGRADE_SQL)


_UPGRADE_SQL = r"""
CREATE TABLE internal_import.evidence_unlock_source (
    source_ref uuid PRIMARY KEY,
    source_evidence_ref uuid NOT NULL UNIQUE,
    entity_id uuid NOT NULL,
    business_unit_id uuid NOT NULL,
    reviewed_audit_event_id uuid NOT NULL UNIQUE
        REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT evidence_unlock_source_ref_nonzero CHECK (
        source_ref <> UUID '00000000-0000-0000-0000-000000000000'
    ),
    CONSTRAINT evidence_unlock_source_distinct_ref CHECK (source_ref <> source_evidence_ref),
    CONSTRAINT evidence_unlock_source_scope FOREIGN KEY (
        source_evidence_ref, entity_id, business_unit_id
    ) REFERENCES public.evidence_object(evidence_ref, entity_id, business_unit_id)
        ON DELETE RESTRICT
);

CREATE TABLE internal_command.evidence_unlock_operation (
    operation_id uuid PRIMARY KEY,
    source_ref uuid NOT NULL REFERENCES internal_import.evidence_unlock_source(source_ref)
        ON DELETE RESTRICT,
    assertion_jti uuid NOT NULL UNIQUE,
    actor_ref varchar(200) NOT NULL,
    authentication_generation integer NOT NULL,
    workload_principal_ref varchar(200) NOT NULL,
    verified_san varchar(200) NOT NULL,
    policy_generation varchar(128) NOT NULL,
    scope_bindings jsonb NOT NULL,
    prepared_audit_event_id uuid NOT NULL UNIQUE
        REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT evidence_unlock_operation_nonzero CHECK (
        operation_id <> UUID '00000000-0000-0000-0000-000000000000'
        AND assertion_jti <> UUID '00000000-0000-0000-0000-000000000000'
    ),
    CONSTRAINT evidence_unlock_operation_actor CHECK (btrim(actor_ref) <> ''),
    CONSTRAINT evidence_unlock_operation_auth_generation CHECK (
        authentication_generation > 0
    ),
    CONSTRAINT evidence_unlock_operation_principal CHECK (
        btrim(workload_principal_ref) <> '' AND btrim(verified_san) <> ''
        AND btrim(policy_generation) <> ''
    ),
    CONSTRAINT evidence_unlock_operation_scope_shape CHECK (
        jsonb_typeof(scope_bindings) = 'array'
        AND jsonb_array_length(scope_bindings) BETWEEN 1 AND 128
    )
);

CREATE TABLE internal_command.evidence_unlock_receipt (
    operation_id uuid PRIMARY KEY
        REFERENCES internal_command.evidence_unlock_operation(operation_id)
        ON DELETE RESTRICT,
    outcome varchar(16) NOT NULL,
    output_count integer NOT NULL,
    error_code varchar(64),
    completion_audit_event_id uuid NOT NULL UNIQUE
        REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT evidence_unlock_receipt_outcome CHECK (outcome IN ('UNLOCKED','REJECTED')),
    CONSTRAINT evidence_unlock_receipt_shape CHECK (
        (outcome = 'UNLOCKED' AND output_count BETWEEN 1 AND 64 AND error_code IS NULL)
        OR (outcome = 'REJECTED' AND output_count = 0 AND error_code = 'UNLOCK_REJECTED')
    )
);

CREATE TABLE internal_command.evidence_unlock_output (
    operation_id uuid NOT NULL
        REFERENCES internal_command.evidence_unlock_operation(operation_id)
        ON DELETE RESTRICT,
    ordinal integer NOT NULL,
    proposed_evidence_ref uuid NOT NULL,
    evidence_ref uuid NOT NULL REFERENCES public.evidence_object(evidence_ref)
        ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT evidence_unlock_output_ordinal CHECK (ordinal BETWEEN 0 AND 63),
    CONSTRAINT evidence_unlock_output_proposed_nonzero CHECK (
        proposed_evidence_ref <> UUID '00000000-0000-0000-0000-000000000000'
    ),
    CONSTRAINT evidence_unlock_output_pkey PRIMARY KEY (operation_id, ordinal),
    CONSTRAINT evidence_unlock_output_proposed_unique UNIQUE (
        operation_id, proposed_evidence_ref
    ),
    CONSTRAINT evidence_unlock_output_evidence_unique UNIQUE (operation_id, evidence_ref)
);

CREATE FUNCTION internal_command.evidence_unlock_reject_mutation()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
BEGIN
    RAISE EXCEPTION 'evidence unlock facts are append-only'
        USING ERRCODE = 'integrity_constraint_violation';
END
$function$;

ALTER FUNCTION internal_read.list_candidates_as_of(
    uuid, uuid, varchar, bigint, bytea, timestamptz, uuid, integer
) RENAME TO list_candidates_base_as_of;
ALTER FUNCTION internal_read.render_candidate_revision(uuid, integer)
    RENAME TO render_candidate_revision_base;

CREATE FUNCTION internal_read.project_evidence_unlocks(
    p_evidence jsonb, p_audit_horizon_sequence bigint
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE v_projection jsonb;
BEGIN
    IF jsonb_typeof(p_evidence) IS DISTINCT FROM 'array'
       OR p_audit_horizon_sequence IS NULL OR p_audit_horizon_sequence <= 0 THEN
        RAISE EXCEPTION 'evidence unlock projection parameters are invalid'
            USING ERRCODE = '22023';
    END IF;
    WITH original AS (
        SELECT item, position
          FROM jsonb_array_elements(p_evidence) WITH ORDINALITY AS entry(item, position)
    ), projected_original AS (
        SELECT position * 1000 AS sort_key,
               item || jsonb_build_object(
                   'unlock_status', CASE
                       WHEN reviewed.id IS NULL THEN 'NOT_REQUIRED'
                       WHEN completion.operation_id IS NULL THEN 'PASSWORD_REQUIRED'
                       ELSE 'UNLOCKED'
                   END,
                   'source_ref', CASE WHEN reviewed.id IS NULL THEN NULL
                                      ELSE source.source_ref END
               ) AS evidence
          FROM original
          LEFT JOIN internal_import.evidence_unlock_source AS source
            ON source.source_evidence_ref = (item->>'evidence_ref')::uuid
          LEFT JOIN public.audit_event AS reviewed
            ON reviewed.id = source.reviewed_audit_event_id
           AND reviewed.sequence <= p_audit_horizon_sequence
          LEFT JOIN LATERAL (
              SELECT receipt.operation_id
                FROM internal_command.evidence_unlock_operation AS operation
                JOIN internal_command.evidence_unlock_receipt AS receipt
                  ON receipt.operation_id = operation.operation_id
                 AND receipt.outcome = 'UNLOCKED'
                JOIN public.audit_event AS completed
                  ON completed.id = receipt.completion_audit_event_id
                 AND completed.sequence <= p_audit_horizon_sequence
               WHERE operation.source_ref = source.source_ref
               ORDER BY completed.sequence DESC, receipt.operation_id DESC
               LIMIT 1
          ) AS completion ON reviewed.id IS NOT NULL
    ), projected_outputs AS (
        SELECT original.position * 1000 + output.ordinal + 1 AS sort_key,
               jsonb_build_object(
                   'evidence_ref', output.evidence_ref,
                   'kind', 'ATTACHMENT',
                   'media_type', evidence.media_type,
                   'display_name', evidence.display_name,
                   'download_available', true,
                   'unlock_status', 'UNLOCKED',
                   'source_ref', source.source_ref
               ) AS evidence
          FROM original
          JOIN internal_import.evidence_unlock_source AS source
            ON source.source_evidence_ref = (original.item->>'evidence_ref')::uuid
          JOIN public.audit_event AS reviewed
            ON reviewed.id = source.reviewed_audit_event_id
           AND reviewed.sequence <= p_audit_horizon_sequence
          JOIN LATERAL (
              SELECT operation.operation_id
                FROM internal_command.evidence_unlock_operation AS operation
                JOIN internal_command.evidence_unlock_receipt AS receipt
                  ON receipt.operation_id = operation.operation_id
                 AND receipt.outcome = 'UNLOCKED'
                JOIN public.audit_event AS completed
                  ON completed.id = receipt.completion_audit_event_id
                 AND completed.sequence <= p_audit_horizon_sequence
               WHERE operation.source_ref = source.source_ref
               ORDER BY completed.sequence DESC, receipt.operation_id DESC
               LIMIT 1
          ) AS completion ON true
          JOIN internal_command.evidence_unlock_output AS output
            ON output.operation_id = completion.operation_id
          JOIN public.evidence_object AS evidence
            ON evidence.evidence_ref = output.evidence_ref
    ), combined AS (
        SELECT * FROM projected_original
        UNION ALL
        SELECT * FROM projected_outputs
    )
    SELECT coalesce(jsonb_agg(evidence ORDER BY sort_key), '[]'::jsonb)
      INTO v_projection FROM combined;
    RETURN v_projection;
END
$function$;

CREATE FUNCTION internal_read.list_candidates_as_of(
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
          p_entity_id, p_business_unit_id, p_status,
          p_audit_horizon_sequence, p_audit_horizon_hash,
          p_last_created_at, p_last_candidate_id, p_limit
      ) AS base;
END
$function$;

CREATE FUNCTION internal_read.render_candidate_revision(
    p_candidate_id uuid, p_revision integer
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE v_projection jsonb; v_horizon bigint;
BEGIN
    v_projection := internal_read.render_candidate_revision_base(
        p_candidate_id, p_revision
    );
    SELECT max(sequence) INTO v_horizon FROM public.audit_event;
    IF v_horizon IS NULL THEN
        RAISE EXCEPTION 'audit chain is empty' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN jsonb_set(
        v_projection, '{evidence}',
        internal_read.project_evidence_unlocks(v_projection->'evidence', v_horizon),
        false
    );
END
$function$;

CREATE FUNCTION internal_command.reject_evidence_unlock(p_request jsonb)
RETURNS TABLE (source_ref uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_operation internal_command.evidence_unlock_operation%ROWTYPE;
    v_receipt internal_command.evidence_unlock_receipt%ROWTYPE; v_audit uuid;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR NOT (p_request ?& ARRAY[
           'contract_version','source_ref','operation_id','assertion_jti','actor_ref',
           'authentication_generation','workload_principal_ref','verified_san',
           'policy_generation','scope_bindings','error_code'
       ])
       OR p_request - ARRAY[
           'contract_version','source_ref','operation_id','assertion_jti','actor_ref',
           'authentication_generation','workload_principal_ref','verified_san',
           'policy_generation','scope_bindings','error_code'
       ] <> '{}'::jsonb
       OR (p_request->>'error_code') IS DISTINCT FROM 'UNLOCK_REJECTED' THEN
        RAISE EXCEPTION 'evidence unlock rejection request is invalid' USING ERRCODE = '22023';
    END IF;
    v_operation := internal_command.require_evidence_unlock_operation(
        p_request, 'ledgerbridge.evidence-unlock-rejection.v1'
    );
    SELECT * INTO v_receipt FROM internal_command.evidence_unlock_receipt AS receipt
     WHERE receipt.operation_id = v_operation.operation_id;
    IF FOUND THEN
        IF v_receipt.outcome = 'UNLOCKED' THEN
            RAISE EXCEPTION 'successful evidence unlock cannot be rejected'
                USING ERRCODE = 'LB005';
        END IF;
        RETURN QUERY SELECT v_operation.source_ref;
        RETURN;
    END IF;
    v_audit := public.append_audit_event(
        v_operation.actor_ref, 'evidence.unlock.rejected',
        'reviewed evidence unlock rejected', 'ledgerbridge.evidence-unlock-rejection.v1',
        jsonb_build_object(
            'operation_id', v_operation.operation_id,
            'source_ref', v_operation.source_ref,
            'error_code', 'UNLOCK_REJECTED'
        )
    );
    INSERT INTO internal_command.evidence_unlock_receipt(
        operation_id, outcome, output_count, error_code,
        completion_audit_event_id, created_at
    ) VALUES (
        v_operation.operation_id, 'REJECTED', 0, 'UNLOCK_REJECTED', v_audit,
        (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
    );
    RETURN QUERY SELECT v_operation.source_ref;
END
$function$;

CREATE FUNCTION internal_command.complete_evidence_unlock(p_request jsonb)
RETURNS TABLE (source_ref uuid, unlock_status text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_operation internal_command.evidence_unlock_operation%ROWTYPE;
    v_source internal_import.evidence_unlock_source%ROWTYPE;
    v_receipt internal_command.evidence_unlock_receipt%ROWTYPE;
    v_item jsonb; v_actual_evidence uuid; v_proposed_evidence uuid;
    v_existing_evidence public.evidence_object%ROWTYPE; v_existing_blob record;
    v_plaintext_size bigint; v_ciphertext_size bigint; v_chunk_size integer;
    v_evidence_audit uuid; v_blob_audit uuid; v_blob_ref uuid; v_completion_audit uuid;
    v_ordinal integer := 0; v_output_facts jsonb := '[]'::jsonb;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR NOT (p_request ?& ARRAY[
           'contract_version','source_ref','operation_id','assertion_jti','actor_ref',
           'authentication_generation','workload_principal_ref','verified_san',
           'policy_generation','scope_bindings','outputs'
       ])
       OR p_request - ARRAY[
           'contract_version','source_ref','operation_id','assertion_jti','actor_ref',
           'authentication_generation','workload_principal_ref','verified_san',
           'policy_generation','scope_bindings','outputs'
       ] <> '{}'::jsonb
       OR jsonb_typeof(p_request->'outputs') IS DISTINCT FROM 'array'
       OR jsonb_array_length(p_request->'outputs') NOT BETWEEN 1 AND 64 THEN
        RAISE EXCEPTION 'evidence unlock completion request is invalid' USING ERRCODE = '22023';
    END IF;
    v_operation := internal_command.require_evidence_unlock_operation(
        p_request, 'ledgerbridge.evidence-unlock-completion.v1'
    );
    SELECT * INTO v_receipt FROM internal_command.evidence_unlock_receipt AS receipt
     WHERE receipt.operation_id = v_operation.operation_id;
    IF FOUND THEN
        IF v_receipt.outcome = 'REJECTED' THEN
            RAISE EXCEPTION 'rejected evidence unlock cannot be completed' USING ERRCODE = 'LB006';
        END IF;
        RETURN QUERY SELECT v_operation.source_ref, 'UNLOCKED'::text;
        RETURN;
    END IF;
    SELECT * INTO STRICT v_source FROM internal_import.evidence_unlock_source AS source
     WHERE source.source_ref = v_operation.source_ref;
    IF (
        SELECT count(*) <> count(DISTINCT item->>'object_ref')
            OR count(*) <> count(DISTINCT item->>'evidence_ref')
        FROM jsonb_array_elements(p_request->'outputs') AS item
    ) THEN
        RAISE EXCEPTION 'evidence unlock output identities are duplicated' USING ERRCODE = '22023';
    END IF;
    FOR v_item IN SELECT value FROM jsonb_array_elements(p_request->'outputs') LOOP
        IF jsonb_typeof(v_item) IS DISTINCT FROM 'object'
           OR NOT (v_item ?& ARRAY[
               'evidence_ref','media_type','display_name','object_ref','plaintext_sha256',
               'plaintext_size','ciphertext_sha256','ciphertext_size','storage_key',
               'chunk_size','stream_header','wrapped_key_generation','wrapped_key_nonce',
               'wrapped_key_ciphertext'
           ])
           OR v_item - ARRAY[
               'evidence_ref','media_type','display_name','object_ref','plaintext_sha256',
               'plaintext_size','ciphertext_sha256','ciphertext_size','storage_key',
               'chunk_size','stream_header','wrapped_key_generation','wrapped_key_nonce',
               'wrapped_key_ciphertext'
           ] <> '{}'::jsonb
           OR coalesce(v_item->>'object_ref','') !~ '^[0-9a-f]{64}$'
           OR coalesce(v_item->>'plaintext_sha256','') !~ '^[0-9a-f]{64}$'
           OR coalesce(v_item->>'ciphertext_sha256','') !~ '^[0-9a-f]{64}$'
           OR coalesce(v_item->>'stream_header','') !~ '^[0-9a-f]{48}$'
           OR coalesce(v_item->>'wrapped_key_nonce','') !~ '^[0-9a-f]{48}$'
           OR coalesce(v_item->>'wrapped_key_ciphertext','') !~ '^[0-9a-f]{96}$'
           OR coalesce(v_item->>'wrapped_key_generation','')
                !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
           OR btrim(coalesce(v_item->>'media_type','')) = ''
           OR length(v_item->>'media_type') > 200
           OR (v_item->>'media_type') ~ '[[:cntrl:]]'
           OR btrim(coalesce(v_item->>'display_name','')) = ''
           OR length(v_item->>'display_name') > 200
           OR (v_item->>'display_name') IN ('.','..')
           OR (v_item->>'display_name') ~ '[/\\]|[[:cntrl:]]'
           OR coalesce(v_item->>'plaintext_size','') !~ '^[1-9][0-9]{0,8}$'
           OR coalesce(v_item->>'ciphertext_size','') !~ '^[1-9][0-9]{0,9}$'
           OR coalesce(v_item->>'chunk_size','') !~ '^[1-9][0-9]{0,6}$' THEN
            RAISE EXCEPTION 'evidence unlock output descriptor is invalid'
                USING ERRCODE = '22023';
        END IF;
        BEGIN
            v_proposed_evidence := (v_item->>'evidence_ref')::uuid;
            v_plaintext_size := (v_item->>'plaintext_size')::bigint;
            v_ciphertext_size := (v_item->>'ciphertext_size')::bigint;
            v_chunk_size := (v_item->>'chunk_size')::integer;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RAISE EXCEPTION 'evidence unlock output descriptor is invalid'
                USING ERRCODE = '22023';
        END;
        IF v_proposed_evidence = UUID '00000000-0000-0000-0000-000000000000'
           OR v_proposed_evidence::text IS DISTINCT FROM v_item->>'evidence_ref'
           OR v_plaintext_size NOT BETWEEN 1 AND 52428800
           OR v_ciphertext_size NOT BETWEEN 1 AND 268435456
           OR v_chunk_size NOT BETWEEN 1 AND 1048576
           OR (v_item->>'storage_key') IS DISTINCT FROM concat(
                'sha256/', substr(v_item->>'ciphertext_sha256',1,2), '/',
                substr(v_item->>'ciphertext_sha256',3,2), '/',
                v_item->>'ciphertext_sha256'
           ) THEN
            RAISE EXCEPTION 'evidence unlock output descriptor is invalid'
                USING ERRCODE = '22023';
        END IF;
        SELECT identity.evidence_ref INTO v_actual_evidence
          FROM public.encrypted_object_identity AS identity
         WHERE identity.object_ref = v_item->>'object_ref';
        IF FOUND THEN
            SELECT * INTO STRICT v_existing_evidence FROM public.evidence_object AS evidence
             WHERE evidence.evidence_ref = v_actual_evidence;
            SELECT * INTO v_existing_blob
              FROM internal_read.resolve_active_evidence_blob(v_actual_evidence);
            IF NOT FOUND
               OR v_existing_evidence.entity_id IS DISTINCT FROM v_source.entity_id
               OR v_existing_evidence.business_unit_id IS DISTINCT FROM v_source.business_unit_id
               OR v_existing_evidence.media_type IS DISTINCT FROM v_item->>'media_type'
               OR v_existing_evidence.display_name IS DISTINCT FROM v_item->>'display_name'
               OR v_existing_evidence.plaintext_sha256
                    IS DISTINCT FROM decode(v_item->>'plaintext_sha256', 'hex')
               OR v_existing_evidence.plaintext_size IS DISTINCT FROM v_plaintext_size
               OR v_existing_blob.ciphertext_sha256
                    IS DISTINCT FROM decode(v_item->>'ciphertext_sha256', 'hex')
               OR v_existing_blob.ciphertext_size IS DISTINCT FROM v_ciphertext_size
               OR v_existing_blob.storage_key IS DISTINCT FROM v_item->>'storage_key'
               OR v_existing_blob.chunk_size IS DISTINCT FROM v_chunk_size
               OR v_existing_blob.stream_header
                    IS DISTINCT FROM decode(v_item->>'stream_header', 'hex')
               OR v_existing_blob.wrapped_key_generation
                    IS DISTINCT FROM v_item->>'wrapped_key_generation'
               OR v_existing_blob.wrapped_key_nonce
                    IS DISTINCT FROM decode(v_item->>'wrapped_key_nonce', 'hex')
               OR v_existing_blob.wrapped_key_ciphertext
                    IS DISTINCT FROM decode(v_item->>'wrapped_key_ciphertext', 'hex') THEN
                RAISE EXCEPTION 'encrypted output identity conflicts with existing evidence'
                    USING ERRCODE = 'LB005';
            END IF;
        ELSE
            IF EXISTS (
                SELECT 1 FROM public.evidence_object AS evidence
                 WHERE evidence.evidence_ref = v_proposed_evidence
            ) THEN
                RAISE EXCEPTION 'proposed evidence identity already exists' USING ERRCODE = 'LB005';
            END IF;
            v_actual_evidence := v_proposed_evidence;
            v_evidence_audit := public.append_audit_event(
                v_operation.actor_ref, 'evidence.object.create',
                'register encrypted unlocked evidence', 'ledgerbridge.evidence-unlock.v1',
                jsonb_build_object(
                    'evidence_ref', v_actual_evidence::text,
                    'entity_id', v_source.entity_id::text,
                    'business_unit_id', v_source.business_unit_id::text
                )
            );
            INSERT INTO public.evidence_object(
                evidence_ref, entity_id, business_unit_id, media_type, display_name,
                plaintext_sha256, plaintext_size, audit_event_id,
                raw_artifact_id, source_record_id, created_at
            ) VALUES (
                v_actual_evidence, v_source.entity_id, v_source.business_unit_id,
                v_item->>'media_type', v_item->>'display_name',
                decode(v_item->>'plaintext_sha256', 'hex'), v_plaintext_size,
                v_evidence_audit, NULL, NULL,
                (SELECT occurred_at FROM public.audit_event WHERE id = v_evidence_audit)
            );
            INSERT INTO public.encrypted_object_identity(object_ref, evidence_ref)
            VALUES (v_item->>'object_ref', v_actual_evidence);
            v_blob_ref := gen_random_uuid();
            v_blob_audit := public.append_audit_event(
                v_operation.actor_ref, 'evidence.blob.version',
                'register encrypted unlocked evidence blob', 'ledgerbridge.evidence-unlock.v1',
                jsonb_build_object(
                    'rotation_mode', 'GENESIS',
                    'blob_ref', v_blob_ref::text,
                    'evidence_ref', v_actual_evidence::text,
                    'predecessor_blob_ref', NULL::text,
                    'object_ref', v_item->>'object_ref',
                    'ciphertext_sha256', v_item->>'ciphertext_sha256',
                    'ciphertext_size', v_ciphertext_size,
                    'storage_key', v_item->>'storage_key',
                    'envelope_schema', 'ledgerbridge.secretstream.v1',
                    'algorithm', 'xchacha20poly1305-secretstream',
                    'chunk_size', v_chunk_size,
                    'stream_header', v_item->>'stream_header',
                    'wrapped_key_generation', v_item->>'wrapped_key_generation',
                    'wrapped_key_nonce', v_item->>'wrapped_key_nonce',
                    'wrapped_key_ciphertext', v_item->>'wrapped_key_ciphertext',
                    'purpose', 'ledgerbridge-artifact-v2'
                )
            );
            INSERT INTO public.encrypted_blob_version(
                blob_ref, evidence_ref, predecessor_blob_ref, object_ref,
                ciphertext_sha256, ciphertext_size, storage_key,
                envelope_schema, algorithm, chunk_size, stream_header,
                wrapped_key_generation, wrapped_key_nonce, wrapped_key_ciphertext,
                purpose, audit_event_id, created_at
            ) VALUES (
                v_blob_ref, v_actual_evidence, NULL, v_item->>'object_ref',
                decode(v_item->>'ciphertext_sha256', 'hex'), v_ciphertext_size,
                v_item->>'storage_key', 'ledgerbridge.secretstream.v1',
                'xchacha20poly1305-secretstream', v_chunk_size,
                decode(v_item->>'stream_header', 'hex'),
                v_item->>'wrapped_key_generation',
                decode(v_item->>'wrapped_key_nonce', 'hex'),
                decode(v_item->>'wrapped_key_ciphertext', 'hex'),
                'ledgerbridge-artifact-v2', v_blob_audit,
                (SELECT occurred_at FROM public.audit_event WHERE id = v_blob_audit)
            );
        END IF;
        v_output_facts := v_output_facts || jsonb_build_array(jsonb_build_object(
            'ordinal', v_ordinal, 'proposed_evidence_ref', v_proposed_evidence,
            'evidence_ref', v_actual_evidence
        ));
        v_ordinal := v_ordinal + 1;
    END LOOP;
    v_completion_audit := public.append_audit_event(
        v_operation.actor_ref, 'evidence.unlock.completed',
        'complete reviewed evidence unlock', 'ledgerbridge.evidence-unlock-completion.v1',
        jsonb_build_object(
            'operation_id', v_operation.operation_id,
            'source_ref', v_operation.source_ref,
            'outputs', v_output_facts
        )
    );
    INSERT INTO internal_command.evidence_unlock_output(
        operation_id, ordinal, proposed_evidence_ref, evidence_ref, created_at
    )
    SELECT v_operation.operation_id, (fact->>'ordinal')::integer,
           (fact->>'proposed_evidence_ref')::uuid, (fact->>'evidence_ref')::uuid,
           (SELECT occurred_at FROM public.audit_event WHERE id = v_completion_audit)
      FROM jsonb_array_elements(v_output_facts) AS fact;
    INSERT INTO internal_command.evidence_unlock_receipt(
        operation_id, outcome, output_count, error_code,
        completion_audit_event_id, created_at
    ) VALUES (
        v_operation.operation_id, 'UNLOCKED', v_ordinal, NULL, v_completion_audit,
        (SELECT occurred_at FROM public.audit_event WHERE id = v_completion_audit)
    );
    RETURN QUERY SELECT v_operation.source_ref, 'UNLOCKED'::text;
END
$function$;

CREATE TRIGGER evidence_unlock_source_append_only
BEFORE UPDATE OR DELETE ON internal_import.evidence_unlock_source
FOR EACH ROW EXECUTE FUNCTION internal_command.evidence_unlock_reject_mutation();
CREATE TRIGGER evidence_unlock_operation_append_only
BEFORE UPDATE OR DELETE ON internal_command.evidence_unlock_operation
FOR EACH ROW EXECUTE FUNCTION internal_command.evidence_unlock_reject_mutation();
CREATE TRIGGER evidence_unlock_receipt_append_only
BEFORE UPDATE OR DELETE ON internal_command.evidence_unlock_receipt
FOR EACH ROW EXECUTE FUNCTION internal_command.evidence_unlock_reject_mutation();
CREATE TRIGGER evidence_unlock_output_append_only
BEFORE UPDATE OR DELETE ON internal_command.evidence_unlock_output
FOR EACH ROW EXECUTE FUNCTION internal_command.evidence_unlock_reject_mutation();

CREATE FUNCTION internal_command.normalize_evidence_unlock_scope_bindings(p_bindings jsonb)
RETURNS jsonb LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog
AS $function$
DECLARE v_item jsonb; v_entity uuid; v_unit uuid; v_result jsonb;
BEGIN
    IF jsonb_typeof(p_bindings) IS DISTINCT FROM 'array'
       OR jsonb_array_length(p_bindings) NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'evidence unlock scope bindings are invalid' USING ERRCODE = '22023';
    END IF;
    FOR v_item IN SELECT value FROM jsonb_array_elements(p_bindings) LOOP
        IF jsonb_typeof(v_item) IS DISTINCT FROM 'object'
           OR NOT (v_item ?& ARRAY['entity_ref','business_unit_id'])
           OR v_item - ARRAY['entity_ref','business_unit_id'] <> '{}'::jsonb THEN
            RAISE EXCEPTION 'evidence unlock scope binding is invalid' USING ERRCODE = '22023';
        END IF;
        BEGIN
            v_entity := (v_item->>'entity_ref')::uuid;
            v_unit := (v_item->>'business_unit_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'evidence unlock scope binding is invalid' USING ERRCODE = '22023';
        END;
        IF v_entity = UUID '00000000-0000-0000-0000-000000000000'
           OR v_unit = UUID '00000000-0000-0000-0000-000000000000'
           OR v_entity::text IS DISTINCT FROM v_item->>'entity_ref'
           OR v_unit::text IS DISTINCT FROM v_item->>'business_unit_id' THEN
            RAISE EXCEPTION 'evidence unlock scope binding is invalid' USING ERRCODE = '22023';
        END IF;
    END LOOP;
    SELECT jsonb_agg(binding ORDER BY binding->>'entity_ref', binding->>'business_unit_id')
      INTO v_result
      FROM (
          SELECT DISTINCT jsonb_build_object(
              'entity_ref', (item->>'entity_ref')::uuid::text,
              'business_unit_id', (item->>'business_unit_id')::uuid::text
          ) AS binding
          FROM jsonb_array_elements(p_bindings) AS item
      ) AS normalized;
    RETURN v_result;
END
$function$;

CREATE FUNCTION internal_import.register_evidence_unlock_source(p_request jsonb)
RETURNS TABLE (source_ref uuid, source_evidence_ref uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_source uuid; v_evidence uuid; v_entity uuid; v_unit uuid;
    v_existing internal_import.evidence_unlock_source%ROWTYPE; v_audit uuid;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR NOT (p_request ?& ARRAY[
           'contract_version','source_ref','source_evidence_ref','entity_ref',
           'business_unit_id','actor_ref','reason'
       ])
       OR p_request - ARRAY[
           'contract_version','source_ref','source_evidence_ref','entity_ref',
           'business_unit_id','actor_ref','reason'
       ] <> '{}'::jsonb
       OR (p_request->>'contract_version')
            IS DISTINCT FROM 'ledgerbridge.evidence-unlock-source.v1'
       OR btrim(coalesce(p_request->>'actor_ref','')) = ''
       OR length(p_request->>'actor_ref') > 200
       OR btrim(coalesce(p_request->>'reason','')) = ''
       OR length(p_request->>'reason') > 1000 THEN
        RAISE EXCEPTION 'evidence unlock source request is invalid' USING ERRCODE = '22023';
    END IF;
    BEGIN
        v_source := (p_request->>'source_ref')::uuid;
        v_evidence := (p_request->>'source_evidence_ref')::uuid;
        v_entity := (p_request->>'entity_ref')::uuid;
        v_unit := (p_request->>'business_unit_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION 'evidence unlock source request is invalid' USING ERRCODE = '22023';
    END;
    IF v_source = UUID '00000000-0000-0000-0000-000000000000'
       OR v_evidence = UUID '00000000-0000-0000-0000-000000000000'
       OR v_entity = UUID '00000000-0000-0000-0000-000000000000'
       OR v_unit = UUID '00000000-0000-0000-0000-000000000000'
       OR v_source::text IS DISTINCT FROM p_request->>'source_ref'
       OR v_evidence::text IS DISTINCT FROM p_request->>'source_evidence_ref'
       OR v_entity::text IS DISTINCT FROM p_request->>'entity_ref'
       OR v_unit::text IS DISTINCT FROM p_request->>'business_unit_id' THEN
        RAISE EXCEPTION 'evidence unlock source request is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_existing FROM internal_import.evidence_unlock_source
     WHERE evidence_unlock_source.source_ref = v_source
        OR evidence_unlock_source.source_evidence_ref = v_evidence;
    IF FOUND THEN
        IF v_existing.source_ref IS DISTINCT FROM v_source
           OR v_existing.source_evidence_ref IS DISTINCT FROM v_evidence
           OR v_existing.entity_id IS DISTINCT FROM v_entity
           OR v_existing.business_unit_id IS DISTINCT FROM v_unit THEN
            RAISE EXCEPTION 'evidence unlock source conflicts with reviewed identity'
                USING ERRCODE = 'LB005';
        END IF;
        RETURN QUERY SELECT v_existing.source_ref, v_existing.source_evidence_ref;
        RETURN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.evidence_object AS evidence
         WHERE evidence.evidence_ref = v_evidence
           AND evidence.entity_id = v_entity
           AND evidence.business_unit_id = v_unit
    ) OR NOT EXISTS (
        SELECT 1 FROM internal_read.resolve_active_evidence_blob(v_evidence)
    ) THEN
        RAISE EXCEPTION 'reviewed evidence source was not found' USING ERRCODE = 'LB004';
    END IF;
    v_audit := public.append_audit_event(
        p_request->>'actor_ref', 'evidence.unlock.source.reviewed', p_request->>'reason',
        'ledgerbridge.evidence-unlock-source.v1',
        jsonb_build_object(
            'source_ref', v_source, 'source_evidence_ref', v_evidence,
            'entity_ref', v_entity, 'business_unit_id', v_unit
        )
    );
    INSERT INTO internal_import.evidence_unlock_source(
        source_ref, source_evidence_ref, entity_id, business_unit_id,
        reviewed_audit_event_id, created_at
    ) VALUES (
        v_source, v_evidence, v_entity, v_unit, v_audit,
        (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
    );
    RETURN QUERY SELECT v_source, v_evidence;
END
$function$;

CREATE FUNCTION internal_command.require_evidence_unlock_operation(
    p_request jsonb, p_contract_version text
) RETURNS internal_command.evidence_unlock_operation
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_operation uuid; v_source uuid; v_jti uuid; v_auth integer;
    v_scope jsonb; v_row internal_command.evidence_unlock_operation%ROWTYPE;
BEGIN
    IF (p_request->>'contract_version') IS DISTINCT FROM p_contract_version
       OR btrim(coalesce(p_request->>'actor_ref','')) = ''
       OR length(p_request->>'actor_ref') > 200
       OR btrim(coalesce(p_request->>'workload_principal_ref','')) = ''
       OR length(p_request->>'workload_principal_ref') > 200
       OR btrim(coalesce(p_request->>'verified_san','')) = ''
       OR length(p_request->>'verified_san') > 200
       OR btrim(coalesce(p_request->>'policy_generation','')) = ''
       OR length(p_request->>'policy_generation') > 128
       OR coalesce(p_request->>'authentication_generation','') !~ '^[1-9][0-9]{0,9}$' THEN
        RAISE EXCEPTION 'evidence unlock operation identity is invalid' USING ERRCODE = '22023';
    END IF;
    BEGIN
        v_operation := (p_request->>'operation_id')::uuid;
        v_source := (p_request->>'source_ref')::uuid;
        v_jti := (p_request->>'assertion_jti')::uuid;
        v_auth := (p_request->>'authentication_generation')::integer;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'evidence unlock operation identity is invalid' USING ERRCODE = '22023';
    END;
    v_scope := internal_command.normalize_evidence_unlock_scope_bindings(
        p_request->'scope_bindings'
    );
    SELECT * INTO v_row FROM internal_command.evidence_unlock_operation AS operation
     WHERE operation.operation_id = v_operation FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'evidence unlock operation was not prepared' USING ERRCODE = 'LB004';
    END IF;
    IF v_row.source_ref IS DISTINCT FROM v_source
       OR v_row.assertion_jti IS DISTINCT FROM v_jti
       OR v_row.actor_ref IS DISTINCT FROM p_request->>'actor_ref'
       OR v_row.authentication_generation IS DISTINCT FROM v_auth
       OR v_row.workload_principal_ref IS DISTINCT FROM p_request->>'workload_principal_ref'
       OR v_row.verified_san IS DISTINCT FROM p_request->>'verified_san'
       OR v_row.policy_generation IS DISTINCT FROM p_request->>'policy_generation'
       OR v_row.scope_bindings IS DISTINCT FROM v_scope THEN
        RAISE EXCEPTION 'evidence unlock operation identity conflicts with reservation'
            USING ERRCODE = 'LB005';
    END IF;
    RETURN v_row;
END
$function$;

CREATE FUNCTION internal_command.prepare_evidence_unlock(p_request jsonb)
RETURNS TABLE (
    outcome text, source_ref uuid, source_evidence_ref uuid, entity_ref uuid,
    business_unit_ref varchar(100), object_ref varchar(64), plaintext_sha256 bytea,
    plaintext_size bigint, ciphertext_sha256 bytea, ciphertext_size bigint,
    storage_key varchar(77), chunk_size integer, stream_header bytea,
    wrapped_key_generation varchar(128), wrapped_key_nonce bytea,
    wrapped_key_ciphertext bytea
) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_operation uuid; v_source_ref uuid; v_jti uuid; v_auth integer;
    v_scope jsonb; v_source internal_import.evidence_unlock_source%ROWTYPE;
    v_operation_row internal_command.evidence_unlock_operation%ROWTYPE;
    v_receipt internal_command.evidence_unlock_receipt%ROWTYPE;
    v_blob record; v_audit uuid;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR NOT (p_request ?& ARRAY[
           'contract_version','source_ref','operation_id','assertion_jti','actor_ref',
           'authentication_generation','workload_principal_ref','verified_san',
           'policy_generation','scope_bindings'
       ])
       OR p_request - ARRAY[
           'contract_version','source_ref','operation_id','assertion_jti','actor_ref',
           'authentication_generation','workload_principal_ref','verified_san',
           'policy_generation','scope_bindings'
       ] <> '{}'::jsonb
       OR (p_request->>'contract_version')
            IS DISTINCT FROM 'ledgerbridge.evidence-unlock-command.v1'
       OR btrim(coalesce(p_request->>'actor_ref','')) = ''
       OR length(p_request->>'actor_ref') > 200
       OR btrim(coalesce(p_request->>'workload_principal_ref','')) = ''
       OR length(p_request->>'workload_principal_ref') > 200
       OR btrim(coalesce(p_request->>'verified_san','')) = ''
       OR length(p_request->>'verified_san') > 200
       OR btrim(coalesce(p_request->>'policy_generation','')) = ''
       OR length(p_request->>'policy_generation') > 128
       OR coalesce(p_request->>'authentication_generation','') !~ '^[1-9][0-9]{0,9}$' THEN
        RAISE EXCEPTION 'evidence unlock preparation request is invalid' USING ERRCODE = '22023';
    END IF;
    BEGIN
        v_operation := (p_request->>'operation_id')::uuid;
        v_source_ref := (p_request->>'source_ref')::uuid;
        v_jti := (p_request->>'assertion_jti')::uuid;
        v_auth := (p_request->>'authentication_generation')::integer;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'evidence unlock preparation request is invalid' USING ERRCODE = '22023';
    END;
    IF v_operation = UUID '00000000-0000-0000-0000-000000000000'
       OR v_source_ref = UUID '00000000-0000-0000-0000-000000000000'
       OR v_jti = UUID '00000000-0000-0000-0000-000000000000'
       OR v_operation::text IS DISTINCT FROM p_request->>'operation_id'
       OR v_source_ref::text IS DISTINCT FROM p_request->>'source_ref'
       OR v_jti::text IS DISTINCT FROM p_request->>'assertion_jti' THEN
        RAISE EXCEPTION 'evidence unlock preparation request is invalid' USING ERRCODE = '22023';
    END IF;
    v_scope := internal_command.normalize_evidence_unlock_scope_bindings(
        p_request->'scope_bindings'
    );
    SELECT source.* INTO v_source
      FROM internal_import.evidence_unlock_source AS source
     WHERE source.source_ref = v_source_ref
       AND EXISTS (
           SELECT 1 FROM jsonb_array_elements(v_scope) AS binding
            WHERE (binding->>'entity_ref')::uuid = source.entity_id
              AND (binding->>'business_unit_id')::uuid = source.business_unit_id
       );
    IF NOT FOUND THEN
        RAISE EXCEPTION 'reviewed evidence source was not found in granted scope'
            USING ERRCODE = 'LB004';
    END IF;
    IF EXISTS (
        SELECT 1 FROM internal_command.evidence_unlock_operation AS prior
         WHERE prior.assertion_jti = v_jti AND prior.operation_id <> v_operation
    ) THEN
        RAISE EXCEPTION 'evidence unlock assertion was reused' USING ERRCODE = 'LB005';
    END IF;
    SELECT * INTO v_operation_row
      FROM internal_command.evidence_unlock_operation AS operation
     WHERE operation.operation_id = v_operation FOR UPDATE;
    IF FOUND THEN
        IF v_operation_row.source_ref IS DISTINCT FROM v_source_ref
           OR v_operation_row.assertion_jti IS DISTINCT FROM v_jti
           OR v_operation_row.actor_ref IS DISTINCT FROM p_request->>'actor_ref'
           OR v_operation_row.authentication_generation IS DISTINCT FROM v_auth
           OR v_operation_row.workload_principal_ref
                IS DISTINCT FROM p_request->>'workload_principal_ref'
           OR v_operation_row.verified_san IS DISTINCT FROM p_request->>'verified_san'
           OR v_operation_row.policy_generation IS DISTINCT FROM p_request->>'policy_generation'
           OR v_operation_row.scope_bindings IS DISTINCT FROM v_scope THEN
            RAISE EXCEPTION 'evidence unlock operation conflicts with first request'
                USING ERRCODE = 'LB005';
        END IF;
    ELSE
        v_audit := public.append_audit_event(
            p_request->>'actor_ref', 'evidence.unlock.prepared',
            'prepare reviewed evidence unlock', 'ledgerbridge.evidence-unlock-command.v1',
            jsonb_build_object(
                'operation_id', v_operation, 'source_ref', v_source_ref,
                'assertion_jti', v_jti,
                'workload_principal_ref', p_request->>'workload_principal_ref'
            )
        );
        INSERT INTO internal_command.evidence_unlock_operation(
            operation_id, source_ref, assertion_jti, actor_ref,
            authentication_generation, workload_principal_ref, verified_san,
            policy_generation, scope_bindings, prepared_audit_event_id, created_at
        ) VALUES (
            v_operation, v_source_ref, v_jti, p_request->>'actor_ref', v_auth,
            p_request->>'workload_principal_ref', p_request->>'verified_san',
            p_request->>'policy_generation', v_scope, v_audit,
            (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
        );
    END IF;
    SELECT * INTO v_receipt FROM internal_command.evidence_unlock_receipt AS receipt
     WHERE receipt.operation_id = v_operation;
    IF FOUND THEN
        outcome := CASE v_receipt.outcome
            WHEN 'UNLOCKED' THEN 'REPLAY_UNLOCKED' ELSE 'REPLAY_REJECTED' END;
    ELSIF EXISTS (
        SELECT 1
          FROM internal_command.evidence_unlock_operation AS completed_operation
          JOIN internal_command.evidence_unlock_receipt AS completed_receipt
            ON completed_receipt.operation_id = completed_operation.operation_id
           AND completed_receipt.outcome = 'UNLOCKED'
         WHERE completed_operation.source_ref = v_source_ref
    ) THEN
        outcome := 'REPLAY_UNLOCKED';
    ELSE
        outcome := 'READY';
    END IF;
    SELECT * INTO v_blob
      FROM internal_read.resolve_active_evidence_blob(v_source.source_evidence_ref);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'reviewed evidence source blob was not found' USING ERRCODE = 'LB004';
    END IF;
    RETURN QUERY SELECT
        outcome, v_source.source_ref, v_source.source_evidence_ref, v_source.entity_id,
        v_blob.business_unit_ref, v_blob.object_ref, v_blob.plaintext_sha256,
        v_blob.plaintext_size, v_blob.ciphertext_sha256, v_blob.ciphertext_size,
        v_blob.storage_key, v_blob.chunk_size, v_blob.stream_header,
        v_blob.wrapped_key_generation, v_blob.wrapped_key_nonce,
        v_blob.wrapped_key_ciphertext;
END
$function$;

REVOKE ALL ON ALL TABLES IN SCHEMA internal_import
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker;
REVOKE ALL ON ALL TABLES IN SCHEMA internal_command
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker;
REVOKE ALL ON FUNCTION
    internal_import.register_evidence_unlock_source(jsonb),
    internal_command.evidence_unlock_reject_mutation(),
    internal_command.normalize_evidence_unlock_scope_bindings(jsonb),
    internal_command.require_evidence_unlock_operation(jsonb, text),
    internal_command.prepare_evidence_unlock(jsonb),
    internal_command.complete_evidence_unlock(jsonb),
    internal_command.reject_evidence_unlock(jsonb),
    internal_read.project_evidence_unlocks(jsonb, bigint),
    internal_read.list_candidates_base_as_of(
        uuid, uuid, varchar, bigint, bytea, timestamptz, uuid, integer
    ),
    internal_read.render_candidate_revision_base(uuid, integer),
    internal_read.list_candidates_as_of(
        uuid, uuid, varchar, bigint, bytea, timestamptz, uuid, integer
    ),
    internal_read.render_candidate_revision(uuid, integer)
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker;
DO $optional_acl$
DECLARE role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['ledgerbridge_app','ledgerbridge_backup'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format(
                'REVOKE ALL ON TABLE internal_import.evidence_unlock_source, '
                'internal_command.evidence_unlock_operation, '
                'internal_command.evidence_unlock_receipt, '
                'internal_command.evidence_unlock_output FROM %I', role_name
            );
            EXECUTE format(
                'REVOKE ALL ON FUNCTION '
                'internal_import.register_evidence_unlock_source(jsonb), '
                'internal_command.evidence_unlock_reject_mutation(), '
                'internal_command.normalize_evidence_unlock_scope_bindings(jsonb), '
                'internal_command.require_evidence_unlock_operation(jsonb,text), '
                'internal_command.prepare_evidence_unlock(jsonb), '
                'internal_command.complete_evidence_unlock(jsonb), '
                'internal_command.reject_evidence_unlock(jsonb), '
                'internal_read.project_evidence_unlocks(jsonb,bigint), '
                'internal_read.list_candidates_base_as_of('
                    'uuid,uuid,varchar,bigint,bytea,timestamptz,uuid,integer), '
                'internal_read.render_candidate_revision_base(uuid,integer), '
                'internal_read.list_candidates_as_of('
                    'uuid,uuid,varchar,bigint,bytea,timestamptz,uuid,integer), '
                'internal_read.render_candidate_revision(uuid,integer) FROM %I', role_name
            );
        END IF;
    END LOOP;
END
$optional_acl$;
GRANT EXECUTE ON FUNCTION internal_import.register_evidence_unlock_source(jsonb)
    TO ledgerbridge_worker;
GRANT EXECUTE ON FUNCTION internal_command.prepare_evidence_unlock(jsonb)
    TO ledgerbridge_api;
GRANT EXECUTE ON FUNCTION internal_command.complete_evidence_unlock(jsonb)
    TO ledgerbridge_api;
GRANT EXECUTE ON FUNCTION internal_command.reject_evidence_unlock(jsonb)
    TO ledgerbridge_api;
GRANT EXECUTE ON FUNCTION internal_read.list_candidates_as_of(
    uuid, uuid, varchar, bigint, bytea, timestamptz, uuid, integer
) TO ledgerbridge_reader;
"""


_DOWNGRADE_SQL = r"""
REVOKE ALL ON FUNCTION internal_read.list_candidates_as_of(
    uuid, uuid, varchar, bigint, bytea, timestamptz, uuid, integer
) FROM ledgerbridge_reader;
REVOKE ALL ON FUNCTION internal_import.register_evidence_unlock_source(jsonb)
    FROM ledgerbridge_worker;
REVOKE ALL ON FUNCTION internal_command.prepare_evidence_unlock(jsonb),
    internal_command.complete_evidence_unlock(jsonb),
    internal_command.reject_evidence_unlock(jsonb) FROM ledgerbridge_api;
DROP FUNCTION internal_read.list_candidates_as_of(
    uuid, uuid, varchar, bigint, bytea, timestamptz, uuid, integer
);
DROP FUNCTION internal_read.render_candidate_revision(uuid, integer);
DROP FUNCTION internal_read.project_evidence_unlocks(jsonb, bigint);
ALTER FUNCTION internal_read.list_candidates_base_as_of(
    uuid, uuid, varchar, bigint, bytea, timestamptz, uuid, integer
) RENAME TO list_candidates_as_of;
ALTER FUNCTION internal_read.render_candidate_revision_base(uuid, integer)
    RENAME TO render_candidate_revision;
GRANT EXECUTE ON FUNCTION internal_read.list_candidates_as_of(
    uuid, uuid, varchar, bigint, bytea, timestamptz, uuid, integer
) TO ledgerbridge_reader;
DROP FUNCTION internal_command.reject_evidence_unlock(jsonb);
DROP FUNCTION internal_command.complete_evidence_unlock(jsonb);
DROP FUNCTION internal_command.prepare_evidence_unlock(jsonb);
DROP FUNCTION internal_command.require_evidence_unlock_operation(jsonb, text);
DROP FUNCTION internal_import.register_evidence_unlock_source(jsonb);
DROP TRIGGER evidence_unlock_output_append_only
    ON internal_command.evidence_unlock_output;
DROP TRIGGER evidence_unlock_receipt_append_only
    ON internal_command.evidence_unlock_receipt;
DROP TRIGGER evidence_unlock_operation_append_only
    ON internal_command.evidence_unlock_operation;
DROP TRIGGER evidence_unlock_source_append_only
    ON internal_import.evidence_unlock_source;
DROP TABLE internal_command.evidence_unlock_output;
DROP TABLE internal_command.evidence_unlock_receipt;
DROP TABLE internal_command.evidence_unlock_operation;
DROP TABLE internal_import.evidence_unlock_source;
DROP FUNCTION internal_command.normalize_evidence_unlock_scope_bindings(jsonb);
DROP FUNCTION internal_command.evidence_unlock_reject_mutation();
"""
