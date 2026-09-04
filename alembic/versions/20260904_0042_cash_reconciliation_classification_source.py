# ruff: noqa: E501
"""Read company cash reconciliation from confirmed classifications.

Revision ID: 20260904_0042
Revises: 20260904_0041
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260904_0042"
down_revision: str | None = "20260904_0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)


_UPGRADE_SQL = r"""
CREATE TABLE public.company_transaction_reporting_item (
    item_code varchar(100) NOT NULL CHECK (btrim(item_code) <> ''),
    revision integer NOT NULL CHECK (revision > 0),
    status text NOT NULL CHECK (status IN ('ACTIVE','RETIRED')),
    category_code varchar(64) NOT NULL CHECK (category_code IN (
        'PLATFORM_ROOM_REVENUE','RELATED_PARTY_CURRENT','PAYROLL','FINANCING',
        'BOTTLED_WATER','INTERNAL_TRANSFER','RENT','RENTAL_INCOME','BANK_INTEREST',
        'LINEN_LAUNDRY','OPERATING_FEE')),
    item_label varchar(100) NOT NULL CHECK (btrim(item_label) <> ''),
    match_counterparty_name varchar(300),
    audit_event_id uuid NOT NULL REFERENCES public.audit_event(id),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (item_code, revision),
    UNIQUE (category_code, item_code, revision),
    CHECK (match_counterparty_name IS NULL OR btrim(match_counterparty_name) <> '')
);
CREATE FUNCTION public.r1_validate_company_transaction_reporting_item()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
AS $function$
DECLARE v_payload jsonb; v_action text; v_rule text; v_time timestamptz;
BEGIN
    SELECT payload, action, rule_version, occurred_at
      INTO v_payload, v_action, v_rule, v_time
      FROM public.audit_event WHERE id = NEW.audit_event_id;
    IF v_action IS DISTINCT FROM 'company_transaction_reporting_item.record'
       OR v_rule IS DISTINCT FROM 'ledgerbridge.company-transaction-reporting-item.v1'
       OR v_payload->>'item_code' IS DISTINCT FROM NEW.item_code
       OR v_payload->>'revision' IS DISTINCT FROM NEW.revision::text
       OR v_payload->>'status' IS DISTINCT FROM NEW.status
       OR v_payload->>'category_code' IS DISTINCT FROM NEW.category_code
       OR v_payload->>'item_label' IS DISTINCT FROM NEW.item_label
       OR v_payload->>'match_counterparty_name' IS DISTINCT FROM NEW.match_counterparty_name
       OR v_time IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'company transaction reporting item audit binding is invalid'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER validate_company_transaction_reporting_item
BEFORE INSERT ON public.company_transaction_reporting_item
FOR EACH ROW EXECUTE FUNCTION public.r1_validate_company_transaction_reporting_item();
CREATE TRIGGER company_transaction_reporting_item_append_only
BEFORE UPDATE OR DELETE ON public.company_transaction_reporting_item
FOR EACH ROW EXECUTE FUNCTION public.r1_bank_statement_append_only();
REVOKE ALL ON public.company_transaction_reporting_item
FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;

CREATE TABLE public.company_transaction_reporting_item_match (
    category_code varchar(64) NOT NULL,
    match_field text NOT NULL CHECK (match_field IN ('COUNTERPARTY_NAME','TRANSACTION_NAME')),
    match_mode text NOT NULL CHECK (match_mode IN ('EXACT','CONTAINS')),
    match_value varchar(300) NOT NULL CHECK (btrim(match_value) <> ''),
    item_code varchar(100) NOT NULL,
    item_revision integer NOT NULL,
    audit_event_id uuid NOT NULL REFERENCES public.audit_event(id),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (category_code, match_field, match_mode, match_value),
    FOREIGN KEY (category_code, item_code, item_revision)
        REFERENCES public.company_transaction_reporting_item(category_code, item_code, revision)
);
CREATE FUNCTION public.r1_validate_company_transaction_reporting_item_match()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
AS $function$
DECLARE v_payload jsonb; v_action text; v_rule text; v_time timestamptz;
BEGIN
    SELECT payload, action, rule_version, occurred_at
      INTO v_payload, v_action, v_rule, v_time
      FROM public.audit_event WHERE id = NEW.audit_event_id;
    IF v_action IS DISTINCT FROM 'company_transaction_reporting_item_match.record'
       OR v_rule IS DISTINCT FROM 'ledgerbridge.company-transaction-reporting-item.v1'
       OR v_payload->>'category_code' IS DISTINCT FROM NEW.category_code
       OR v_payload->>'match_field' IS DISTINCT FROM NEW.match_field
       OR v_payload->>'match_mode' IS DISTINCT FROM NEW.match_mode
       OR v_payload->>'match_value' IS DISTINCT FROM NEW.match_value
       OR v_payload->>'item_code' IS DISTINCT FROM NEW.item_code
       OR v_payload->>'item_revision' IS DISTINCT FROM NEW.item_revision::text
       OR v_time IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'company transaction reporting item match audit binding is invalid'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER validate_company_transaction_reporting_item_match
BEFORE INSERT ON public.company_transaction_reporting_item_match
FOR EACH ROW EXECUTE FUNCTION public.r1_validate_company_transaction_reporting_item_match();
CREATE TRIGGER company_transaction_reporting_item_match_append_only
BEFORE UPDATE OR DELETE ON public.company_transaction_reporting_item_match
FOR EACH ROW EXECUTE FUNCTION public.r1_bank_statement_append_only();
REVOKE ALL ON public.company_transaction_reporting_item_match
FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;

CREATE TABLE public.cash_reconciliation_adjustment_scope (
    adjustment_id uuid PRIMARY KEY REFERENCES public.cash_reconciliation_adjustment(adjustment_id),
    entity_id uuid NOT NULL REFERENCES public.entity(id),
    business_unit_id uuid NOT NULL REFERENCES public.business_unit(id),
    audit_event_id uuid NOT NULL REFERENCES public.audit_event(id),
    created_at timestamptz NOT NULL
);
CREATE FUNCTION public.r1_validate_cash_reconciliation_adjustment_scope()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
AS $function$
DECLARE v_payload jsonb; v_action text; v_rule text; v_time timestamptz;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.business_unit unit
        WHERE unit.id = NEW.business_unit_id AND unit.entity_id = NEW.entity_id) THEN
        RAISE EXCEPTION 'cash reconciliation adjustment scope crosses entity boundary'
            USING ERRCODE = '23514';
    END IF;
    SELECT payload, action, rule_version, occurred_at
      INTO v_payload, v_action, v_rule, v_time
      FROM public.audit_event WHERE id = NEW.audit_event_id;
    IF v_action IS DISTINCT FROM 'cash_reconciliation_adjustment_scope.record'
       OR v_rule IS DISTINCT FROM 'ledgerbridge.cash-reconciliation-adjustment-scope.v1'
       OR v_payload->>'adjustment_id' IS DISTINCT FROM NEW.adjustment_id::text
       OR v_payload->>'entity_id' IS DISTINCT FROM NEW.entity_id::text
       OR v_payload->>'business_unit_id' IS DISTINCT FROM NEW.business_unit_id::text
       OR v_time IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'cash reconciliation adjustment scope audit binding is invalid'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER validate_cash_reconciliation_adjustment_scope
BEFORE INSERT ON public.cash_reconciliation_adjustment_scope
FOR EACH ROW EXECUTE FUNCTION public.r1_validate_cash_reconciliation_adjustment_scope();
CREATE TRIGGER cash_reconciliation_adjustment_scope_append_only
BEFORE UPDATE OR DELETE ON public.cash_reconciliation_adjustment_scope
FOR EACH ROW EXECUTE FUNCTION public.r1_bank_statement_append_only();
REVOKE ALL ON public.cash_reconciliation_adjustment_scope
FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;

