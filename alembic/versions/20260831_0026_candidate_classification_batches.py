# ruff: noqa: E501

"""Install atomic, append-only Candidate classification batches.

Revision ID: 20260831_0026
Revises: 20260830_0025

The API role receives one JSONB SECURITY DEFINER command.  It validates and
locks every member in UUID order before calling the existing per-Candidate
decision command.  PostgreSQL function calls share the caller transaction, so
any stale, unauthorized, malformed, or drifted member rolls back all member
events, receipts, and the batch receipt.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260831_0026"
down_revision: str | None = "20260830_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE internal_command.candidate_classification_batch_receipt (
            operation_id uuid PRIMARY KEY,
            group_ref varchar(35) NOT NULL,
            accounting_month date NOT NULL,
            source_candidate_id uuid NOT NULL
                REFERENCES public.candidate(id) ON DELETE RESTRICT,
            target_business_unit_ref varchar(100) NOT NULL,
            target_category_code varchar(100) NOT NULL,
            actor_ref varchar(200) NOT NULL,
            workload_principal_ref varchar(200) NOT NULL,
            verified_san varchar(200) NOT NULL,
            request_fingerprint bytea NOT NULL,
            member_operation_ids uuid[] NOT NULL,
            decided_at timestamptz NOT NULL,
            audit_event_id uuid NOT NULL UNIQUE
                REFERENCES public.audit_event(id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT candidate_classification_batch_group_shape
                CHECK (group_ref ~ '^cg_[0-9a-f]{32}$'),
            CONSTRAINT candidate_classification_batch_month_first
                CHECK (accounting_month = date_trunc('month', accounting_month)::date),
            CONSTRAINT candidate_classification_batch_target_not_blank
                CHECK (btrim(target_business_unit_ref) <> '' AND btrim(target_category_code) <> ''),
            CONSTRAINT candidate_classification_batch_actor_not_blank
                CHECK (btrim(actor_ref) <> '' AND btrim(workload_principal_ref) <> ''),
            CONSTRAINT candidate_classification_batch_san_shape
                CHECK (verified_san ~ '^spiffe://ledgerbridge(\.test|\.local)?/[a-z0-9/_-]+$'),
            CONSTRAINT candidate_classification_batch_fingerprint_length
                CHECK (octet_length(request_fingerprint) = 32),
            CONSTRAINT candidate_classification_batch_member_count
                CHECK (cardinality(member_operation_ids) BETWEEN 2 AND 100)
        );

        CREATE TABLE internal_command.candidate_classification_batch_member (
            batch_operation_id uuid NOT NULL
                REFERENCES internal_command.candidate_classification_batch_receipt(operation_id)
                ON DELETE RESTRICT,
            ordinal integer NOT NULL,
            candidate_id uuid NOT NULL REFERENCES public.candidate(id) ON DELETE RESTRICT,
            expected_revision integer NOT NULL,
            member_operation_id uuid NOT NULL UNIQUE
                REFERENCES internal_command.candidate_decision_receipt(operation_id)
                ON DELETE RESTRICT,
            CONSTRAINT candidate_classification_batch_member_pk
                PRIMARY KEY (batch_operation_id, ordinal),
            CONSTRAINT candidate_classification_batch_member_candidate_unique
                UNIQUE (batch_operation_id, candidate_id),
            CONSTRAINT candidate_classification_batch_member_ordinal
                CHECK (ordinal BETWEEN 1 AND 100),
            CONSTRAINT candidate_classification_batch_member_revision
                CHECK (expected_revision >= 1)
        );

        CREATE TABLE internal_command.candidate_classification_batch_assertion_use (
            assertion_jti uuid PRIMARY KEY,
            operation_id uuid NOT NULL
                REFERENCES internal_command.candidate_classification_batch_receipt(operation_id)
                ON DELETE RESTRICT,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE FUNCTION internal_command.reject_cross_surface_assertion_reuse()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended('candidate-assertion:' || NEW.assertion_jti::text, 0)
            );
            IF TG_TABLE_NAME = 'candidate_assertion_use' AND EXISTS (
                SELECT 1
                  FROM internal_command.candidate_classification_batch_assertion_use
                 WHERE assertion_jti = NEW.assertion_jti
            ) THEN
                RAISE EXCEPTION 'assertion JTI was reused across command surfaces'
                    USING ERRCODE = 'LB001';
            ELSIF TG_TABLE_NAME = 'candidate_classification_batch_assertion_use' AND EXISTS (
                SELECT 1 FROM internal_command.candidate_assertion_use
                 WHERE assertion_jti = NEW.assertion_jti
            ) THEN
                RAISE EXCEPTION 'assertion JTI was reused across command surfaces'
                    USING ERRCODE = 'LB001';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE TRIGGER candidate_assertion_cross_surface_guard
            BEFORE INSERT ON internal_command.candidate_assertion_use
            FOR EACH ROW EXECUTE FUNCTION
                internal_command.reject_cross_surface_assertion_reuse();
        CREATE TRIGGER candidate_classification_batch_assertion_cross_surface_guard
            BEFORE INSERT ON internal_command.candidate_classification_batch_assertion_use
            FOR EACH ROW EXECUTE FUNCTION
                internal_command.reject_cross_surface_assertion_reuse();

        CREATE TRIGGER candidate_classification_batch_receipt_append_only
            BEFORE UPDATE OR DELETE
            ON internal_command.candidate_classification_batch_receipt
            FOR EACH ROW EXECUTE FUNCTION internal_command.reject_mutation();
        CREATE TRIGGER candidate_classification_batch_member_append_only
            BEFORE UPDATE OR DELETE
            ON internal_command.candidate_classification_batch_member
            FOR EACH ROW EXECUTE FUNCTION internal_command.reject_mutation();
        CREATE TRIGGER candidate_classification_batch_assertion_append_only
            BEFORE UPDATE OR DELETE
            ON internal_command.candidate_classification_batch_assertion_use
            FOR EACH ROW EXECUTE FUNCTION internal_command.reject_mutation();

        CREATE FUNCTION internal_command.render_candidate_classification_batch_receipt(
            p_operation_id uuid, p_replayed boolean
        ) RETURNS jsonb
        LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE v_receipt jsonb;
        BEGIN
            SELECT jsonb_build_object(
                'contract_version', 'ledgerbridge.classification-batch.v1',
                'operation_id', batch.operation_id,
                'replayed', p_replayed,
                'group_ref', batch.group_ref,
                'accounting_month', to_char(batch.accounting_month, 'YYYY-MM'),
                'source_candidate_ref', batch.source_candidate_id,
                'target', jsonb_build_object(
                    'business_unit_ref', batch.target_business_unit_ref,
                    'category_code', batch.target_category_code
                ),
                'results', (
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'candidate_ref', member.candidate_id,
                            'operation_id', member.member_operation_id,
                            'status', CASE WHEN p_replayed THEN 'REPLAYED' ELSE 'APPLIED' END,
                            'candidate', internal_read.render_candidate_revision(
                                member.candidate_id, decision.final_revision
                            ),
                            'events', (
                                SELECT jsonb_agg(
                                    internal_read.render_candidate_event(event_id)
                                    ORDER BY event_ordinal
                                )
                                FROM unnest(decision.event_operation_ids) WITH ORDINALITY
                                     AS event_ids(event_id, event_ordinal)
                            )
                        ) ORDER BY member.ordinal
                    )
                    FROM internal_command.candidate_classification_batch_member AS member
                    JOIN internal_command.candidate_decision_receipt AS decision
                      ON decision.operation_id = member.member_operation_id
                    WHERE member.batch_operation_id = batch.operation_id
                )
            ) INTO v_receipt
            FROM internal_command.candidate_classification_batch_receipt AS batch
            WHERE batch.operation_id = p_operation_id;
            IF v_receipt IS NULL THEN
                RAISE EXCEPTION 'candidate classification batch receipt is not visible'
                    USING ERRCODE = 'LB004';
            END IF;
            RETURN v_receipt;
        END
        $function$;

        CREATE FUNCTION internal_command.apply_candidate_classification_batch(
            p_request jsonb
        ) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_operation_id uuid;
            v_assertion_jti uuid;
            v_actor_ref varchar(200);
            v_workload_ref varchar(200);
            v_verified_san varchar(200);
            v_authorized_entity_id uuid;
            v_group_ref varchar(35);
            v_accounting_month date;
            v_source_candidate_id uuid;
            v_target_business_unit_ref varchar(100);
            v_target_category_code varchar(100);
            v_decided_at timestamptz;
            v_members jsonb;
            v_member jsonb;
            v_member_refs uuid[];
            v_member_operations uuid[];
            v_normalized_members jsonb;
            v_request_fingerprint bytea;
            v_receipt internal_command.candidate_classification_batch_receipt%ROWTYPE;
            v_existing_operation uuid;
            v_member_receipt jsonb;
            v_audit_event_id uuid;
            v_count integer;
            v_ordinal integer := 0;
            v_source_summary_parts text[];
            v_source_source public.candidate_source%ROWTYPE;
            v_source_counterparty varchar(99);
        BEGIN
            IF p_request IS NULL OR jsonb_typeof(p_request) <> 'object' THEN
                RAISE EXCEPTION 'invalid candidate classification batch request'
                    USING ERRCODE = 'LB003';
            END IF;
            BEGIN
                v_operation_id := (p_request->>'operation_id')::uuid;
                v_assertion_jti := (p_request->>'assertion_jti')::uuid;
                v_actor_ref := p_request->>'actor_ref';
                v_workload_ref := p_request->>'workload_principal_ref';
                v_verified_san := p_request->>'verified_san';
                v_authorized_entity_id := (p_request->>'authorized_entity_id')::uuid;
                v_group_ref := p_request->>'group_ref';
                v_accounting_month := to_date(p_request->>'accounting_month' || '-01', 'YYYY-MM-DD');
                v_source_candidate_id := (p_request->>'source_candidate_ref')::uuid;
                v_target_business_unit_ref := p_request#>>'{target,business_unit_ref}';
                v_target_category_code := p_request#>>'{target,category_code}';
                v_decided_at := (p_request->>'decided_at')::timestamptz;
                v_members := p_request->'members';
            EXCEPTION WHEN invalid_text_representation OR datetime_field_overflow
                           OR numeric_value_out_of_range THEN
                RAISE EXCEPTION 'invalid candidate classification batch request'
                    USING ERRCODE = 'LB003';
            END;
            IF v_operation_id IS NULL OR v_assertion_jti IS NULL
               OR v_actor_ref IS NULL OR btrim(v_actor_ref) = ''
               OR v_workload_ref IS NULL OR btrim(v_workload_ref) = ''
               OR v_verified_san IS NULL
               OR v_verified_san !~ '^spiffe://ledgerbridge(\.test|\.local)?/[a-z0-9/_-]+$'
               OR v_authorized_entity_id IS NULL
               OR v_group_ref IS NULL OR v_group_ref !~ '^cg_[0-9a-f]{32}$'
               OR p_request->>'accounting_month' !~ '^[0-9]{4}-(0[1-9]|1[0-2])$'
               OR v_source_candidate_id IS NULL
               OR v_target_business_unit_ref IS NULL OR btrim(v_target_business_unit_ref) = ''
               OR v_target_category_code IS NULL OR btrim(v_target_category_code) = ''
               OR v_decided_at IS NULL OR jsonb_typeof(v_members) <> 'array'
               OR jsonb_array_length(v_members) NOT BETWEEN 2 AND 100 THEN
                RAISE EXCEPTION 'invalid candidate classification batch request'
                    USING ERRCODE = 'LB003';
            END IF;

            SELECT array_agg((member->>'candidate_ref')::uuid ORDER BY member->>'candidate_ref'),
                   array_agg((member->>'operation_id')::uuid ORDER BY member->>'candidate_ref'),
                   jsonb_agg(member - 'assertion_jti' ORDER BY member->>'candidate_ref')
              INTO v_member_refs, v_member_operations, v_normalized_members
              FROM jsonb_array_elements(v_members) AS members(member);
            IF cardinality(v_member_refs) IS DISTINCT FROM (
                    SELECT count(DISTINCT value)::integer FROM unnest(v_member_refs) AS refs(value)
               ) OR cardinality(v_member_operations) IS DISTINCT FROM (
                    SELECT count(DISTINCT value)::integer
                      FROM unnest(v_member_operations) AS operations(value)
               ) OR NOT v_source_candidate_id = ANY(v_member_refs) THEN
                RAISE EXCEPTION 'candidate classification batch members are invalid'
                    USING ERRCODE = 'LB003';
            END IF;

            v_request_fingerprint := public.digest(convert_to(jsonb_build_object(
                'group_ref', v_group_ref,
                'accounting_month', to_char(v_accounting_month, 'YYYY-MM'),
                'source_candidate_ref', v_source_candidate_id,
                'target', p_request->'target',
                'acknowledged_risk_codes', coalesce(
                    p_request->'acknowledged_risk_codes', '[]'::jsonb
                ),
                'members', v_normalized_members
            )::text, 'UTF8'), 'sha256');
            PERFORM pg_advisory_xact_lock(
                hashtextextended('classification-batch:' || v_operation_id::text, 0)
            );
            IF EXISTS (
                SELECT 1 FROM internal_command.candidate_assertion_use
                 WHERE assertion_jti = v_assertion_jti
            ) THEN
                RAISE EXCEPTION 'assertion JTI was reused for another operation'
                    USING ERRCODE = 'LB001';
            END IF;
            SELECT operation_id INTO v_existing_operation
              FROM internal_command.candidate_classification_batch_assertion_use
             WHERE assertion_jti = v_assertion_jti;
            IF FOUND AND v_existing_operation IS DISTINCT FROM v_operation_id THEN
                RAISE EXCEPTION 'assertion JTI was reused for another operation'
                    USING ERRCODE = 'LB001';
            END IF;
            SELECT * INTO v_receipt
              FROM internal_command.candidate_classification_batch_receipt
             WHERE operation_id = v_operation_id;
            IF FOUND THEN
                IF v_receipt.group_ref IS DISTINCT FROM v_group_ref
                   OR v_receipt.accounting_month IS DISTINCT FROM v_accounting_month
                   OR v_receipt.source_candidate_id IS DISTINCT FROM v_source_candidate_id
                   OR v_receipt.target_business_unit_ref IS DISTINCT FROM v_target_business_unit_ref
                   OR v_receipt.target_category_code IS DISTINCT FROM v_target_category_code
                   OR v_receipt.actor_ref IS DISTINCT FROM v_actor_ref
                   OR v_receipt.workload_principal_ref IS DISTINCT FROM v_workload_ref
                   OR v_receipt.verified_san IS DISTINCT FROM v_verified_san
                   OR v_receipt.request_fingerprint IS DISTINCT FROM v_request_fingerprint
                   OR v_receipt.member_operation_ids IS DISTINCT FROM v_member_operations THEN
                    RAISE EXCEPTION 'batch operation ID was reused with different content or actor'
                        USING ERRCODE = 'LB001';
                END IF;
                INSERT INTO internal_command.candidate_classification_batch_assertion_use(
                    assertion_jti, operation_id
                ) VALUES (v_assertion_jti, v_operation_id) ON CONFLICT DO NOTHING;
                SELECT operation_id INTO v_existing_operation
                  FROM internal_command.candidate_classification_batch_assertion_use
                 WHERE assertion_jti = v_assertion_jti;
                IF v_existing_operation IS DISTINCT FROM v_operation_id THEN
                    RAISE EXCEPTION 'assertion JTI was reused for another operation'
                        USING ERRCODE = 'LB001';
                END IF;
                RETURN internal_command.render_candidate_classification_batch_receipt(
                    v_operation_id, true
                );
            END IF;

            -- Establish one deterministic lock order before any member command writes.
            PERFORM candidate.id
              FROM public.candidate AS candidate
             WHERE candidate.id = ANY(v_member_refs)
             ORDER BY candidate.id
             FOR UPDATE;
            GET DIAGNOSTICS v_count = ROW_COUNT;
            IF v_count IS DISTINCT FROM cardinality(v_member_refs) THEN
                RAISE EXCEPTION 'candidate classification batch scope changed'
                    USING ERRCODE = 'LB002';
            END IF;

            SELECT * INTO v_source_source
              FROM public.candidate_source WHERE candidate_id = v_source_candidate_id;
            SELECT counterparty_ref INTO v_source_counterparty
              FROM public.candidate_counterparty WHERE candidate_id = v_source_candidate_id;
            SELECT regexp_split_to_array(
                       replace(current.summary, chr(65372), '|'), '[[:space:]]*\|[[:space:]]*'
                   ) INTO v_source_summary_parts
              FROM public.candidate_revision AS current
             WHERE current.candidate_id = v_source_candidate_id
               AND current.revision = (
                   SELECT max(revision) FROM public.candidate_revision
                    WHERE candidate_id = v_source_candidate_id
               );
            IF cardinality(v_source_summary_parts) <> 7 THEN
                RAISE EXCEPTION 'candidate classification group key drifted'
                    USING ERRCODE = 'LB002';
            END IF;

            -- Complete the read-only preflight for every member before the first write.
            FOR v_member IN SELECT value FROM jsonb_array_elements(v_members)
            LOOP
                BEGIN
                    SELECT count(*) INTO v_count
                      FROM public.candidate AS candidate
                      JOIN public.candidate_source AS source
                        ON source.candidate_id = candidate.id
                      JOIN public.candidate_revision AS current
                        ON current.candidate_id = candidate.id
                       AND current.revision = (v_member->>'expected_revision')::integer
                      LEFT JOIN public.candidate_counterparty AS counterparty
                        ON counterparty.candidate_id = candidate.id
                     WHERE candidate.id = (v_member->>'candidate_ref')::uuid
                       AND candidate.entity_id = v_authorized_entity_id
                       AND current.revision = (
                           SELECT max(revision) FROM public.candidate_revision
                            WHERE candidate_id = candidate.id
                       )
                       AND current.status = 'PENDING'
                       AND current.accounting_month = v_accounting_month
                       AND current.confidence_basis_points >= 9000
                       AND current.business_unit_id IS NOT DISTINCT FROM
                           (v_member->>'current_business_unit_id')::uuid
                       AND source.source_system_id = v_source_source.source_system_id
                       AND source.ingest_channel_id = v_source_source.ingest_channel_id
                       AND current.currency = 'CNY'
                       AND NOT EXISTS (
                           SELECT 1 FROM public.candidate_blocker AS blocker
                            WHERE blocker.candidate_id = candidate.id
                              AND blocker.revision = current.revision
                       )
                       AND (
                           (v_source_counterparty IS NOT NULL
                            AND counterparty.counterparty_ref = v_source_counterparty)
                           OR (v_source_counterparty IS NULL
                               AND counterparty.counterparty_ref IS NULL)
                       )
                       AND cardinality(regexp_split_to_array(
                               replace(current.summary, chr(65372), '|'),
                               '[[:space:]]*\|[[:space:]]*'
                           )) = 7
                       AND lower(btrim((regexp_split_to_array(
                               replace(current.summary, chr(65372), '|'),
                               '[[:space:]]*\|[[:space:]]*'
                           ))[1])) = lower(btrim(v_source_summary_parts[1]))
                       AND lower(btrim((regexp_split_to_array(
                               replace(current.summary, chr(65372), '|'),
                               '[[:space:]]*\|[[:space:]]*'
                           ))[3])) = lower(btrim(v_source_summary_parts[3]))
                       AND lower(btrim((regexp_split_to_array(
                               replace(current.summary, chr(65372), '|'),
                               '[[:space:]]*\|[[:space:]]*'
                           ))[4])) = lower(btrim(v_source_summary_parts[4]))
                       AND (
                           v_source_counterparty IS NOT NULL
                           OR lower(btrim((regexp_split_to_array(
                               replace(current.summary, chr(65372), '|'),
                               '[[:space:]]*\|[[:space:]]*'
                           ))[5])) = lower(btrim(v_source_summary_parts[5]))
                       )
                       AND lower(btrim((regexp_split_to_array(
                               replace(current.summary, chr(65372), '|'),
                               '[[:space:]]*\|[[:space:]]*'
                           ))[6])) = lower(btrim(v_source_summary_parts[6]))
                       AND lower(btrim((regexp_split_to_array(
                               replace(current.summary, chr(65372), '|'),
                               '[[:space:]]*\|[[:space:]]*'
                           ))[7])) = lower(btrim(v_source_summary_parts[7]));
                EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                    RAISE EXCEPTION 'candidate classification batch member is invalid'
                        USING ERRCODE = 'LB003';
                END;
                IF v_count IS DISTINCT FROM 1 THEN
                    RAISE EXCEPTION 'candidate classification batch member or group key drifted'
                        USING ERRCODE = 'LB002';
                END IF;
            END LOOP;

            -- Calls remain inside this transaction; a later error rolls back earlier calls.
            FOR v_member IN
                SELECT value FROM jsonb_array_elements(v_members)
                ORDER BY value->>'candidate_ref'
            LOOP
                v_member_receipt := internal_command.apply_candidate_decision(
                    (v_member->>'operation_id')::uuid,
                    (v_member->>'assertion_jti')::uuid,
                    (v_member->>'candidate_ref')::uuid,
                    v_actor_ref,
                    v_workload_ref,
                    v_verified_san,
                    v_authorized_entity_id,
                    (v_member->>'current_business_unit_id')::uuid,
                    (v_member->>'target_business_unit_id')::uuid,
                    (v_member->>'decision')::varchar(32),
                    (v_member->>'expected_revision')::integer,
                    (v_member->>'reason')::varchar(1000),
                    (v_member->>'set_business_unit')::boolean,
                    (v_member->>'business_unit_ref')::varchar(100),
                    (v_member->>'set_category')::boolean,
                    (v_member->>'category_code')::varchar(100),
                    false, NULL, false, NULL, NULL, v_decided_at
                );
                IF v_member_receipt IS NULL THEN
                    RAISE EXCEPTION 'candidate classification member returned no receipt'
                        USING ERRCODE = 'LB003';
                END IF;
            END LOOP;

            v_audit_event_id := public.append_audit_event(
                v_actor_ref,
                'candidate.classification.batch',
                'explicit similar-transaction classification batch',
                'ledgerbridge.classification-batch.v1',
                jsonb_build_object(
                    'operation_id', v_operation_id,
                    'group_ref', v_group_ref,
                    'accounting_month', to_char(v_accounting_month, 'YYYY-MM'),
                    'source_candidate_ref', v_source_candidate_id,
                    'target_business_unit_ref', v_target_business_unit_ref,
                    'target_category_code', v_target_category_code,
                    'member_operation_ids', v_member_operations,
                    'request_fingerprint', encode(v_request_fingerprint, 'hex')
                )
            );
            INSERT INTO internal_command.candidate_classification_batch_receipt (
                operation_id, group_ref, accounting_month, source_candidate_id,
                target_business_unit_ref, target_category_code, actor_ref,
                workload_principal_ref, verified_san, request_fingerprint,
                member_operation_ids, decided_at, audit_event_id
            ) VALUES (
                v_operation_id, v_group_ref, v_accounting_month, v_source_candidate_id,
                v_target_business_unit_ref, v_target_category_code, v_actor_ref,
                v_workload_ref, v_verified_san, v_request_fingerprint,
                v_member_operations, v_decided_at, v_audit_event_id
            );
            FOR v_member IN
                SELECT value FROM jsonb_array_elements(v_members)
                ORDER BY value->>'candidate_ref'
            LOOP
                v_ordinal := v_ordinal + 1;
                INSERT INTO internal_command.candidate_classification_batch_member (
                    batch_operation_id, ordinal, candidate_id,
                    expected_revision, member_operation_id
                ) VALUES (
                    v_operation_id, v_ordinal, (v_member->>'candidate_ref')::uuid,
                    (v_member->>'expected_revision')::integer,
                    (v_member->>'operation_id')::uuid
                );
            END LOOP;
            INSERT INTO internal_command.candidate_classification_batch_assertion_use(
                assertion_jti, operation_id
            ) VALUES (v_assertion_jti, v_operation_id);
            RETURN internal_command.render_candidate_classification_batch_receipt(
                v_operation_id, false
            );
        END
        $function$;

        REVOKE ALL ON TABLE
            internal_command.candidate_classification_batch_receipt,
            internal_command.candidate_classification_batch_member,
            internal_command.candidate_classification_batch_assertion_use
            FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker;
        REVOKE ALL ON FUNCTION
            internal_command.render_candidate_classification_batch_receipt(uuid, boolean),
            internal_command.apply_candidate_classification_batch(jsonb),
            internal_command.reject_cross_surface_assertion_reuse()
            FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker;
        GRANT EXECUTE ON FUNCTION
            internal_command.apply_candidate_classification_batch(jsonb)
            TO ledgerbridge_api;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $guard$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM internal_command.candidate_classification_batch_receipt
            ) OR EXISTS (
                SELECT 1 FROM internal_command.candidate_classification_batch_member
            ) OR EXISTS (
                SELECT 1 FROM internal_command.candidate_classification_batch_assertion_use
            ) THEN
                RAISE EXCEPTION
                    'candidate classification batch facts prevent destructive downgrade';
            END IF;
        END
        $guard$;
        REVOKE ALL ON FUNCTION
            internal_command.apply_candidate_classification_batch(jsonb)
            FROM ledgerbridge_api;
        DROP FUNCTION internal_command.apply_candidate_classification_batch(jsonb);
        DROP FUNCTION internal_command.render_candidate_classification_batch_receipt(uuid, boolean);
        DROP TRIGGER candidate_classification_batch_assertion_cross_surface_guard
            ON internal_command.candidate_classification_batch_assertion_use;
        DROP TRIGGER candidate_assertion_cross_surface_guard
            ON internal_command.candidate_assertion_use;
        DROP FUNCTION internal_command.reject_cross_surface_assertion_reuse();
        DROP TRIGGER candidate_classification_batch_assertion_append_only
            ON internal_command.candidate_classification_batch_assertion_use;
        DROP TRIGGER candidate_classification_batch_member_append_only
            ON internal_command.candidate_classification_batch_member;
        DROP TRIGGER candidate_classification_batch_receipt_append_only
            ON internal_command.candidate_classification_batch_receipt;
        DROP TABLE internal_command.candidate_classification_batch_assertion_use;
        DROP TABLE internal_command.candidate_classification_batch_member;
        DROP TABLE internal_command.candidate_classification_batch_receipt;
        """
    )
