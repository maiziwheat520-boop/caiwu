# ruff: noqa: E501

"""Install atomic, append-only Candidate classification batches.

Revision ID: 20260831_0026
Revises: 20260830_0025

The API role receives replay and apply JSONB SECURITY DEFINER commands. The
database independently recomputes the versioned classification key and risk
signature for every locked member before any decision write.
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
        CREATE FUNCTION internal_command.candidate_classification_risk_signature(
            p_candidate_id uuid, p_revision integer
        ) RETURNS varchar(64)[]
        LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_source_system varchar(64);
            v_category varchar(100);
            v_summary varchar(500);
            v_parts text[];
            v_transaction_type text;
            v_counterparty text;
            v_funding text;
            v_status text;
            v_counterparty_class varchar(32);
            v_satisfied varchar(64)[] := ARRAY[]::varchar(64)[];
            v_risks varchar(64)[] := ARRAY[]::varchar(64)[];
            v_transfer_text text;
            v_platform_internal boolean;
        BEGIN
            SELECT source.source_system_id, current.category_code_snapshot, current.summary
              INTO v_source_system, v_category, v_summary
              FROM public.candidate_source AS source
              JOIN public.candidate_revision AS current
                ON current.candidate_id = source.candidate_id
               AND current.revision = p_revision
             WHERE source.candidate_id = p_candidate_id;
            IF NOT FOUND THEN RETURN NULL; END IF;
            SELECT coalesce(array_agg(DISTINCT link.risk_code ORDER BY link.risk_code),
                            ARRAY[]::varchar(64)[])
              INTO v_satisfied
              FROM public.candidate_evidence_link AS link
             WHERE link.subject_candidate_id = p_candidate_id
                OR (link.risk_code = 'REVERSAL_MATCH_REQUIRED'
                    AND link.evidence_candidate_id = p_candidate_id);
            SELECT fact.counterparty_class
              INTO v_counterparty_class
              FROM public.candidate_counterparty AS candidate_counterparty
              JOIN public.counterparty_classification AS fact
                ON fact.entity_id = candidate_counterparty.entity_id
               AND fact.counterparty_ref = candidate_counterparty.counterparty_ref
             WHERE candidate_counterparty.candidate_id = p_candidate_id
             ORDER BY fact.classification_revision DESC LIMIT 1;
            IF v_source_system IN ('hotel_photo_reconciliation', 'hotel_bill_ocr')
               AND NOT ('HOTEL_PAYOUT_STATEMENT_REQUIRED' = ANY(v_satisfied)) THEN
                v_risks := array_append(v_risks, 'HOTEL_PAYOUT_STATEMENT_REQUIRED');
            END IF;
            IF v_category IN ('WECHAT_TRANSACTION_REVIEW', 'ALIPAY_TRANSACTION_REVIEW') THEN
                -- Deliberately mirrors review_risk.py's canonical delimiter.
                v_parts := string_to_array(v_summary, ' | ');
                v_transaction_type := CASE WHEN cardinality(v_parts) > 3 THEN v_parts[4] ELSE v_summary END;
                v_counterparty := CASE WHEN cardinality(v_parts) > 4 THEN v_parts[5] ELSE '' END;
                v_funding := CASE WHEN cardinality(v_parts) > 5 THEN v_parts[6] ELSE '' END;
                v_status := CASE WHEN cardinality(v_parts) > 6 THEN v_parts[7] ELSE '' END;
                v_transfer_text := concat_ws(' ', v_transaction_type, v_counterparty, v_funding);
                IF v_funding ~ '(银行|储蓄卡|信用卡)'
                   AND NOT ('FUNDING_STATEMENT_REQUIRED' = ANY(v_satisfied)) THEN
                    v_risks := array_append(v_risks, 'FUNDING_STATEMENT_REQUIRED');
                END IF;
                IF v_status ~ '(付款中|生成中|未出账|交易关闭|付款异常)'
                   AND NOT ('UNSETTLED_TRANSACTION' = ANY(v_satisfied)) THEN
                    v_risks := array_append(v_risks, 'UNSETTLED_TRANSACTION');
                END IF;
                IF (v_transaction_type ~ '(退款|冲正|撤销)' OR v_status ~ '(退款|冲正|撤销)')
                   AND NOT ('REVERSAL_MATCH_REQUIRED' = ANY(v_satisfied)) THEN
                    v_risks := array_append(v_risks, 'REVERSAL_MATCH_REQUIRED');
                END IF;
                v_platform_internal := v_transaction_type ~ '(余额互转|充值|赎回|花呗还款)'
                    AND v_transfer_text ~ '(账户余额|余额宝|花呗|零钱|零钱通)'
                    AND v_funding !~ '(银行|储蓄卡|信用卡)';
                IF v_transfer_text ~ '(转账|提现|投资理财|信用卡还款|信用借还|余额互转)'
                   AND NOT v_platform_internal THEN
                    IF v_counterparty_class IN ('self_managed', 'related_party')
                       AND NOT ('RELATED_ACCOUNT_STATEMENT_REQUIRED' = ANY(v_satisfied)) THEN
                        v_risks := array_append(v_risks, 'RELATED_ACCOUNT_STATEMENT_REQUIRED');
                    ELSIF NOT ('TRANSFER_REVIEW_REQUIRED' = ANY(v_satisfied)) THEN
                        v_risks := array_append(v_risks, 'TRANSFER_REVIEW_REQUIRED');
                    END IF;
                END IF;
            END IF;
            SELECT coalesce(array_agg(DISTINCT risk ORDER BY risk), ARRAY[]::varchar(64)[])
              INTO v_risks FROM unnest(v_risks) AS risks(risk);
            RETURN v_risks;
        END
        $function$;

        CREATE FUNCTION internal_command.candidate_classification_group_key(
            p_candidate_id uuid, p_revision integer
        ) RETURNS jsonb
        LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_entity_id uuid;
            v_source_system varchar(64);
            v_ingest_channel varchar(64);
            v_source_kind varchar(64);
            v_summary varchar(500);
            v_amount bigint;
            v_currency varchar(3);
            v_parts text[];
            v_platform text;
            v_direction text;
            v_type text;
            v_counterparty text;
            v_counterparty_ref varchar(99);
            v_counterparty_key text;
            v_counterparty_basis text;
            v_funding text;
            v_status text;
            v_risks varchar(64)[];
            v_canonical text;
            v_group_ref varchar(35);
        BEGIN
            SELECT candidate.entity_id, source.source_system_id, source.ingest_channel_id,
                   current.summary, current.amount_minor, current.currency,
                   counterparty.counterparty_ref
              INTO v_entity_id, v_source_system, v_ingest_channel, v_summary,
                   v_amount, v_currency, v_counterparty_ref
              FROM public.candidate AS candidate
              JOIN public.candidate_source AS source ON source.candidate_id = candidate.id
              JOIN public.candidate_revision AS current
                ON current.candidate_id = candidate.id AND current.revision = p_revision
              LEFT JOIN public.candidate_counterparty AS counterparty
                ON counterparty.candidate_id = candidate.id
             WHERE candidate.id = p_candidate_id;
            IF NOT FOUND OR v_amount IS NULL OR v_currency <> 'CNY' THEN RETURN NULL; END IF;
            v_parts := regexp_split_to_array(replace(v_summary, chr(65372), '|'), '[[:space:]]*\|[[:space:]]*');
            IF cardinality(v_parts) <> 7
               OR EXISTS (SELECT 1 FROM unnest(v_parts) AS parts(part) WHERE btrim(part) = '')
               OR v_parts[2] !~ '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$' THEN
                RETURN NULL;
            END IF;
            v_platform := lower(regexp_replace(btrim(v_parts[1]), '[[:space:]]+', ' ', 'g'));
            v_type := lower(regexp_replace(btrim(v_parts[4]), '[[:space:]]+', ' ', 'g'));
            v_counterparty := lower(regexp_replace(btrim(v_parts[5]), '[[:space:]]+', ' ', 'g'));
            v_funding := lower(regexp_replace(btrim(v_parts[6]), '[[:space:]]+', ' ', 'g'));
            v_status := lower(regexp_replace(btrim(v_parts[7]), '[[:space:]]+', ' ', 'g'));
            v_direction := CASE lower(regexp_replace(btrim(v_parts[3]), '[[:space:]]+', ' ', 'g'))
                WHEN '收入' THEN 'INFLOW' WHEN '退款收入' THEN 'INFLOW'
                WHEN 'income' THEN 'INFLOW' WHEN 'inflow' THEN 'INFLOW'
                WHEN '支出' THEN 'OUTFLOW' WHEN 'expense' THEN 'OUTFLOW'
                WHEN 'outflow' THEN 'OUTFLOW' WHEN '不计收支' THEN 'NEUTRAL'
                WHEN 'neutral' THEN 'NEUTRAL' ELSE NULL END;
            IF v_direction IS NULL OR (v_direction = 'INFLOW' AND v_amount < 0)
               OR (v_direction = 'OUTFLOW' AND v_amount > 0)
               OR (v_direction = 'NEUTRAL' AND v_amount <> 0) THEN RETURN NULL; END IF;
            v_source_system := lower(regexp_replace(btrim(v_source_system), '[[:space:]]+', ' ', 'g'));
            v_source_kind := CASE v_ingest_channel
                WHEN 'controlled_upload' THEN 'CONTROLLED_UPLOAD'
                WHEN 'manual_upload' THEN 'CONTROLLED_UPLOAD'
                WHEN 'hermes' THEN 'HERMES'
                WHEN 'outlook' THEN 'OUTLOOK'
                WHEN 'synthetic_upload' THEN 'SYNTHETIC' ELSE NULL END;
            IF v_source_kind IS NULL THEN RETURN NULL; END IF;
            IF v_counterparty_ref IS NULL THEN
                v_counterparty_key := 'exact:' || v_counterparty;
                v_counterparty_basis := 'EXACT_PLATFORM_SUMMARY_V1';
            ELSE
                v_counterparty_key := v_counterparty_ref;
                v_counterparty_basis := 'REGISTRY_COUNTERPARTY';
            END IF;
            v_risks := internal_command.candidate_classification_risk_signature(p_candidate_id, p_revision);
            IF v_risks IS NULL THEN RETURN NULL; END IF;
            v_canonical := concat_ws(chr(31),
                'ledgerbridge.classification-key.v1', v_entity_id::text, v_source_system,
                v_source_kind, v_platform, v_direction, v_type, v_counterparty_key,
                v_counterparty_basis, v_funding, v_status, v_currency,
                array_to_string(v_risks, chr(30))
            );
            v_group_ref := 'cg_' || substr(encode(public.digest(convert_to(v_canonical, 'UTF8'), 'sha256'), 'hex'), 1, 32);
            RETURN jsonb_build_object(
                'group_ref', v_group_ref,
                'conditions', jsonb_build_object(
                    'key_version', 'ledgerbridge.classification-key.v1', 'entity_ref', v_entity_id,
                    'source_system', v_source_system, 'source_kind', v_source_kind,
                    'platform', v_platform, 'direction', v_direction, 'transaction_type', v_type,
                    'counterparty_key', v_counterparty_key, 'counterparty_label', v_counterparty,
                    'counterparty_basis', v_counterparty_basis, 'funding_instrument', v_funding,
                    'transaction_status', v_status, 'currency', v_currency,
                    'risk_signature', to_jsonb(v_risks)
                )
            );
        END
        $function$;

        CREATE TABLE internal_command.candidate_classification_batch_receipt (
            operation_id uuid PRIMARY KEY,
            group_ref varchar(35) NOT NULL,
            key_version varchar(64) NOT NULL,
            accounting_month date NOT NULL,
            authorized_entity_id uuid NOT NULL,
            target_business_unit_id uuid NOT NULL,
            source_candidate_id uuid NOT NULL,
            target_business_unit_ref varchar(100) NOT NULL,
            target_category_code varchar(100) NOT NULL,
            acknowledged_risk_codes varchar(64)[] NOT NULL,
            actor_ref varchar(200) NOT NULL,
            workload_principal_ref varchar(200) NOT NULL,
            verified_san varchar(200) NOT NULL,
            request_fingerprint bytea NOT NULL,
            member_operation_ids uuid[] NOT NULL,
            decided_at timestamptz NOT NULL,
            audit_event_id uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT candidate_classification_batch_group_shape CHECK (group_ref ~ '^cg_[0-9a-f]{32}$'),
            CONSTRAINT candidate_classification_batch_key_version CHECK (key_version = 'ledgerbridge.classification-key.v1'),
            CONSTRAINT candidate_classification_batch_month_first CHECK (accounting_month = date_trunc('month', accounting_month)::date),
            CONSTRAINT candidate_classification_batch_target_not_blank CHECK (btrim(target_business_unit_ref) <> '' AND btrim(target_category_code) <> ''),
            CONSTRAINT candidate_classification_batch_actor_not_blank CHECK (btrim(actor_ref) <> '' AND btrim(workload_principal_ref) <> ''),
            CONSTRAINT candidate_classification_batch_san_shape CHECK (verified_san ~ '^spiffe://ledgerbridge(\.test|\.local)?/[a-z0-9/_-]+$'),
            CONSTRAINT candidate_classification_batch_fingerprint_length CHECK (octet_length(request_fingerprint) = 32),
            CONSTRAINT candidate_classification_batch_member_count CHECK (cardinality(member_operation_ids) BETWEEN 2 AND 100),
            CONSTRAINT candidate_classification_batch_risks_closed CHECK (
                cardinality(acknowledged_risk_codes) <= 6
                AND array_position(acknowledged_risk_codes, NULL) IS NULL
                AND acknowledged_risk_codes <@ ARRAY[
                    'FUNDING_STATEMENT_REQUIRED','RELATED_ACCOUNT_STATEMENT_REQUIRED',
                    'HOTEL_PAYOUT_STATEMENT_REQUIRED','TRANSFER_REVIEW_REQUIRED',
                    'REVERSAL_MATCH_REQUIRED','UNSETTLED_TRANSACTION'
                ]::varchar(64)[]
            ),
            CONSTRAINT candidate_classification_batch_receipt_entity_fk
                FOREIGN KEY (authorized_entity_id) REFERENCES public.entity(id) ON DELETE RESTRICT,
            CONSTRAINT candidate_classification_batch_receipt_target_unit_fk
                FOREIGN KEY (target_business_unit_id) REFERENCES public.business_unit(id) ON DELETE RESTRICT,
            CONSTRAINT candidate_classification_batch_receipt_source_fk
                FOREIGN KEY (source_candidate_id) REFERENCES public.candidate(id) ON DELETE RESTRICT,
            CONSTRAINT candidate_classification_batch_receipt_audit_unique UNIQUE (audit_event_id),
            CONSTRAINT candidate_classification_batch_receipt_audit_fk
                FOREIGN KEY (audit_event_id) REFERENCES public.audit_event(id) ON DELETE RESTRICT
        );

        CREATE TABLE internal_command.candidate_classification_batch_member (
            batch_operation_id uuid NOT NULL,
            ordinal integer NOT NULL,
            candidate_id uuid NOT NULL,
            expected_revision integer NOT NULL,
            member_operation_id uuid NOT NULL,
            CONSTRAINT candidate_classification_batch_member_pk PRIMARY KEY (batch_operation_id, ordinal),
            CONSTRAINT candidate_classification_batch_member_candidate_unique UNIQUE (batch_operation_id, candidate_id),
            CONSTRAINT candidate_classification_batch_member_ordinal CHECK (ordinal BETWEEN 1 AND 100),
            CONSTRAINT candidate_classification_batch_member_revision CHECK (expected_revision >= 1),
            CONSTRAINT candidate_classification_batch_member_batch_fk
                FOREIGN KEY (batch_operation_id) REFERENCES internal_command.candidate_classification_batch_receipt(operation_id) ON DELETE RESTRICT,
            CONSTRAINT candidate_classification_batch_member_candidate_fk
                FOREIGN KEY (candidate_id) REFERENCES public.candidate(id) ON DELETE RESTRICT,
            CONSTRAINT candidate_classification_batch_member_operation_unique UNIQUE (member_operation_id),
            CONSTRAINT candidate_classification_batch_member_operation_fk
                FOREIGN KEY (member_operation_id) REFERENCES internal_command.candidate_decision_receipt(operation_id) ON DELETE RESTRICT
        );
        CREATE TABLE internal_command.candidate_classification_batch_assertion_use (
            assertion_jti uuid PRIMARY KEY,
            operation_id uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT candidate_classification_batch_assertion_operation_fk
                FOREIGN KEY (operation_id) REFERENCES internal_command.candidate_classification_batch_receipt(operation_id) ON DELETE RESTRICT
        );

        CREATE FUNCTION internal_command.reject_cross_surface_assertion_reuse()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtextextended('candidate-assertion:' || NEW.assertion_jti::text, 0));
            IF TG_TABLE_NAME = 'candidate_assertion_use' AND EXISTS (
                SELECT 1 FROM internal_command.candidate_classification_batch_assertion_use WHERE assertion_jti = NEW.assertion_jti
            ) THEN
                RAISE EXCEPTION 'assertion JTI was reused across command surfaces' USING ERRCODE = 'LB001';
            ELSIF TG_TABLE_NAME = 'candidate_classification_batch_assertion_use' AND EXISTS (
                SELECT 1 FROM internal_command.candidate_assertion_use WHERE assertion_jti = NEW.assertion_jti
            ) THEN
                RAISE EXCEPTION 'assertion JTI was reused across command surfaces' USING ERRCODE = 'LB001';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE TRIGGER candidate_assertion_cross_surface_guard BEFORE INSERT ON internal_command.candidate_assertion_use FOR EACH ROW EXECUTE FUNCTION internal_command.reject_cross_surface_assertion_reuse();
        CREATE TRIGGER candidate_classification_batch_assertion_cross_surface_guard BEFORE INSERT ON internal_command.candidate_classification_batch_assertion_use FOR EACH ROW EXECUTE FUNCTION internal_command.reject_cross_surface_assertion_reuse();
        CREATE TRIGGER candidate_classification_batch_receipt_append_only BEFORE UPDATE OR DELETE ON internal_command.candidate_classification_batch_receipt FOR EACH ROW EXECUTE FUNCTION internal_command.reject_mutation();
        CREATE TRIGGER candidate_classification_batch_member_append_only BEFORE UPDATE OR DELETE ON internal_command.candidate_classification_batch_member FOR EACH ROW EXECUTE FUNCTION internal_command.reject_mutation();
        CREATE TRIGGER candidate_classification_batch_assertion_append_only BEFORE UPDATE OR DELETE ON internal_command.candidate_classification_batch_assertion_use FOR EACH ROW EXECUTE FUNCTION internal_command.reject_mutation();

        CREATE FUNCTION internal_command.render_candidate_classification_batch_receipt(
            p_operation_id uuid, p_replayed boolean
        ) RETURNS jsonb
        LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE v_receipt jsonb;
        BEGIN
            SELECT jsonb_build_object(
                'contract_version', 'ledgerbridge.classification-batch.v1',
                'operation_id', batch.operation_id, 'replayed', p_replayed,
                'group_ref', batch.group_ref, 'accounting_month', to_char(batch.accounting_month, 'YYYY-MM'),
                'source_candidate_ref', batch.source_candidate_id,
                'target', jsonb_build_object('business_unit_ref', batch.target_business_unit_ref, 'category_code', batch.target_category_code),
                'acknowledged_risk_codes', to_jsonb(batch.acknowledged_risk_codes),
                'results', (
                    SELECT jsonb_agg(jsonb_build_object(
                        'candidate_ref', member.candidate_id, 'operation_id', member.member_operation_id,
                        'status', CASE WHEN p_replayed THEN 'REPLAYED' ELSE 'APPLIED' END,
                        'candidate', internal_read.render_candidate_revision(member.candidate_id, decision.final_revision),
                        'events', (SELECT jsonb_agg(internal_read.render_candidate_event(event_id) ORDER BY event_ordinal)
                                     FROM unnest(decision.event_operation_ids) WITH ORDINALITY AS event_ids(event_id, event_ordinal))
                    ) ORDER BY member.ordinal)
                      FROM internal_command.candidate_classification_batch_member AS member
                      JOIN internal_command.candidate_decision_receipt AS decision ON decision.operation_id = member.member_operation_id
                     WHERE member.batch_operation_id = batch.operation_id
                )
            ) INTO v_receipt FROM internal_command.candidate_classification_batch_receipt AS batch WHERE batch.operation_id = p_operation_id;
            IF v_receipt IS NULL THEN RAISE EXCEPTION 'candidate classification batch receipt is not visible' USING ERRCODE = 'LB004'; END IF;
            RETURN v_receipt;
        END
        $function$;

        CREATE FUNCTION internal_command.replay_candidate_classification_batch(p_request jsonb)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_operation_id uuid; v_assertion_jti uuid; v_actor_ref varchar(200);
            v_workload_ref varchar(200); v_verified_san varchar(200); v_authorized_entities uuid[];
            v_authorized_business_units uuid[];
            v_group_ref varchar(35); v_accounting_month date; v_source_candidate_id uuid;
            v_target_business_unit_ref varchar(100); v_target_category_code varchar(100);
            v_acknowledged_risks varchar(64)[]; v_members jsonb; v_member_refs uuid[];
            v_member_operations uuid[]; v_normalized_members jsonb; v_request_fingerprint bytea;
            v_receipt internal_command.candidate_classification_batch_receipt%ROWTYPE;
            v_existing_operation uuid;
        BEGIN
            IF p_request IS NULL OR jsonb_typeof(p_request) <> 'object' THEN
                RAISE EXCEPTION 'invalid candidate classification batch replay request' USING ERRCODE = 'LB003';
            END IF;
            BEGIN
                v_operation_id := (p_request->>'operation_id')::uuid;
                v_assertion_jti := (p_request->>'assertion_jti')::uuid;
                v_actor_ref := p_request->>'actor_ref'; v_workload_ref := p_request->>'workload_principal_ref';
                v_verified_san := p_request->>'verified_san';
                SELECT array_agg(value::uuid ORDER BY value::uuid) INTO v_authorized_entities FROM jsonb_array_elements_text(p_request->'authorized_entity_ids') AS ids(value);
                SELECT array_agg(value::uuid ORDER BY value::uuid) INTO v_authorized_business_units FROM jsonb_array_elements_text(p_request->'authorized_business_unit_ids') AS ids(value);
                v_group_ref := p_request->>'group_ref';
                v_accounting_month := to_date(p_request->>'accounting_month' || '-01', 'YYYY-MM-DD');
                v_source_candidate_id := (p_request->>'source_candidate_ref')::uuid;
                v_target_business_unit_ref := p_request#>>'{target,business_unit_ref}';
                v_target_category_code := p_request#>>'{target,category_code}';
                SELECT coalesce(array_agg(value::varchar(64) ORDER BY value), ARRAY[]::varchar(64)[]) INTO v_acknowledged_risks FROM jsonb_array_elements_text(coalesce(p_request->'acknowledged_risk_codes', '[]'::jsonb)) AS risks(value);
                v_members := p_request->'members';
                SELECT array_agg((member->>'candidate_ref')::uuid ORDER BY member->>'candidate_ref'),
                       array_agg((member->>'operation_id')::uuid ORDER BY member->>'candidate_ref'),
                       jsonb_agg(jsonb_build_object('candidate_ref', member->>'candidate_ref', 'expected_revision', (member->>'expected_revision')::integer) ORDER BY member->>'candidate_ref')
                  INTO v_member_refs, v_member_operations, v_normalized_members FROM jsonb_array_elements(v_members) AS members(member);
            EXCEPTION WHEN invalid_text_representation OR datetime_field_overflow OR numeric_value_out_of_range THEN
                RAISE EXCEPTION 'invalid candidate classification batch replay request' USING ERRCODE = 'LB003';
            END;
            IF v_operation_id IS NULL OR v_assertion_jti IS NULL
               OR coalesce(cardinality(v_authorized_entities), 0) < 1
               OR coalesce(cardinality(v_authorized_business_units), 0) < 1
               OR btrim(coalesce(v_actor_ref, '')) = '' OR btrim(coalesce(v_workload_ref, '')) = ''
               OR v_verified_san !~ '^spiffe://ledgerbridge(\.test|\.local)?/[a-z0-9/_-]+$'
               OR v_group_ref !~ '^cg_[0-9a-f]{32}$' OR p_request->>'accounting_month' !~ '^[0-9]{4}-(0[1-9]|1[0-2])$'
               OR v_source_candidate_id IS NULL OR btrim(coalesce(v_target_business_unit_ref, '')) = ''
               OR btrim(coalesce(v_target_category_code, '')) = '' OR btrim(coalesce(p_request->>'reason', '')) = ''
               OR jsonb_typeof(v_members) <> 'array' OR jsonb_array_length(v_members) NOT BETWEEN 2 AND 100
               OR cardinality(v_member_refs) <> (SELECT count(DISTINCT value) FROM unnest(v_member_refs) AS refs(value))
               OR cardinality(v_member_operations) <> (SELECT count(DISTINCT value) FROM unnest(v_member_operations) AS operations(value))
               OR NOT v_source_candidate_id = ANY(v_member_refs)
               OR NOT (v_acknowledged_risks <@ ARRAY['FUNDING_STATEMENT_REQUIRED','RELATED_ACCOUNT_STATEMENT_REQUIRED','HOTEL_PAYOUT_STATEMENT_REQUIRED','TRANSFER_REVIEW_REQUIRED','REVERSAL_MATCH_REQUIRED','UNSETTLED_TRANSACTION']::varchar(64)[])
               OR cardinality(v_acknowledged_risks) <> jsonb_array_length(coalesce(p_request->'acknowledged_risk_codes', '[]'::jsonb)) THEN
                RAISE EXCEPTION 'invalid candidate classification batch replay request' USING ERRCODE = 'LB003';
            END IF;
            v_request_fingerprint := public.digest(convert_to(jsonb_build_object(
                'group_ref', v_group_ref, 'accounting_month', to_char(v_accounting_month, 'YYYY-MM'),
                'source_candidate_ref', v_source_candidate_id, 'target', p_request->'target',
                'members', v_normalized_members, 'reason', p_request->>'reason',
                'acknowledged_risk_codes', to_jsonb(v_acknowledged_risks)
            )::text, 'UTF8'), 'sha256');
            PERFORM pg_advisory_xact_lock(hashtextextended('classification-batch:' || v_operation_id::text, 0));
            SELECT * INTO v_receipt FROM internal_command.candidate_classification_batch_receipt
             WHERE operation_id = v_operation_id AND authorized_entity_id = ANY(v_authorized_entities)
               AND target_business_unit_id = ANY(v_authorized_business_units);
            IF NOT FOUND THEN RETURN NULL; END IF;
            IF EXISTS (SELECT 1 FROM internal_command.candidate_assertion_use WHERE assertion_jti = v_assertion_jti) THEN
                RAISE EXCEPTION 'assertion JTI was reused for another operation' USING ERRCODE = 'LB001';
            END IF;
            SELECT operation_id INTO v_existing_operation FROM internal_command.candidate_classification_batch_assertion_use WHERE assertion_jti = v_assertion_jti;
            IF FOUND AND v_existing_operation IS DISTINCT FROM v_operation_id THEN
                RAISE EXCEPTION 'assertion JTI was reused for another operation' USING ERRCODE = 'LB001';
            END IF;
            IF v_receipt.group_ref IS DISTINCT FROM v_group_ref OR v_receipt.accounting_month IS DISTINCT FROM v_accounting_month
               OR v_receipt.source_candidate_id IS DISTINCT FROM v_source_candidate_id
               OR v_receipt.target_business_unit_ref IS DISTINCT FROM v_target_business_unit_ref
               OR v_receipt.target_category_code IS DISTINCT FROM v_target_category_code
               OR v_receipt.acknowledged_risk_codes IS DISTINCT FROM v_acknowledged_risks
               OR v_receipt.actor_ref IS DISTINCT FROM v_actor_ref OR v_receipt.workload_principal_ref IS DISTINCT FROM v_workload_ref
               OR v_receipt.verified_san IS DISTINCT FROM v_verified_san OR v_receipt.request_fingerprint IS DISTINCT FROM v_request_fingerprint
               OR v_receipt.member_operation_ids IS DISTINCT FROM v_member_operations THEN
                RAISE EXCEPTION 'batch operation ID was reused with different content or actor' USING ERRCODE = 'LB001';
            END IF;
            INSERT INTO internal_command.candidate_classification_batch_assertion_use(assertion_jti, operation_id)
            VALUES (v_assertion_jti, v_operation_id) ON CONFLICT DO NOTHING;
            SELECT operation_id INTO v_existing_operation FROM internal_command.candidate_classification_batch_assertion_use WHERE assertion_jti = v_assertion_jti;
            IF v_existing_operation IS DISTINCT FROM v_operation_id THEN
                RAISE EXCEPTION 'assertion JTI was reused for another operation' USING ERRCODE = 'LB001';
            END IF;
            RETURN internal_command.render_candidate_classification_batch_receipt(v_operation_id, true);
        END
        $function$;

        CREATE FUNCTION internal_command.apply_candidate_classification_batch(p_request jsonb)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_replayed jsonb; v_operation_id uuid; v_assertion_jti uuid; v_actor_ref varchar(200);
            v_workload_ref varchar(200); v_verified_san varchar(200); v_authorized_entity_id uuid;
            v_authorized_business_units uuid[]; v_authorized_unassigned_entities uuid[];
            v_target_business_unit_id uuid;
            v_group_ref varchar(35); v_accounting_month date; v_source_candidate_id uuid;
            v_target_business_unit_ref varchar(100); v_target_category_code varchar(100);
            v_acknowledged_risks varchar(64)[]; v_decided_at timestamptz; v_members jsonb;
            v_member jsonb; v_member_refs uuid[]; v_member_operations uuid[]; v_normalized_members jsonb;
            v_request_fingerprint bytea; v_member_receipt jsonb; v_member_group jsonb;
            v_audit_event_id uuid; v_count integer; v_ordinal integer := 0; v_median numeric;
        BEGIN
            v_replayed := internal_command.replay_candidate_classification_batch(p_request);
            IF v_replayed IS NOT NULL THEN RETURN v_replayed; END IF;
            BEGIN
                v_operation_id := (p_request->>'operation_id')::uuid; v_assertion_jti := (p_request->>'assertion_jti')::uuid;
                v_actor_ref := p_request->>'actor_ref'; v_workload_ref := p_request->>'workload_principal_ref'; v_verified_san := p_request->>'verified_san';
                v_authorized_entity_id := (p_request->>'authorized_entity_id')::uuid; v_group_ref := p_request->>'group_ref';
                SELECT array_agg(value::uuid ORDER BY value::uuid) INTO v_authorized_business_units FROM jsonb_array_elements_text(p_request->'authorized_business_unit_ids') AS ids(value);
                SELECT coalesce(array_agg(value::uuid ORDER BY value::uuid), ARRAY[]::uuid[]) INTO v_authorized_unassigned_entities FROM jsonb_array_elements_text(p_request->'authorized_unassigned_entity_ids') AS ids(value);
                v_accounting_month := to_date(p_request->>'accounting_month' || '-01', 'YYYY-MM-DD');
                v_source_candidate_id := (p_request->>'source_candidate_ref')::uuid;
                v_target_business_unit_ref := p_request#>>'{target,business_unit_ref}'; v_target_category_code := p_request#>>'{target,category_code}';
                SELECT coalesce(array_agg(value::varchar(64) ORDER BY value), ARRAY[]::varchar(64)[]) INTO v_acknowledged_risks FROM jsonb_array_elements_text(coalesce(p_request->'acknowledged_risk_codes', '[]'::jsonb)) AS risks(value);
                v_decided_at := (p_request->>'decided_at')::timestamptz; v_members := p_request->'members';
                SELECT array_agg((member->>'candidate_ref')::uuid ORDER BY member->>'candidate_ref'),
                       array_agg((member->>'operation_id')::uuid ORDER BY member->>'candidate_ref'),
                       jsonb_agg(jsonb_build_object('candidate_ref', member->>'candidate_ref', 'expected_revision', (member->>'expected_revision')::integer) ORDER BY member->>'candidate_ref')
                  INTO v_member_refs, v_member_operations, v_normalized_members FROM jsonb_array_elements(v_members) AS members(member);
            EXCEPTION WHEN invalid_text_representation OR datetime_field_overflow OR numeric_value_out_of_range THEN
                RAISE EXCEPTION 'invalid candidate classification batch request' USING ERRCODE = 'LB003';
            END;
            IF p_request->>'key_version' <> 'ledgerbridge.classification-key.v1' OR v_authorized_entity_id IS NULL
               OR v_decided_at IS NULL
               OR NOT v_authorized_entity_id = ANY(ARRAY(SELECT value::uuid FROM jsonb_array_elements_text(p_request->'authorized_entity_ids') AS ids(value)))
               OR NOT (v_acknowledged_risks = ARRAY[]::varchar(64)[] OR v_acknowledged_risks = ARRAY['TRANSFER_REVIEW_REQUIRED']::varchar(64)[]) THEN
                RAISE EXCEPTION 'invalid candidate classification batch request' USING ERRCODE = 'LB003';
            END IF;
            SELECT unit.id INTO v_target_business_unit_id FROM public.business_unit AS unit
             WHERE unit.entity_id = v_authorized_entity_id AND unit.ref = v_target_business_unit_ref
               AND unit.retired_at IS NULL;
            IF NOT FOUND OR NOT v_target_business_unit_id = ANY(v_authorized_business_units)
               OR NOT EXISTS (SELECT 1 FROM public.reporting_category AS category
                               WHERE category.entity_id = v_authorized_entity_id
                                 AND category.code = v_target_category_code
                                 AND category.retired_at IS NULL) THEN
                RAISE EXCEPTION 'candidate classification target is outside authorized scope' USING ERRCODE = 'LB004';
            END IF;
            v_request_fingerprint := public.digest(convert_to(jsonb_build_object(
                'group_ref', v_group_ref, 'accounting_month', to_char(v_accounting_month, 'YYYY-MM'),
                'source_candidate_ref', v_source_candidate_id, 'target', p_request->'target',
                'members', v_normalized_members, 'reason', p_request->>'reason',
                'acknowledged_risk_codes', to_jsonb(v_acknowledged_risks)
            )::text, 'UTF8'), 'sha256');
            -- Establish one deterministic lock order before any member command writes.
            PERFORM candidate.id FROM public.candidate AS candidate WHERE candidate.id = ANY(v_member_refs) ORDER BY candidate.id FOR UPDATE;
            GET DIAGNOSTICS v_count = ROW_COUNT;
            IF v_count IS DISTINCT FROM cardinality(v_member_refs) THEN RAISE EXCEPTION 'candidate classification batch scope changed' USING ERRCODE = 'LB002'; END IF;
            -- Complete the read-only preflight for every member before the first write.
            FOR v_member IN SELECT value FROM jsonb_array_elements(v_members)
            LOOP
                BEGIN
                    SELECT internal_command.candidate_classification_group_key((v_member->>'candidate_ref')::uuid, (v_member->>'expected_revision')::integer) INTO v_member_group;
                    SELECT count(*) INTO v_count
                      FROM public.candidate AS candidate
                      JOIN public.candidate_revision AS current ON current.candidate_id = candidate.id AND current.revision = (v_member->>'expected_revision')::integer
                     WHERE candidate.id = (v_member->>'candidate_ref')::uuid AND candidate.entity_id = v_authorized_entity_id
                       AND current.revision = (SELECT max(revision) FROM public.candidate_revision WHERE candidate_id = candidate.id)
                       AND current.status = 'PENDING' AND current.accounting_month = v_accounting_month
                       AND current.confidence_basis_points >= 9000
                       AND current.business_unit_id IS NOT DISTINCT FROM (v_member->>'current_business_unit_id')::uuid
                       AND (current.business_unit_id = ANY(v_authorized_business_units)
                            OR (current.business_unit_id IS NULL
                                AND v_authorized_entity_id = ANY(v_authorized_unassigned_entities)))
                       AND (v_member->>'target_business_unit_id')::uuid = v_target_business_unit_id
                       AND v_member->>'reason' = p_request->>'reason'
                       AND v_member->>'decision' = CASE
                           WHEN current.business_unit_id IS DISTINCT FROM v_target_business_unit_id
                             OR current.category_code_snapshot IS DISTINCT FROM v_target_category_code
                           THEN 'CORRECT_AND_CONFIRM' ELSE 'CONFIRM' END
                       AND CASE WHEN current.business_unit_id IS DISTINCT FROM v_target_business_unit_id
                           THEN (v_member->>'set_business_unit')::boolean
                                AND v_member->>'business_unit_ref' = v_target_business_unit_ref
                           ELSE NOT (v_member->>'set_business_unit')::boolean
                                AND v_member->>'business_unit_ref' IS NULL END
                       AND CASE WHEN current.category_code_snapshot IS DISTINCT FROM v_target_category_code
                           THEN (v_member->>'set_category')::boolean
                                AND v_member->>'category_code' = v_target_category_code
                           ELSE NOT (v_member->>'set_category')::boolean
                                AND v_member->>'category_code' IS NULL END
                       AND NOT EXISTS (SELECT 1 FROM public.candidate_blocker AS blocker WHERE blocker.candidate_id = candidate.id AND blocker.revision = current.revision)
                       AND v_member_group->>'group_ref' = v_group_ref
                       AND v_member_group#>>'{conditions,key_version}' = 'ledgerbridge.classification-key.v1'
                       AND v_member_group#>'{conditions,risk_signature}' = to_jsonb(v_acknowledged_risks);
                EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                    RAISE EXCEPTION 'candidate classification batch member is invalid' USING ERRCODE = 'LB003';
                END;
                IF v_count IS DISTINCT FROM 1 THEN RAISE EXCEPTION 'candidate classification batch member, risk signature, or group key drifted' USING ERRCODE = 'LB002'; END IF;
            END LOOP;
            SELECT floor(percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(current.amount_minor))) INTO v_median
              FROM public.candidate_revision AS current WHERE current.candidate_id = ANY(v_member_refs)
               AND current.revision = (SELECT max(r.revision) FROM public.candidate_revision AS r WHERE r.candidate_id = current.candidate_id);
            IF cardinality(v_member_refs) >= 3 AND EXISTS (
                SELECT 1 FROM public.candidate_revision AS current WHERE current.candidate_id = ANY(v_member_refs)
                 AND current.revision = (SELECT max(r.revision) FROM public.candidate_revision AS r WHERE r.candidate_id = current.candidate_id)
                 AND abs(current.amount_minor) >= greatest(100000, v_median * 10)
            ) THEN RAISE EXCEPTION 'candidate classification batch contains an amount outlier' USING ERRCODE = 'LB002'; END IF;
            -- Calls remain inside this transaction; a later error rolls back earlier calls.
            FOR v_member IN SELECT value FROM jsonb_array_elements(v_members) ORDER BY value->>'candidate_ref'
            LOOP
                v_member_receipt := internal_command.apply_candidate_decision(
                    (v_member->>'operation_id')::uuid, (v_member->>'assertion_jti')::uuid, (v_member->>'candidate_ref')::uuid,
                    v_actor_ref, v_workload_ref, v_verified_san, v_authorized_entity_id,
                    (v_member->>'current_business_unit_id')::uuid, (v_member->>'target_business_unit_id')::uuid,
                    (v_member->>'decision')::varchar(32), (v_member->>'expected_revision')::integer,
                    (v_member->>'reason')::varchar(1000), (v_member->>'set_business_unit')::boolean,
                    (v_member->>'business_unit_ref')::varchar(100), (v_member->>'set_category')::boolean,
                    (v_member->>'category_code')::varchar(100), false, NULL, false, NULL, NULL, v_decided_at
                );
                IF v_member_receipt IS NULL THEN RAISE EXCEPTION 'candidate classification member returned no receipt' USING ERRCODE = 'LB003'; END IF;
            END LOOP;
            v_audit_event_id := public.append_audit_event(
                v_actor_ref, 'candidate.classification.batch', 'explicit similar-transaction classification batch', 'ledgerbridge.classification-batch.v1',
                jsonb_build_object('operation_id', v_operation_id, 'group_ref', v_group_ref,
                    'key_version', 'ledgerbridge.classification-key.v1', 'accounting_month', to_char(v_accounting_month, 'YYYY-MM'),
                    'authorized_entity_id', v_authorized_entity_id, 'source_candidate_ref', v_source_candidate_id,
                    'target_business_unit_ref', v_target_business_unit_ref, 'target_category_code', v_target_category_code,
                    'acknowledged_risk_codes', to_jsonb(v_acknowledged_risks), 'member_operation_ids', v_member_operations,
                    'request_fingerprint', encode(v_request_fingerprint, 'hex'))
            );
            INSERT INTO internal_command.candidate_classification_batch_receipt(
                operation_id, group_ref, key_version, accounting_month, authorized_entity_id,
                target_business_unit_id, source_candidate_id,
                target_business_unit_ref, target_category_code, acknowledged_risk_codes, actor_ref,
                workload_principal_ref, verified_san, request_fingerprint, member_operation_ids, decided_at, audit_event_id
            ) VALUES (
                v_operation_id, v_group_ref, 'ledgerbridge.classification-key.v1', v_accounting_month, v_authorized_entity_id,
                v_target_business_unit_id, v_source_candidate_id, v_target_business_unit_ref,
                v_target_category_code, v_acknowledged_risks,
                v_actor_ref, v_workload_ref, v_verified_san, v_request_fingerprint, v_member_operations, v_decided_at, v_audit_event_id
            );
            FOR v_member IN SELECT value FROM jsonb_array_elements(v_members) ORDER BY value->>'candidate_ref'
            LOOP
                v_ordinal := v_ordinal + 1;
                INSERT INTO internal_command.candidate_classification_batch_member(batch_operation_id, ordinal, candidate_id, expected_revision, member_operation_id)
                VALUES (v_operation_id, v_ordinal, (v_member->>'candidate_ref')::uuid, (v_member->>'expected_revision')::integer, (v_member->>'operation_id')::uuid);
            END LOOP;
            INSERT INTO internal_command.candidate_classification_batch_assertion_use(assertion_jti, operation_id) VALUES (v_assertion_jti, v_operation_id);
            RETURN internal_command.render_candidate_classification_batch_receipt(v_operation_id, false);
        END
        $function$;

        REVOKE ALL ON TABLE internal_command.candidate_classification_batch_receipt, internal_command.candidate_classification_batch_member, internal_command.candidate_classification_batch_assertion_use FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker;
        REVOKE ALL ON FUNCTION
            internal_command.candidate_classification_risk_signature(uuid, integer),
            internal_command.candidate_classification_group_key(uuid, integer),
            internal_command.render_candidate_classification_batch_receipt(uuid, boolean),
            internal_command.replay_candidate_classification_batch(jsonb),
            internal_command.apply_candidate_classification_batch(jsonb),
            internal_command.reject_cross_surface_assertion_reuse()
            FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker;
        GRANT EXECUTE ON FUNCTION internal_command.replay_candidate_classification_batch(jsonb), internal_command.apply_candidate_classification_batch(jsonb) TO ledgerbridge_api;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM internal_command.candidate_classification_batch_receipt)
               OR EXISTS (SELECT 1 FROM internal_command.candidate_classification_batch_member)
               OR EXISTS (SELECT 1 FROM internal_command.candidate_classification_batch_assertion_use) THEN
                RAISE EXCEPTION 'candidate classification batch facts prevent destructive downgrade';
            END IF;
        END
        $guard$;
        REVOKE ALL ON FUNCTION internal_command.replay_candidate_classification_batch(jsonb), internal_command.apply_candidate_classification_batch(jsonb) FROM ledgerbridge_api;
        DROP FUNCTION internal_command.apply_candidate_classification_batch(jsonb);
        DROP FUNCTION internal_command.replay_candidate_classification_batch(jsonb);
        DROP FUNCTION internal_command.render_candidate_classification_batch_receipt(uuid, boolean);
        DROP TRIGGER candidate_classification_batch_assertion_cross_surface_guard ON internal_command.candidate_classification_batch_assertion_use;
        DROP TRIGGER candidate_assertion_cross_surface_guard ON internal_command.candidate_assertion_use;
        DROP FUNCTION internal_command.reject_cross_surface_assertion_reuse();
        DROP TRIGGER candidate_classification_batch_assertion_append_only ON internal_command.candidate_classification_batch_assertion_use;
        DROP TRIGGER candidate_classification_batch_member_append_only ON internal_command.candidate_classification_batch_member;
        DROP TRIGGER candidate_classification_batch_receipt_append_only ON internal_command.candidate_classification_batch_receipt;
        DROP TABLE internal_command.candidate_classification_batch_assertion_use;
        DROP TABLE internal_command.candidate_classification_batch_member;
        DROP TABLE internal_command.candidate_classification_batch_receipt;
        DROP FUNCTION internal_command.candidate_classification_group_key(uuid, integer);
        DROP FUNCTION internal_command.candidate_classification_risk_signature(uuid, integer);
        """
    )