CREATE TABLE public.cash_reconciliation_projection_activation (
    revision integer PRIMARY KEY CHECK (revision > 0),
    status text NOT NULL CHECK (status IN ('PENDING','ACTIVE')),
    audit_event_id uuid NOT NULL REFERENCES public.audit_event(id),
    created_at timestamptz NOT NULL
);
CREATE FUNCTION public.r1_validate_cash_reconciliation_projection_activation()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
AS $function$
DECLARE v_payload jsonb; v_action text; v_rule text; v_time timestamptz;
BEGIN
    IF NEW.status = 'ACTIVE' THEN
        LOCK TABLE public.company_transaction_classification IN SHARE ROW EXCLUSIVE MODE;
        LOCK TABLE public.cash_reconciliation_adjustment IN SHARE ROW EXCLUSIVE MODE;
        LOCK TABLE public.cash_reconciliation_adjustment_scope IN SHARE ROW EXCLUSIVE MODE;
        IF EXISTS (
            SELECT 1 FROM (
                SELECT DISTINCT ON (item.transaction_ref) item.*
                  FROM public.company_transaction_classification item
                 ORDER BY item.transaction_ref, item.revision DESC
            ) current
            WHERE current.status = 'CONFIRMED'
              AND (current.reporting_item_code IS NULL
                   OR current.reporting_item_revision IS NULL)
        ) THEN
            RAISE EXCEPTION 'cash reconciliation activation requires complete reporting items'
                USING ERRCODE = '23514';
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.cash_reconciliation_adjustment adjustment
            LEFT JOIN public.cash_reconciliation_adjustment_scope scope
              ON scope.adjustment_id = adjustment.adjustment_id
            WHERE scope.adjustment_id IS NULL
        ) THEN
            RAISE EXCEPTION 'cash reconciliation activation requires complete adjustment scope'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    SELECT payload, action, rule_version, occurred_at
      INTO v_payload, v_action, v_rule, v_time
      FROM public.audit_event WHERE id = NEW.audit_event_id;
    IF v_action IS DISTINCT FROM 'cash_reconciliation_projection_activation.record'
       OR v_rule IS DISTINCT FROM 'ledgerbridge.cash-reconciliation-projection-activation.v1'
       OR v_payload->>'revision' IS DISTINCT FROM NEW.revision::text
       OR v_payload->>'status' IS DISTINCT FROM NEW.status
       OR v_time IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'cash reconciliation projection activation audit binding is invalid'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER validate_cash_reconciliation_projection_activation
BEFORE INSERT ON public.cash_reconciliation_projection_activation
FOR EACH ROW EXECUTE FUNCTION public.r1_validate_cash_reconciliation_projection_activation();
CREATE TRIGGER cash_reconciliation_projection_activation_append_only
BEFORE UPDATE OR DELETE ON public.cash_reconciliation_projection_activation
FOR EACH ROW EXECUTE FUNCTION public.r1_bank_statement_append_only();
REVOKE ALL ON public.cash_reconciliation_projection_activation
FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;

CREATE FUNCTION internal_import.activate_cash_reconciliation_single_source(
    p_actor_ref text, p_reason text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE v_audit uuid; v_revision integer; v_status text;
BEGIN
    IF p_actor_ref IS NULL OR btrim(p_actor_ref) = '' OR length(p_actor_ref) > 200
       OR p_reason IS NULL OR btrim(p_reason) = '' OR length(p_reason) > 1000 THEN
        RAISE EXCEPTION 'cash reconciliation activation command is invalid'
            USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'cash-reconciliation-single-source-activation', 0));
    SELECT status INTO v_status FROM public.cash_reconciliation_projection_activation
     ORDER BY revision DESC LIMIT 1;
    IF v_status = 'ACTIVE' THEN
        RETURN jsonb_build_object('status', 'ACTIVE', 'created', false);
    END IF;
    SELECT COALESCE(max(revision), 0) + 1 INTO v_revision
      FROM public.cash_reconciliation_projection_activation;
    v_audit := public.append_audit_event(
        p_actor_ref, 'cash_reconciliation_projection_activation.record', p_reason,
        'ledgerbridge.cash-reconciliation-projection-activation.v1',
        jsonb_build_object('revision', v_revision, 'status', 'ACTIVE'));
    INSERT INTO public.cash_reconciliation_projection_activation(
        revision, status, audit_event_id, created_at)
    VALUES (v_revision, 'ACTIVE', v_audit,
        (SELECT occurred_at FROM public.audit_event WHERE id = v_audit));
    RETURN jsonb_build_object('status', 'ACTIVE', 'created', true);
END
$function$;
REVOKE ALL ON FUNCTION internal_import.activate_cash_reconciliation_single_source(text,text)
FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_app;
GRANT EXECUTE ON FUNCTION internal_import.activate_cash_reconciliation_single_source(text,text)
TO ledgerbridge_worker;

DO $seed$
DECLARE v_item record; v_audit uuid;
BEGIN
    FOR v_item IN SELECT * FROM (VALUES
        ('FLIGGY','PLATFORM_ROOM_REVENUE','飞猪'),
        ('MEITUAN','PLATFORM_ROOM_REVENUE','美团'),
        ('CTRIP','PLATFORM_ROOM_REVENUE','携程'),
        ('PAYROLL','PAYROLL','工资'),
        ('FINANCING','FINANCING','融资'),
        ('BOTTLED_WATER','BOTTLED_WATER','瓶装水'),
        ('INTERNAL_TRANSFER','INTERNAL_TRANSFER','内部资金归集'),
        ('RENT','RENT','房租'),
        ('BANK_INTEREST','BANK_INTEREST','银行利息'),
        ('LINEN_LAUNDRY','LINEN_LAUNDRY','布草洗涤')
        ,('WENJIE_RENT','RENTAL_INCOME','文杰房租')
        ,('XINHUA_DORM_RENT_UTILITIES','RENTAL_INCOME','新华宿舍房租水电')
        ,('MOONCAKE','OPERATING_FEE','月饼')
        ,('HOTEL_SUPPLIES','OPERATING_FEE','酒店用品')
        ,('FRESH_FOOD','OPERATING_FEE','生鲜')
        ,('INSURANCE','OPERATING_FEE','保险费')
        ,('OPERATING_FEE','OPERATING_FEE','运营费')
    ) AS seeded(item_code, category_code, item_label)
    LOOP
        v_audit := public.append_audit_event(
            'migration:20260904_0042', 'company_transaction_reporting_item.record',
            'seed versioned reporting item registry',
            'ledgerbridge.company-transaction-reporting-item.v1',
            jsonb_build_object('item_code', v_item.item_code, 'revision', 1,
                'status', 'ACTIVE', 'category_code', v_item.category_code,
                'item_label', v_item.item_label, 'match_counterparty_name', NULL)
        );
        INSERT INTO public.company_transaction_reporting_item(
            item_code, revision, status, category_code, item_label,
            match_counterparty_name, audit_event_id, created_at)
        VALUES (v_item.item_code, 1, 'ACTIVE', v_item.category_code,
            v_item.item_label, NULL, v_audit,
            (SELECT occurred_at FROM public.audit_event WHERE id = v_audit));
    END LOOP;
END
$seed$;

