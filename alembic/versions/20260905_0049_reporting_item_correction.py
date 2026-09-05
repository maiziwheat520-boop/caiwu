"""Add owner-only, audited reporting item correction without changing classifications."""

from alembic import op

revision = "20260905_0049"
down_revision = "20260905_0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    # Removing the maintenance entry point preserves every versioned correction.
    op.execute(
        "DROP FUNCTION internal_import.correct_company_transaction_reporting_item"
        "(uuid,integer,text,text,text,uuid,text,text)"
    )


_SQL = r"""
CREATE FUNCTION internal_import.correct_company_transaction_reporting_item(
    p_transaction_ref uuid,
    p_expected_revision integer,
    p_expected_category_code text,
    p_expected_reporting_item_code text,
    p_reporting_item_code text,
    p_operation_id uuid,
    p_actor_ref text,
    p_reason text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_existing public.company_transaction_classification%ROWTYPE;
    v_current public.company_transaction_classification%ROWTYPE;
    v_revision integer;
    v_item_revision integer;
    v_command bytea;
    v_audit uuid;
BEGIN
    IF p_transaction_ref IS NULL OR p_expected_revision IS NULL
       OR p_expected_revision <= 0
       OR p_expected_category_code IS NULL OR btrim(p_expected_category_code) = ''
       OR p_expected_reporting_item_code IS NULL OR btrim(p_expected_reporting_item_code) = ''
       OR p_expected_reporting_item_code = p_reporting_item_code
       OR p_reporting_item_code IS NULL OR btrim(p_reporting_item_code) = ''
       OR length(p_reporting_item_code) > 100
       OR p_operation_id IS NULL OR p_actor_ref IS NULL OR btrim(p_actor_ref) = ''
       OR p_reason IS NULL OR btrim(p_reason) = '' THEN
        RAISE EXCEPTION 'company transaction reporting item is invalid'
            USING ERRCODE = '22023';
    END IF;
    v_command := public.digest(convert_to(jsonb_build_array(
        p_transaction_ref, p_expected_revision, p_expected_category_code,
        p_expected_reporting_item_code, p_reporting_item_code,
        p_actor_ref, p_reason)::text, 'UTF8'), 'sha256');
    PERFORM pg_advisory_xact_lock(hashtextextended(p_operation_id::text, 0));
    SELECT * INTO v_existing
      FROM public.company_transaction_classification
     WHERE operation_id = p_operation_id
     ORDER BY operation_id = p_operation_id DESC LIMIT 1;
    IF FOUND THEN
        IF v_existing.transaction_ref IS DISTINCT FROM p_transaction_ref
           OR v_existing.expected_revision IS DISTINCT FROM p_expected_revision
           OR v_existing.category_code IS DISTINCT FROM p_expected_category_code
           OR v_existing.reporting_item_code IS DISTINCT FROM p_reporting_item_code
           OR v_existing.command_sha256 IS DISTINCT FROM v_command THEN
            RAISE EXCEPTION 'company transaction reporting item idempotency conflict'
                USING ERRCODE = 'LB001';
        END IF;
        RETURN jsonb_build_object('transaction_ref', p_transaction_ref,
            'reporting_item_code', v_existing.reporting_item_code, 'created', false);
    END IF;
    SELECT * INTO v_current FROM public.company_transaction_classification
     WHERE transaction_ref = p_transaction_ref ORDER BY revision DESC LIMIT 1 FOR UPDATE;
    IF NOT FOUND OR v_current.revision IS DISTINCT FROM p_expected_revision
       OR v_current.status IS DISTINCT FROM 'CONFIRMED'
       OR v_current.category_code IS DISTINCT FROM p_expected_category_code THEN
        RAISE EXCEPTION 'reporting item correction requires current confirmed classification'
            USING ERRCODE = 'LB004';
    END IF;
    IF v_current.reporting_item_code IS DISTINCT FROM p_expected_reporting_item_code THEN
        RAISE EXCEPTION 'company transaction reporting item correction old value mismatch'
            USING ERRCODE = 'LB003';
    END IF;
    SELECT registry.revision INTO v_item_revision
      FROM public.company_transaction_reporting_item registry
     WHERE registry.category_code = p_expected_category_code
       AND registry.item_code = p_reporting_item_code
     ORDER BY registry.revision DESC LIMIT 1;
    IF v_item_revision IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.company_transaction_reporting_item registry
         WHERE registry.category_code = p_expected_category_code
           AND registry.item_code = p_reporting_item_code
           AND registry.revision = v_item_revision AND registry.status = 'ACTIVE') THEN
        v_item_revision := NULL;
    END IF;
    IF v_item_revision IS NULL THEN
        RAISE EXCEPTION 'company transaction reporting item is not active for category'
            USING ERRCODE = 'LB003';
    END IF;
    v_revision := v_current.revision + 1;
    v_audit := public.append_audit_event(
        p_actor_ref, 'company_transaction_classification.reporting_item.backfill', p_reason,
        'ledgerbridge.company-transaction-classification.v1',
        jsonb_build_object(
            'transaction_ref', p_transaction_ref,
            'revision', v_revision,
            'status', 'CONFIRMED',
            'category_code', v_current.category_code,
            'previous_reporting_item_code', v_current.reporting_item_code,
            'previous_reporting_item_revision', v_current.reporting_item_revision,
            'reporting_item_code', p_reporting_item_code,
            'reporting_item_revision', v_item_revision,
            'source', 'BACKFILL',
            'rule_version', 'reporting-item-correction.v1',
            'operation_id', p_operation_id,
            'command_sha256', encode(v_command, 'hex')
        )
    );
    INSERT INTO public.company_transaction_classification(
        transaction_ref, revision, status, category_code, reporting_item_code,
        reporting_item_revision,
        source, rule_version, operation_id, assertion_jti, actor_ref,
        workload_principal_ref, expected_revision, command_sha256,
        audit_event_id, classified_at
    ) VALUES (
        p_transaction_ref, v_revision, 'CONFIRMED', v_current.category_code,
        p_reporting_item_code, v_item_revision, 'BACKFILL', 'reporting-item-correction.v1',
        p_operation_id, NULL, p_actor_ref, NULL, p_expected_revision, v_command, v_audit,
        (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
    );
    RETURN jsonb_build_object('transaction_ref', p_transaction_ref,
        'reporting_item_code', p_reporting_item_code, 'created', true);
END
$function$;

REVOKE ALL ON FUNCTION internal_import.correct_company_transaction_reporting_item(
    uuid,integer,text,text,text,uuid,text,text)
FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_app, ledgerbridge_worker;

"""
