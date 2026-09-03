"""Automatically classify exact approved company patterns after statement confirmation.

Revision ID: 20260904_0040
Revises: 20260904_0039
"""

from alembic import op

revision = "20260904_0040"
down_revision = "20260904_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS auto_classify_confirmed_company_statement "
        "ON public.bank_statement_review; "
        "DROP FUNCTION IF EXISTS internal_import.auto_classify_confirmed_company_statement();"
    )


_UPGRADE_SQL = r"""
CREATE FUNCTION internal_import.auto_classify_confirmed_company_statement()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    item record; v_category text; v_matches integer; v_hex text; v_operation uuid;
BEGIN
    IF NEW.status <> 'CONFIRMED' THEN
        RETURN NEW;
    END IF;
    FOR item IN
        SELECT DISTINCT transaction.transaction_ref, transaction.amount_minor,
               (transaction.occurred_at AT TIME ZONE 'Asia/Shanghai')::date AS occurred_on,
               regexp_replace(coalesce(transaction.counterparty_name,''), '\s', '', 'g')
                   AS normalized_counterparty,
               regexp_replace(concat_ws('|', transaction.counterparty_name,
                   transaction.transaction_name), '\s', '', 'g') AS normalized
          FROM public.bank_statement_observation AS observation
          JOIN public.bank_statement_transaction AS transaction
            ON transaction.transaction_ref = observation.transaction_ref
          JOIN public.managed_account AS account
            ON account.managed_account_ref = transaction.managed_account_ref
         WHERE observation.statement_ref = NEW.statement_ref
           AND account.owner_kind = 'COMPANY'
           AND NOT EXISTS (
               SELECT 1 FROM public.company_transaction_classification AS existing
                WHERE existing.transaction_ref = transaction.transaction_ref
           )
         ORDER BY transaction.transaction_ref
    LOOP
        v_matches := 0;
        v_category := NULL;

        -- AUTO-P03: company credit containing 陈明哲 -> owner current account.
        IF item.occurred_on >= DATE '2026-09-04'
           AND item.amount_minor > 0 AND item.normalized_counterparty = '陈明哲' THEN
            v_matches := v_matches + 1;
            v_category := 'RELATED_PARTY_CURRENT';
        END IF;
        -- AUTO-P04: payroll clearing account + batch payroll, including refunds.
        IF item.occurred_on >= DATE '2026-09-04'
           AND item.normalized LIKE '%企业代发过渡户%'
           AND item.normalized LIKE '%批量代发%' THEN
            v_matches := v_matches + 1;
            v_category := 'PAYROLL';
        END IF;
        -- AUTO-P06: MYbank loan repayment debit -> financing.
        IF item.occurred_on >= DATE '2026-09-04'
           AND item.amount_minor < 0 AND item.normalized LIKE '%浙江网商银行%'
           AND item.normalized LIKE '%贷款还款%' THEN
            v_matches := v_matches + 1;
            v_category := 'FINANCING';
        END IF;

        IF v_matches > 1 THEN
            RAISE EXCEPTION 'company auto-classification rules overlap for transaction %',
                item.transaction_ref USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF v_matches = 1 THEN
            v_hex := encode(public.digest(convert_to(
                'company-auto-classification.2026-09.v1:' ||
                item.transaction_ref::text || ':' || v_category, 'UTF8'), 'sha256'), 'hex');
            v_operation := (substring(v_hex,1,8)||'-'||substring(v_hex,9,4)||'-'||
                substring(v_hex,13,4)||'-'||substring(v_hex,17,4)||'-'||
                substring(v_hex,21,12))::uuid;
            PERFORM internal_import.seed_company_transaction_classification(
                item.transaction_ref, v_operation, 'CONFIRMED', v_category,
                'system:company-auto-classification',
                'confirmed statement matched user-approved AUTO-P03/P04/P06 rule',
                'company-bank-classification.2026-09.v1'
            );
        END IF;
    END LOOP;
    RETURN NEW;
END
$function$;

CREATE TRIGGER auto_classify_confirmed_company_statement
AFTER INSERT ON public.bank_statement_review
FOR EACH ROW EXECUTE FUNCTION internal_import.auto_classify_confirmed_company_statement();

REVOKE ALL ON FUNCTION internal_import.auto_classify_confirmed_company_statement()
FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;
"""