DO $aliases$
DECLARE v_match record; v_audit uuid;
BEGIN
    FOR v_match IN SELECT * FROM (VALUES
        ('COUNTERPARTY_NAME','CONTAINS','支付宝支付','FLIGGY'),
        ('COUNTERPARTY_NAME','CONTAINS','飞猪','FLIGGY'),
        ('TRANSACTION_NAME','CONTAINS','飞猪','FLIGGY'),
        ('TRANSACTION_NAME','CONTAINS','房款结算','FLIGGY'),
        ('COUNTERPARTY_NAME','CONTAINS','美团','MEITUAN'),
        ('TRANSACTION_NAME','CONTAINS','美团','MEITUAN'),
        ('COUNTERPARTY_NAME','CONTAINS','携程','CTRIP'),
        ('TRANSACTION_NAME','CONTAINS','携程','CTRIP')
    ) AS aliases(match_field, match_mode, match_value, item_code)
    LOOP
        v_audit := public.append_audit_event(
            'migration:20260904_0042',
            'company_transaction_reporting_item_match.record',
            'seed approved platform reporting-item alias',
            'ledgerbridge.company-transaction-reporting-item.v1',
            jsonb_build_object('category_code', 'PLATFORM_ROOM_REVENUE',
                'match_field', v_match.match_field, 'match_mode', v_match.match_mode,
                'match_value', v_match.match_value,
                'item_code', v_match.item_code, 'item_revision', 1));
        INSERT INTO public.company_transaction_reporting_item_match(
            category_code, match_field, match_mode, match_value,
            item_code, item_revision, audit_event_id, created_at)
        VALUES ('PLATFORM_ROOM_REVENUE', v_match.match_field, v_match.match_mode,
            v_match.match_value, v_match.item_code, 1, v_audit,
            (SELECT occurred_at FROM public.audit_event WHERE id = v_audit));
    END LOOP;
END
$aliases$;

DO $activation$
DECLARE v_audit uuid;
BEGIN
    v_audit := public.append_audit_event(
        'migration:20260904_0042', 'cash_reconciliation_projection_activation.record',
        'Hold single-source projection until controlled backfill commits.',
        'ledgerbridge.cash-reconciliation-projection-activation.v1',
        jsonb_build_object('revision', 1, 'status', 'PENDING'));
    INSERT INTO public.cash_reconciliation_projection_activation(
        revision, status, audit_event_id, created_at)
    VALUES (1, 'PENDING', v_audit,
        (SELECT occurred_at FROM public.audit_event WHERE id = v_audit));
END
$activation$;

ALTER TABLE public.company_transaction_classification
    ADD COLUMN reporting_item_code varchar(100),
    ADD COLUMN reporting_item_revision integer,
    ADD CONSTRAINT company_transaction_classification_reporting_item_pair_check CHECK (
        (reporting_item_code IS NULL AND reporting_item_revision IS NULL)
        OR (reporting_item_code IS NOT NULL AND btrim(reporting_item_code) <> ''
            AND reporting_item_revision > 0)),
    ADD CONSTRAINT company_transaction_classification_reporting_item_fk
        FOREIGN KEY (category_code, reporting_item_code, reporting_item_revision)
        REFERENCES public.company_transaction_reporting_item(category_code, item_code, revision);
ALTER TABLE public.company_transaction_classification
    DROP CONSTRAINT company_transaction_classification_source_check;
ALTER TABLE public.company_transaction_classification
    ADD CONSTRAINT company_transaction_classification_source_check
        CHECK (source IN ('AUTO_RULE','HUMAN_REVIEW','BACKFILL'));
ALTER TABLE public.company_transaction_classification
    DROP CONSTRAINT company_transaction_classification_check1;
ALTER TABLE public.company_transaction_classification
    ADD CONSTRAINT company_transaction_classification_check1 CHECK (
        (revision = 1 AND source = 'AUTO_RULE' AND assertion_jti IS NULL
            AND workload_principal_ref IS NULL AND expected_revision IS NULL)
        OR (revision > 1 AND source = 'HUMAN_REVIEW' AND assertion_jti IS NOT NULL
            AND btrim(workload_principal_ref) <> '' AND expected_revision = revision - 1
            AND status = 'CONFIRMED')
        OR (revision > 1 AND source = 'BACKFILL' AND assertion_jti IS NULL
            AND workload_principal_ref IS NULL AND expected_revision = revision - 1
            AND status = 'CONFIRMED' AND reporting_item_code IS NOT NULL
            AND reporting_item_revision IS NOT NULL)
    );

CREATE OR REPLACE FUNCTION public.r1_validate_company_transaction_classification()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
AS $function$
DECLARE
    v_action text; v_rule text; v_payload jsonb; v_actor text;
    v_audit_time timestamptz; v_expected_revision integer; v_previous_status text;
BEGIN
    SELECT action, rule_version, payload, actor, occurred_at
      INTO v_action, v_rule, v_payload, v_actor, v_audit_time
      FROM public.audit_event WHERE id = NEW.audit_event_id;
    IF v_action IS DISTINCT FROM (CASE
            WHEN NEW.revision = 1 THEN 'company_transaction_classification.record'
            WHEN NEW.source = 'BACKFILL'
                THEN 'company_transaction_classification.reporting_item.backfill'
            ELSE 'company_transaction_classification.review' END)
       OR v_rule IS DISTINCT FROM 'ledgerbridge.company-transaction-classification.v1'
       OR v_actor IS DISTINCT FROM NEW.actor_ref
       OR v_payload->>'transaction_ref' IS DISTINCT FROM NEW.transaction_ref::text
       OR v_payload->>'revision' IS DISTINCT FROM NEW.revision::text
       OR v_payload->>'status' IS DISTINCT FROM NEW.status
       OR v_payload->>'category_code' IS DISTINCT FROM NEW.category_code
       OR v_payload->>'reporting_item_code' IS DISTINCT FROM NEW.reporting_item_code
       OR v_payload->>'reporting_item_revision' IS DISTINCT FROM NEW.reporting_item_revision::text
       OR v_payload->>'source' IS DISTINCT FROM NEW.source
       OR v_payload->>'rule_version' IS DISTINCT FROM NEW.rule_version
       OR v_payload->>'operation_id' IS DISTINCT FROM NEW.operation_id::text
       OR v_payload->>'command_sha256' IS DISTINCT FROM encode(NEW.command_sha256, 'hex')
       OR v_audit_time IS DISTINCT FROM NEW.classified_at THEN
        RAISE EXCEPTION 'company transaction classification audit binding is invalid'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.revision > 1 THEN
        SELECT revision, status INTO v_expected_revision, v_previous_status
          FROM public.company_transaction_classification
         WHERE transaction_ref = NEW.transaction_ref
         ORDER BY revision DESC LIMIT 1;
        IF v_expected_revision IS DISTINCT FROM NEW.expected_revision
           OR NEW.revision IS DISTINCT FROM v_expected_revision + 1
           OR (v_previous_status IS DISTINCT FROM 'PENDING'
               AND NEW.source IS DISTINCT FROM 'BACKFILL') THEN
            RAISE EXCEPTION 'company transaction classification revision chain is invalid'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION internal_import.resolve_company_transaction_reporting_item(
    p_transaction_ref uuid, p_category_code text, p_actor_ref text, p_reason text
) RETURNS TABLE(item_code text, item_revision integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_transaction public.bank_statement_transaction%ROWTYPE;
    v_fixed_code text; v_code text; v_audit uuid;
BEGIN
    SELECT * INTO v_transaction FROM public.bank_statement_transaction
     WHERE transaction_ref = p_transaction_ref;
    IF NOT FOUND THEN RETURN; END IF;
    v_fixed_code := CASE p_category_code
        WHEN 'PAYROLL' THEN 'PAYROLL' WHEN 'FINANCING' THEN 'FINANCING'
        WHEN 'BOTTLED_WATER' THEN 'BOTTLED_WATER'
        WHEN 'INTERNAL_TRANSFER' THEN 'INTERNAL_TRANSFER'
        WHEN 'RENT' THEN 'RENT' WHEN 'BANK_INTEREST' THEN 'BANK_INTEREST'
        WHEN 'LINEN_LAUNDRY' THEN 'LINEN_LAUNDRY' ELSE NULL END;
    IF p_category_code = 'RELATED_PARTY_CURRENT'
       AND v_transaction.counterparty_name IS NOT NULL
       AND btrim(v_transaction.counterparty_name) <> ''
       AND length('COUNTERPARTY:' || btrim(v_transaction.counterparty_name)) <= 100 THEN
        v_code := 'COUNTERPARTY:' || btrim(v_transaction.counterparty_name);
        PERFORM pg_advisory_xact_lock(hashtextextended(
            p_category_code || ':' || v_code, 0));
        IF NOT EXISTS (SELECT 1 FROM public.company_transaction_reporting_item item
            WHERE item.category_code = p_category_code AND item.item_code = v_code
              AND item.revision = (SELECT max(latest.revision)
                  FROM public.company_transaction_reporting_item latest
                 WHERE latest.item_code = item.item_code)
              AND item.status = 'ACTIVE') THEN
            v_audit := public.append_audit_event(
                p_actor_ref, 'company_transaction_reporting_item.record', p_reason,
                'ledgerbridge.company-transaction-reporting-item.v1',
                jsonb_build_object('item_code', v_code, 'revision', 1,
                    'status', 'ACTIVE', 'category_code', p_category_code,
                    'item_label', btrim(v_transaction.counterparty_name),
                    'match_counterparty_name', btrim(v_transaction.counterparty_name)));
            INSERT INTO public.company_transaction_reporting_item(
                item_code, revision, status, category_code, item_label,
                match_counterparty_name, audit_event_id, created_at)
            VALUES (v_code, 1, 'ACTIVE', p_category_code,
                btrim(v_transaction.counterparty_name), btrim(v_transaction.counterparty_name),
                v_audit, (SELECT occurred_at FROM public.audit_event WHERE id = v_audit));
        END IF;
        v_fixed_code := v_code;
    END IF;
    RETURN QUERY
    SELECT registry.item_code::text, registry.revision
      FROM public.company_transaction_reporting_item registry
     WHERE registry.category_code = p_category_code
       AND registry.revision = (SELECT max(latest.revision)
            FROM public.company_transaction_reporting_item latest
           WHERE latest.item_code = registry.item_code)
       AND registry.status = 'ACTIVE'
       AND (registry.item_code = v_fixed_code OR (v_fixed_code IS NULL AND (
            registry.match_counterparty_name IS NOT DISTINCT FROM v_transaction.counterparty_name
            OR EXISTS (SELECT 1 FROM public.company_transaction_reporting_item_match match
                WHERE match.category_code = p_category_code
                  AND match.item_code = registry.item_code
                  AND match.item_revision = registry.revision
                  AND CASE match.match_field
                    WHEN 'COUNTERPARTY_NAME' THEN CASE match.match_mode
                      WHEN 'EXACT' THEN v_transaction.counterparty_name = match.match_value
                      WHEN 'CONTAINS' THEN position(match.match_value in
                        COALESCE(v_transaction.counterparty_name, '')) > 0 END
                    WHEN 'TRANSACTION_NAME' THEN CASE match.match_mode
                      WHEN 'EXACT' THEN v_transaction.transaction_name = match.match_value
                      WHEN 'CONTAINS' THEN position(match.match_value in
                        COALESCE(v_transaction.transaction_name, '')) > 0 END
                  END))))
     ORDER BY registry.revision DESC LIMIT 1;
END
$function$;
REVOKE ALL ON FUNCTION internal_import.resolve_company_transaction_reporting_item(
    uuid,text,text,text) FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_app;

CREATE FUNCTION internal_import.backfill_company_transaction_reporting_item(
    p_transaction_ref uuid,
    p_expected_revision integer,
    p_expected_category_code text,
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
       OR p_reporting_item_code IS NULL OR btrim(p_reporting_item_code) = ''
       OR length(p_reporting_item_code) > 100
       OR p_operation_id IS NULL OR p_actor_ref IS NULL OR btrim(p_actor_ref) = ''
       OR p_reason IS NULL OR btrim(p_reason) = '' THEN
        RAISE EXCEPTION 'company transaction reporting item is invalid'
            USING ERRCODE = '22023';
    END IF;
    v_command := public.digest(convert_to(jsonb_build_array(
        p_transaction_ref, p_expected_revision, p_expected_category_code,
        p_reporting_item_code, p_actor_ref, p_reason)::text, 'UTF8'), 'sha256');
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
        RAISE EXCEPTION 'company transaction reporting item requires current confirmed classification'
            USING ERRCODE = 'LB004';
    END IF;
    IF v_current.reporting_item_code IS NOT NULL THEN
        RAISE EXCEPTION 'company transaction reporting item is already assigned'
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
            'reporting_item_code', p_reporting_item_code,
            'reporting_item_revision', v_item_revision,
            'source', 'BACKFILL',
            'rule_version', 'reporting-item-backfill.v1',
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
        p_reporting_item_code, v_item_revision, 'BACKFILL', 'reporting-item-backfill.v1',
        p_operation_id, NULL, p_actor_ref, NULL, p_expected_revision, v_command, v_audit,
        (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
    );
    RETURN jsonb_build_object('transaction_ref', p_transaction_ref,
        'reporting_item_code', p_reporting_item_code, 'created', true);
END
$function$;

REVOKE ALL ON FUNCTION internal_import.backfill_company_transaction_reporting_item(
    uuid,integer,text,text,uuid,text,text)
FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_app;
GRANT EXECUTE ON FUNCTION internal_import.backfill_company_transaction_reporting_item(
    uuid,integer,text,text,uuid,text,text) TO ledgerbridge_worker;

CREATE OR REPLACE FUNCTION internal_import.seed_company_transaction_classification(
    p_transaction_ref uuid, p_operation_id uuid, p_status text,
    p_category_code text, p_actor_ref text, p_reason text, p_rule_version text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_existing public.company_transaction_classification%ROWTYPE;
    v_command bytea; v_audit uuid; v_item_code text; v_item_revision integer;
BEGIN
    IF p_transaction_ref IS NULL OR p_operation_id IS NULL OR p_status IS NULL
       OR p_status NOT IN ('PENDING','CONFIRMED')
       OR ((p_status = 'PENDING') <> (p_category_code IS NULL))
       OR p_category_code IS NOT NULL AND p_category_code NOT IN (
            'PLATFORM_ROOM_REVENUE','RELATED_PARTY_CURRENT','PAYROLL','FINANCING',
            'BOTTLED_WATER','INTERNAL_TRANSFER','RENT','RENTAL_INCOME','BANK_INTEREST',
            'LINEN_LAUNDRY','OPERATING_FEE')
       OR p_actor_ref IS NULL OR btrim(p_actor_ref) = '' OR length(p_actor_ref) > 200
       OR p_reason IS NULL OR btrim(p_reason) = '' OR length(p_reason) > 1000
       OR p_rule_version IS DISTINCT FROM 'company-bank-classification.2026-09.v1' THEN
        RAISE EXCEPTION 'company transaction classification seed is invalid'
            USING ERRCODE = 'LB003';
    END IF;
    v_command := public.digest(convert_to(jsonb_build_array(
        p_transaction_ref, p_operation_id, p_status, p_category_code,
        p_actor_ref, p_reason, p_rule_version)::text, 'UTF8'), 'sha256');
    PERFORM pg_advisory_xact_lock(hashtextextended(p_transaction_ref::text, 0));
    SELECT * INTO v_existing FROM public.company_transaction_classification
     WHERE operation_id = p_operation_id;
    IF FOUND THEN
        IF v_existing.transaction_ref IS DISTINCT FROM p_transaction_ref
           OR v_existing.status IS DISTINCT FROM p_status
           OR v_existing.category_code IS DISTINCT FROM p_category_code
           OR v_existing.command_sha256 IS DISTINCT FROM v_command THEN
            RAISE EXCEPTION 'company transaction classification idempotency conflict'
                USING ERRCODE = 'LB001';
        END IF;
        RETURN jsonb_build_object('transaction_ref', p_transaction_ref,
            'status', p_status, 'revision', v_existing.revision, 'created', false);
    END IF;
    IF EXISTS (SELECT 1 FROM public.company_transaction_classification
                WHERE transaction_ref = p_transaction_ref) THEN
        RAISE EXCEPTION 'company transaction is already classified' USING ERRCODE = 'LB003';
    END IF;
    IF p_status = 'CONFIRMED' THEN
        SELECT * INTO v_item_code, v_item_revision
          FROM internal_import.resolve_company_transaction_reporting_item(
            p_transaction_ref, p_category_code, p_actor_ref, p_reason);
    END IF;
    v_audit := public.append_audit_event(
        p_actor_ref, 'company_transaction_classification.record', p_reason,
        'ledgerbridge.company-transaction-classification.v1',
        jsonb_build_object('transaction_ref', p_transaction_ref, 'revision', 1,
            'status', p_status, 'category_code', p_category_code,
            'reporting_item_code', v_item_code,
            'reporting_item_revision', v_item_revision,
            'source', 'AUTO_RULE', 'rule_version', p_rule_version,
            'operation_id', p_operation_id, 'command_sha256', encode(v_command, 'hex')));
    INSERT INTO public.company_transaction_classification(
        transaction_ref, revision, status, category_code, reporting_item_code,
        reporting_item_revision, source, rule_version, operation_id, actor_ref,
        command_sha256, audit_event_id, classified_at)
    VALUES (p_transaction_ref, 1, p_status, p_category_code, v_item_code,
        v_item_revision, 'AUTO_RULE', p_rule_version, p_operation_id, p_actor_ref,
        v_command, v_audit, (SELECT occurred_at FROM public.audit_event WHERE id = v_audit));
    RETURN jsonb_build_object('transaction_ref', p_transaction_ref,
        'status', p_status, 'revision', 1, 'reporting_item_code', v_item_code,
        'reporting_item_revision', v_item_revision, 'created', true);
END
$function$;

CREATE OR REPLACE FUNCTION internal_command.review_company_transaction_classification(
    p_transaction_ref uuid, p_entity_ref uuid, p_operation_id uuid,
    p_assertion_jti uuid, p_actor_ref text, p_workload_principal_ref text,
    p_expected_revision integer, p_category_code text, p_reason text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_existing public.company_transaction_classification%ROWTYPE;
    v_current public.company_transaction_classification%ROWTYPE;
    v_command bytea; v_audit uuid; v_revision integer;
    v_item_code text; v_item_revision integer;
BEGIN
    IF p_transaction_ref IS NULL OR p_entity_ref IS NULL OR p_operation_id IS NULL
       OR p_assertion_jti IS NULL OR p_expected_revision IS NULL OR p_expected_revision < 1
       OR p_category_code IS NULL OR p_category_code NOT IN (
            'PLATFORM_ROOM_REVENUE','RELATED_PARTY_CURRENT','PAYROLL','FINANCING',
            'BOTTLED_WATER','INTERNAL_TRANSFER','RENT','RENTAL_INCOME','BANK_INTEREST',
            'LINEN_LAUNDRY','OPERATING_FEE')
       OR p_actor_ref IS NULL OR btrim(p_actor_ref) = '' OR length(p_actor_ref) > 200
       OR p_workload_principal_ref IS NULL OR btrim(p_workload_principal_ref) = ''
       OR length(p_workload_principal_ref) > 200
       OR p_reason IS NULL OR btrim(p_reason) = '' OR length(p_reason) > 1000 THEN
        RAISE EXCEPTION 'company transaction review command is invalid' USING ERRCODE = 'LB003';
    END IF;
    v_command := public.digest(convert_to(jsonb_build_array(
        p_transaction_ref, p_entity_ref, p_operation_id, p_assertion_jti,
        p_actor_ref, p_workload_principal_ref, p_expected_revision,
        p_category_code, p_reason)::text, 'UTF8'), 'sha256');
    PERFORM pg_advisory_xact_lock(hashtextextended(p_transaction_ref::text, 0));
    SELECT * INTO v_existing FROM public.company_transaction_classification
     WHERE operation_id = p_operation_id OR assertion_jti = p_assertion_jti
     ORDER BY operation_id = p_operation_id DESC LIMIT 1;
    IF FOUND THEN
        IF v_existing.transaction_ref IS DISTINCT FROM p_transaction_ref
           OR v_existing.category_code IS DISTINCT FROM p_category_code
           OR v_existing.command_sha256 IS DISTINCT FROM v_command THEN
            RAISE EXCEPTION 'company transaction review idempotency conflict'
                USING ERRCODE = 'LB001';
        END IF;
        RETURN jsonb_build_object('transaction_ref', p_transaction_ref,
            'status', v_existing.status, 'category_code', v_existing.category_code,
            'reporting_item_code', v_existing.reporting_item_code,
            'reporting_item_revision', v_existing.reporting_item_revision,
            'revision', v_existing.revision, 'created', false);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.bank_statement_transaction transaction
        JOIN public.managed_account account
          ON account.managed_account_ref = transaction.managed_account_ref
        WHERE transaction.transaction_ref = p_transaction_ref
          AND account.entity_id = p_entity_ref AND account.owner_kind = 'COMPANY') THEN
        RAISE EXCEPTION 'company transaction is outside authorized entity' USING ERRCODE = 'LB004';
    END IF;
    SELECT * INTO v_current FROM public.company_transaction_classification
     WHERE transaction_ref = p_transaction_ref ORDER BY revision DESC LIMIT 1 FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'company transaction classification is not visible' USING ERRCODE = 'LB004';
    END IF;
    IF v_current.revision IS DISTINCT FROM p_expected_revision THEN
        RAISE EXCEPTION 'company transaction classification revision is stale' USING ERRCODE = 'LB002';
    END IF;
    IF v_current.status IS DISTINCT FROM 'PENDING' THEN
        RAISE EXCEPTION 'company transaction classification is already terminal' USING ERRCODE = 'LB003';
    END IF;
    SELECT * INTO v_item_code, v_item_revision
      FROM internal_import.resolve_company_transaction_reporting_item(
        p_transaction_ref, p_category_code, p_actor_ref, p_reason);
    v_revision := v_current.revision + 1;
    v_audit := public.append_audit_event(
        p_actor_ref, 'company_transaction_classification.review', p_reason,
        'ledgerbridge.company-transaction-classification.v1',
        jsonb_build_object('transaction_ref', p_transaction_ref, 'revision', v_revision,
            'status', 'CONFIRMED', 'category_code', p_category_code,
            'reporting_item_code', v_item_code,
            'reporting_item_revision', v_item_revision,
            'source', 'HUMAN_REVIEW', 'rule_version', 'human-review.v1',
            'operation_id', p_operation_id, 'assertion_jti', p_assertion_jti,
            'workload_principal_ref', p_workload_principal_ref,
            'expected_revision', p_expected_revision,
            'command_sha256', encode(v_command, 'hex')));
    INSERT INTO public.company_transaction_classification(
        transaction_ref, revision, status, category_code, reporting_item_code,
        reporting_item_revision, source, rule_version, operation_id, assertion_jti,
        actor_ref, workload_principal_ref, expected_revision, command_sha256,
        audit_event_id, classified_at)
    VALUES (p_transaction_ref, v_revision, 'CONFIRMED', p_category_code,
        v_item_code, v_item_revision, 'HUMAN_REVIEW', 'human-review.v1',
        p_operation_id, p_assertion_jti, p_actor_ref, p_workload_principal_ref,
        p_expected_revision, v_command, v_audit,
        (SELECT occurred_at FROM public.audit_event WHERE id = v_audit));
    RETURN jsonb_build_object('transaction_ref', p_transaction_ref,
        'status', 'CONFIRMED', 'category_code', p_category_code,
        'reporting_item_code', v_item_code,
        'reporting_item_revision', v_item_revision,
        'revision', v_revision, 'created', true);
END
$function$;

CREATE OR REPLACE FUNCTION internal_read.cash_reconciliation_month_v2(
    p_month date,
    p_entity_ids uuid[],
    p_business_unit_ids uuid[]
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_result jsonb;
BEGIN
    IF p_month IS NULL OR p_month IS DISTINCT FROM date_trunc('month', p_month)::date THEN
        RAISE EXCEPTION 'cash reconciliation month must be the first day of a month'
            USING ERRCODE = '22023';
    END IF;
    IF p_entity_ids IS NULL OR cardinality(p_entity_ids) = 0
       OR cardinality(p_entity_ids) > 100
       OR array_position(p_entity_ids, NULL) IS NOT NULL
       OR cardinality(p_entity_ids) IS DISTINCT FROM (
            SELECT count(DISTINCT value) FROM unnest(p_entity_ids) AS items(value)
       ) THEN
        RAISE EXCEPTION 'cash reconciliation entity scope is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF p_business_unit_ids IS NULL OR cardinality(p_business_unit_ids) = 0
       OR cardinality(p_business_unit_ids) > 1000
       OR array_position(p_business_unit_ids, NULL) IS NOT NULL
       OR cardinality(p_business_unit_ids) IS DISTINCT FROM (
            SELECT count(DISTINCT value) FROM unnest(p_business_unit_ids) AS items(value)
       ) THEN
        RAISE EXCEPTION 'cash reconciliation business-unit scope is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM unnest(p_business_unit_ids) AS allowed(id)
          LEFT JOIN public.business_unit unit ON unit.id = allowed.id
         WHERE unit.id IS NULL OR NOT (unit.entity_id = ANY(p_entity_ids))
    ) THEN
        RAISE EXCEPTION 'cash reconciliation business-unit scope is outside entity scope'
            USING ERRCODE = '22023';
    END IF;
    IF (SELECT activation.status
          FROM public.cash_reconciliation_projection_activation activation
         ORDER BY activation.revision DESC LIMIT 1) IS DISTINCT FROM 'ACTIVE' THEN
        RAISE EXCEPTION 'cash reconciliation single-source projection is not activated'
            USING ERRCODE = '55000';
    END IF;
    WITH bounds AS (
        SELECT p_month AS month_start, (p_month + interval '1 month')::date AS month_end
    ), latest_rules AS (
        SELECT DISTINCT ON (rule_id) rule.*
          FROM public.cash_reconciliation_rule rule
         ORDER BY rule_id, revision DESC
    ), active_rules AS (
        SELECT * FROM latest_rules WHERE status = 'ACTIVE'
    ), latest_candidates AS (
        SELECT DISTINCT ON (revision.candidate_id) revision.*
          FROM public.candidate_revision revision
         ORDER BY revision.candidate_id, revision.revision DESC
    ), rules AS (
        SELECT rule.*
          FROM active_rules rule, bounds month
         WHERE rule.effective_from < month.month_end
           AND (rule.effective_to IS NULL OR rule.effective_to >= month.month_start)
           AND (
                (rule.source_kind = 'BANK_TRANSACTION' AND EXISTS (
                    SELECT 1
                      FROM public.managed_account account
                     WHERE account.account_key = rule.account_key
                       AND account.owner_kind = 'PERSONAL'
                       AND account.entity_id = ANY(p_entity_ids)
                ))
                OR
                (rule.source_kind = 'CANDIDATE' AND EXISTS (
                    SELECT 1
                      FROM public.candidate_source source
                      JOIN public.candidate candidate ON candidate.id = source.candidate_id
                      JOIN latest_candidates latest ON latest.candidate_id = candidate.id
                     WHERE source.source_system_id = rule.source_system_id
                       AND candidate.entity_id = ANY(p_entity_ids)
                       AND latest.business_unit_id = ANY(p_business_unit_ids)
                ))
           )
    ), required_entities AS (
        SELECT DISTINCT account.entity_id
          FROM active_rules rule
          JOIN public.managed_account account ON account.account_key = rule.account_key
         WHERE rule.source_kind = 'BANK_TRANSACTION'
        UNION
        SELECT DISTINCT candidate.entity_id
          FROM active_rules rule
          JOIN public.candidate_source source ON source.source_system_id = rule.source_system_id
          JOIN public.candidate candidate ON candidate.id = source.candidate_id
         WHERE rule.source_kind = 'CANDIDATE'
    ), confirmed_statements AS (
        SELECT statement.statement_ref
          FROM public.bank_statement statement
         WHERE (
            SELECT review.status
              FROM public.bank_statement_review review
             WHERE review.statement_ref = statement.statement_ref
             ORDER BY review.revision DESC
             LIMIT 1
         ) = 'CONFIRMED'
    ), personal_bank_universe AS (
        SELECT DISTINCT
               'BANK_TRANSACTION'::text AS source_kind,
               'BANK_TRANSACTION:' || transaction.transaction_ref::text AS unique_fact_ref,
               transaction.transaction_ref::text AS fact_ref,
               (transaction.occurred_at AT TIME ZONE 'Asia/Shanghai')::date AS occurred_on,
               'DAY'::text AS time_granularity,
               transaction.amount_minor,
               transaction.counterparty_name,
               transaction.transaction_name,
               transaction.counterparty_institution,
               account.account_key,
               NULL::text[] AS source_system_ids
          FROM public.bank_statement_transaction transaction
          JOIN public.managed_account account
            ON account.managed_account_ref = transaction.managed_account_ref
          JOIN bounds month
            ON (transaction.occurred_at AT TIME ZONE 'Asia/Shanghai')::date >= month.month_start
           AND (transaction.occurred_at AT TIME ZONE 'Asia/Shanghai')::date < month.month_end
         WHERE account.owner_kind = 'PERSONAL'
           AND account.entity_id = ANY(p_entity_ids)
           AND account.account_key IN (
                SELECT rule.account_key FROM rules rule WHERE rule.source_kind = 'BANK_TRANSACTION'
           )
           AND EXISTS (
                SELECT 1
                  FROM public.bank_statement_observation observation
                  JOIN confirmed_statements confirmed
                    ON confirmed.statement_ref = observation.statement_ref
                 WHERE observation.transaction_ref = transaction.transaction_ref
                   AND observation.managed_account_ref = transaction.managed_account_ref
           )
    ), candidate_universe AS (
        SELECT
               'CANDIDATE'::text AS source_kind,
               'CANDIDATE:' || latest.candidate_id::text AS unique_fact_ref,
               latest.candidate_id::text AS fact_ref,
               latest.accounting_month AS occurred_on,
               'MONTH'::text AS time_granularity,
               latest.amount_minor,
               latest.summary AS counterparty_name,
               string_agg(DISTINCT source.display_label, ' ' ORDER BY source.display_label)
                   AS transaction_name,
               NULL::text AS counterparty_institution,
               NULL::text AS account_key,
               array_agg(DISTINCT source.source_system_id ORDER BY source.source_system_id)
                   AS source_system_ids
          FROM latest_candidates latest
          JOIN public.candidate candidate ON candidate.id = latest.candidate_id
          JOIN public.candidate_source source ON source.candidate_id = latest.candidate_id
          JOIN bounds month ON latest.accounting_month = month.month_start
         WHERE latest.status = 'CONFIRMED'
           AND candidate.entity_id = ANY(p_entity_ids)
           AND latest.business_unit_id = ANY(p_business_unit_ids)
         GROUP BY latest.candidate_id, latest.accounting_month, latest.amount_minor,
                  latest.summary
        HAVING bool_or(source.source_system_id IN (
            SELECT rule.source_system_id FROM rules rule WHERE rule.source_kind = 'CANDIDATE'
        ))
    ), personal_universe AS (
        SELECT * FROM personal_bank_universe
        UNION ALL
        SELECT * FROM candidate_universe
    ), hits AS (
        SELECT fact.source_kind, fact.unique_fact_ref, fact.fact_ref, fact.occurred_on,
               fact.amount_minor, rule.rule_key
          FROM personal_universe fact
          JOIN rules rule
            ON (rule.source_kind = 'BANK_TRANSACTION'
                AND fact.source_kind = 'BANK_TRANSACTION'
                AND rule.account_key = fact.account_key)
            OR (rule.source_kind = 'CANDIDATE'
                AND fact.source_kind = 'CANDIDATE'
                AND rule.source_system_id = ANY(fact.source_system_ids))
          JOIN bounds month ON true
         WHERE concat_ws(' ', fact.counterparty_name, fact.transaction_name,
                          fact.counterparty_institution) ~* rule.match_pattern
           AND (rule.amount_direction = 'ANY'
                OR (rule.amount_direction = 'CREDIT' AND fact.amount_minor > 0)
                OR (rule.amount_direction = 'DEBIT' AND fact.amount_minor < 0))
           AND (
                (fact.time_granularity = 'DAY'
                 AND fact.occurred_on >= rule.effective_from
                 AND (rule.effective_to IS NULL OR fact.occurred_on <= rule.effective_to))
                OR
                (fact.time_granularity = 'MONTH'
                 AND rule.effective_from < month.month_end
                 AND (rule.effective_to IS NULL OR rule.effective_to >= month.month_start))
           )
    ), match_counts AS (
        SELECT fact.source_kind, fact.unique_fact_ref, fact.fact_ref, fact.occurred_on,
               fact.amount_minor, count(hit.rule_key)::integer AS match_count,
               COALESCE(
                    array_agg(hit.rule_key ORDER BY hit.rule_key)
                        FILTER (WHERE hit.rule_key IS NOT NULL),
                    ARRAY[]::varchar[]
               ) AS matched_rule_keys
          FROM personal_universe fact
          LEFT JOIN hits hit ON hit.unique_fact_ref = fact.unique_fact_ref
         GROUP BY fact.source_kind, fact.unique_fact_ref, fact.fact_ref,
                  fact.occurred_on, fact.amount_minor
    ), unique_hits AS (
        SELECT hit.rule_key, hit.fact_ref, hit.occurred_on, hit.amount_minor
          FROM hits hit
          JOIN match_counts counts ON counts.unique_fact_ref = hit.unique_fact_ref
         WHERE counts.match_count = 1
    ), personal_aggregates AS (
        SELECT rule.rule_key, rule.flow_kind, rule.business_unit_label, rule.item_label,
               rule.source_kind,
               COALESCE(rule.account_key, rule.source_system_id)::text AS source_ref,
               count(hit.fact_ref)::integer AS transaction_count,
               COALESCE(sum(abs(hit.amount_minor)), 0)::bigint AS amount_minor,
               COALESCE(
                    jsonb_agg(jsonb_build_object(
                        'fact_ref', hit.fact_ref,
                        'occurred_on', hit.occurred_on,
                        'amount_minor', hit.amount_minor
                    ) ORDER BY hit.occurred_on, hit.fact_ref)
                        FILTER (WHERE hit.fact_ref IS NOT NULL),
                    '[]'::jsonb
               ) AS facts
          FROM rules rule
          LEFT JOIN unique_hits hit ON hit.rule_key = rule.rule_key
         GROUP BY rule.rule_key, rule.flow_kind, rule.business_unit_label,
                  rule.item_label, rule.source_kind, rule.account_key, rule.source_system_id
    ), company_universe AS (
        SELECT DISTINCT
               transaction.transaction_ref::text AS fact_ref,
               'BANK_TRANSACTION:' || transaction.transaction_ref::text AS unique_fact_ref,
               (transaction.occurred_at AT TIME ZONE 'Asia/Shanghai')::date AS occurred_on,
               transaction.amount_minor,
               assignment.business_unit_id,
               assignment.business_unit_label_snapshot AS business_unit_label,
               classification.status AS classification_status,
               classification.category_code,
               classification.reporting_item_code,
               classification.reporting_item_revision,
               CASE classification.category_code
                   WHEN 'PLATFORM_ROOM_REVENUE' THEN 'INCOME'
                   WHEN 'RENTAL_INCOME' THEN 'INCOME'
                   WHEN 'BANK_INTEREST' THEN 'INCOME'
                   WHEN 'PAYROLL' THEN 'EXPENSE'
                   WHEN 'BOTTLED_WATER' THEN 'EXPENSE'
                   WHEN 'LINEN_LAUNDRY' THEN 'EXPENSE'
                   WHEN 'RENT' THEN 'EXPENSE'
                   WHEN 'OPERATING_FEE' THEN 'EXPENSE'
                   WHEN 'RELATED_PARTY_CURRENT' THEN 'CURRENT'
                   WHEN 'FINANCING' THEN 'CURRENT'
                   WHEN 'INTERNAL_TRANSFER' THEN 'CURRENT'
               END AS flow_kind,
               registry.item_label
          FROM public.bank_statement_transaction transaction
          JOIN public.managed_account account
            ON account.managed_account_ref = transaction.managed_account_ref
          JOIN bounds month
            ON (transaction.occurred_at AT TIME ZONE 'Asia/Shanghai')::date >= month.month_start
           AND (transaction.occurred_at AT TIME ZONE 'Asia/Shanghai')::date < month.month_end
          JOIN LATERAL (
                SELECT item.business_unit_id, item.business_unit_label_snapshot
                  FROM public.account_business_unit_assignment item
                 WHERE item.managed_account_ref = account.managed_account_ref
                   AND item.owner_entity_id = account.entity_id
                   AND item.effective_from <=
                       (transaction.occurred_at AT TIME ZONE 'Asia/Shanghai')::date
                   AND (item.effective_to IS NULL OR item.effective_to >
                       (transaction.occurred_at AT TIME ZONE 'Asia/Shanghai')::date)
                 ORDER BY item.effective_from DESC, item.assignment_ref
                 LIMIT 1
          ) assignment ON assignment.business_unit_id = ANY(p_business_unit_ids)
          LEFT JOIN LATERAL (
                SELECT item.status, item.category_code, item.reporting_item_code,
                       item.reporting_item_revision
                  FROM public.company_transaction_classification item
                 WHERE item.transaction_ref = transaction.transaction_ref
                 ORDER BY item.revision DESC
                 LIMIT 1
          ) classification ON true
          LEFT JOIN public.company_transaction_reporting_item registry
            ON registry.category_code = classification.category_code
           AND registry.item_code = classification.reporting_item_code
           AND registry.revision = classification.reporting_item_revision
         WHERE account.owner_kind = 'COMPANY'
           AND account.entity_id = ANY(p_entity_ids)
           AND EXISTS (
                SELECT 1
                  FROM public.bank_statement_observation observation
                  JOIN confirmed_statements confirmed
                    ON confirmed.statement_ref = observation.statement_ref
                 WHERE observation.transaction_ref = transaction.transaction_ref
                   AND observation.managed_account_ref = transaction.managed_account_ref
           )
    ), company_aggregates AS (
        SELECT 'classification:' || md5(business_unit_id::text || ':' || category_code || ':' || item_label)
                   AS rule_key,
               flow_kind, business_unit_label, item_label,
               'BANK_TRANSACTION'::text AS source_kind,
               'company_transaction_classification:' || category_code
                   || COALESCE(':' || reporting_item_code, '') AS source_ref,
               count(*)::integer AS transaction_count,
               greatest(CASE flow_kind
                   WHEN 'INCOME' THEN sum(amount_minor)
                   WHEN 'EXPENSE' THEN sum(-amount_minor)
                   ELSE sum(abs(amount_minor))
               END, 0)::bigint AS amount_minor,
               jsonb_agg(jsonb_build_object(
                    'fact_ref', fact_ref,
                    'occurred_on', occurred_on,
                    'amount_minor', amount_minor
               ) ORDER BY occurred_on, fact_ref) AS facts
          FROM company_universe
         WHERE classification_status = 'CONFIRMED'
           AND category_code IS NOT NULL
           AND reporting_item_code IS NOT NULL
           AND reporting_item_revision IS NOT NULL
           AND flow_kind IS NOT NULL
           AND item_label IS NOT NULL
        GROUP BY business_unit_id, business_unit_label, category_code,
                  reporting_item_code, reporting_item_revision, flow_kind, item_label
    ), adjustment_rows AS (
        SELECT 'adjustment:' || adjustment.adjustment_id::text AS rule_key,
               adjustment.flow_kind, adjustment.business_unit_label, adjustment.item_label,
               'ADJUSTMENT'::text AS source_kind, 'manual_adjustment'::text AS source_ref,
               1::integer AS transaction_count, adjustment.amount_minor,
               jsonb_build_array(jsonb_build_object(
                    'fact_ref', adjustment.adjustment_id::text,
                    'occurred_on', adjustment.accounting_month,
                    'amount_minor', adjustment.amount_minor
               )) AS facts
          FROM public.cash_reconciliation_adjustment adjustment
          JOIN public.cash_reconciliation_adjustment_scope adjustment_scope
            ON adjustment_scope.adjustment_id = adjustment.adjustment_id
          JOIN bounds month ON adjustment.accounting_month = month.month_start
         WHERE adjustment_scope.entity_id = ANY(p_entity_ids)
           AND adjustment_scope.business_unit_id = ANY(p_business_unit_ids)
    ), all_rows AS (
        SELECT * FROM personal_aggregates
        UNION ALL
        SELECT * FROM company_aggregates
        UNION ALL
        SELECT * FROM adjustment_rows
    ), all_issues AS (
        SELECT counts.source_kind, counts.unique_fact_ref, counts.fact_ref,
               counts.occurred_on, counts.amount_minor, counts.match_count,
               counts.matched_rule_keys
          FROM match_counts counts
         WHERE counts.match_count <> 1
        UNION ALL
        SELECT 'BANK_TRANSACTION'::text, company.unique_fact_ref, company.fact_ref,
               company.occurred_on, company.amount_minor, 0::integer,
               ARRAY[]::varchar[]
          FROM company_universe company
         WHERE company.classification_status IS DISTINCT FROM 'CONFIRMED'
            OR company.category_code IS NULL
            OR company.reporting_item_code IS NULL
            OR company.flow_kind IS NULL
            OR company.item_label IS NULL
    ), issue_rows AS (
        SELECT * FROM all_issues
         ORDER BY occurred_on, unique_fact_ref
         LIMIT 500
    )
    SELECT jsonb_build_object(
        'contract_version', 'ledgerbridge.cash-reconciliation.v2',
        'accounting_month', to_char((SELECT month_start FROM bounds), 'YYYY-MM'),
        'rules', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'rule_key', rule.rule_key,
                'source_kind', rule.source_kind,
                'source_ref', COALESCE(rule.account_key, rule.source_system_id),
                'flow_kind', rule.flow_kind,
                'business_unit_label', rule.business_unit_label,
                'item_label', rule.item_label,
                'match_pattern', rule.match_pattern,
                'amount_direction', rule.amount_direction,
                'effective_from', rule.effective_from,
                'effective_to', rule.effective_to
            ) ORDER BY rule.business_unit_label, rule.flow_kind, rule.item_label, rule.rule_key)
              FROM rules rule
        ), '[]'::jsonb),
        'rows', COALESCE((
            SELECT jsonb_agg(to_jsonb(row) ORDER BY row.business_unit_label,
                             row.flow_kind, row.item_label, row.rule_key)
              FROM all_rows row
        ), '[]'::jsonb),
        'issues', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'issue_kind', CASE WHEN issue.match_count = 0
                                   THEN 'UNMATCHED' ELSE 'MULTIPLE_RULES' END,
                'source_kind', issue.source_kind,
                'fact_ref', issue.unique_fact_ref,
                'occurred_on', issue.occurred_on,
                'amount_minor', issue.amount_minor,
                'matched_rule_keys', to_jsonb(issue.matched_rule_keys)
            ) ORDER BY issue.occurred_on, issue.unique_fact_ref)
              FROM issue_rows issue
        ), '[]'::jsonb),
        'eligible_fact_count',
            (SELECT count(*) FROM match_counts) + (SELECT count(*) FROM company_universe),
        'matched_fact_count',
            (SELECT count(*) FROM match_counts WHERE match_count = 1)
            + (SELECT count(*) FROM company_universe
                WHERE classification_status = 'CONFIRMED' AND category_code IS NOT NULL
                  AND reporting_item_code IS NOT NULL
                  AND flow_kind IS NOT NULL AND item_label IS NOT NULL),
        'unmatched_fact_count',
            (SELECT count(*) FROM match_counts WHERE match_count = 0)
            + (SELECT count(*) FROM company_universe
                WHERE classification_status IS DISTINCT FROM 'CONFIRMED'
                   OR category_code IS NULL
                   OR reporting_item_code IS NULL
                   OR flow_kind IS NULL
                   OR item_label IS NULL),
        'conflicted_fact_count',
            (SELECT count(*) FROM match_counts WHERE match_count > 1),
        'issue_count', (SELECT count(*) FROM all_issues),
        'issues_truncated', (SELECT count(*) > 500 FROM all_issues),
        'totals', jsonb_build_object(
            'income_minor', COALESCE((SELECT sum(amount_minor) FROM all_rows WHERE flow_kind = 'INCOME'), 0),
            'expense_minor', COALESCE((SELECT sum(amount_minor) FROM all_rows WHERE flow_kind = 'EXPENSE'), 0),
            'current_minor', COALESCE((SELECT sum(amount_minor) FROM all_rows WHERE flow_kind = 'CURRENT'), 0)
        )
    ) INTO v_result;

    RETURN v_result;
END;
$function$;

REVOKE ALL ON FUNCTION internal_read.cash_reconciliation_month_v2(date, uuid[], uuid[])
FROM PUBLIC, ledgerbridge_api, ledgerbridge_worker;
GRANT EXECUTE ON FUNCTION internal_read.cash_reconciliation_month_v2(date, uuid[], uuid[])
TO ledgerbridge_reader;
"""


_DOWNGRADE_SQL = r"""
-- The previous function body is restored by re-running migration 0038 during
-- a controlled rollback. Automatic downgrade is deliberately refused because
-- it would re-enable duplicate company classification rules.
DO $function$
BEGIN
    RAISE EXCEPTION 'cash reconciliation single-source downgrade requires controlled 0038 restore';
END;
$function$;
"""
