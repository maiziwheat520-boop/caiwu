"""Expose classified payroll disbursements as a source-backed read model.

Revision ID: 20260905_0046
Revises: 20260904_0045
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260905_0046"
down_revision: str | None = "20260904_0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


_UPGRADE_SQL = r"""
CREATE FUNCTION internal_read.list_payroll_disbursement_records_as_of(
    p_entity_ref uuid, p_pay_period text, p_audit_horizon_sequence bigint,
    p_audit_horizon_hash bytea, p_limit integer
) RETURNS TABLE(item jsonb) LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
BEGIN
    IF p_entity_ref IS NULL
       OR p_pay_period IS NULL OR p_pay_period !~ '^20[0-9]{2}-(0[1-9]|1[0-2])$'
       OR p_audit_horizon_sequence IS NULL OR p_audit_horizon_hash IS NULL
       OR octet_length(p_audit_horizon_hash) <> 32
       OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 500
       OR NOT EXISTS (SELECT 1 FROM public.audit_event
            WHERE sequence = p_audit_horizon_sequence AND hash = p_audit_horizon_hash) THEN
        RAISE EXCEPTION 'payroll disbursement query is invalid'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT jsonb_build_object(
        'record_ref', transaction.transaction_ref,
        'entity_ref', account.entity_id,
        'company_name', entity.name,
        'pay_period', p_pay_period,
        'occurred_at', transaction.occurred_at,
        'actual_amount_minor', abs(transaction.amount_minor),
        'direction', CASE WHEN transaction.amount_minor < 0 THEN 'OUTFLOW'
                          WHEN transaction.amount_minor > 0 THEN 'INFLOW' ELSE 'ZERO' END,
        'currency', transaction.currency,
        'source_channel', CASE account.institution_code
                              WHEN 'mybank' THEN 'MYBANK'
                              WHEN 'boc' THEN 'BOC'
                              ELSE 'BANK' END,
        'source_system', source.source_system,
        'source_artifact_ref', source.evidence_ref,
        'source_statement_ref', source.statement_ref,
        'source_row_number', source.source_row_number,
        'ingested_at', source.created_at,
        'managed_account_ref', account.managed_account_ref,
        'disbursement_account_masked', '****' || account.account_suffix,
        'counterparty_name', CASE
            WHEN projection_audit.sequence <= p_audit_horizon_sequence
                THEN projection.counterparty_name
            WHEN correction_audit.sequence <= p_audit_horizon_sequence
                THEN correction.counterparty_name
            ELSE transaction.counterparty_name END,
        'counterparty_account_masked', CASE
            WHEN length(regexp_replace(coalesce(effective.counterparty_account, ''),
                                       '[^0-9]', '', 'g')) < 4 THEN NULL
            ELSE '****' || right(regexp_replace(effective.counterparty_account,
                                                '[^0-9]', '', 'g'), 4) END,
        'transaction_name', CASE
            WHEN projection_audit.sequence <= p_audit_horizon_sequence
                THEN projection.transaction_name
            ELSE transaction.transaction_name END,
        'classification_revision', current.revision,
        'classification_source', current.source,
        'classification_rule_version', current.rule_version,
        'period_assignment_source', 'NEXT_MONTH_RULE',
        'period_assignment_rule_version',
            'payroll-next-month-disbursement.2026-09.v1',
        'parse_status', 'PARSED',
        'link_status', CASE WHEN transaction.amount_minor < 0
                            THEN 'UNMATCHED' ELSE 'UNSUPPORTED_DIRECTION' END,
        'payable', false,
        'submission_supported', false
    )
      FROM public.bank_statement_transaction AS transaction
      JOIN public.managed_account AS account
        ON account.managed_account_ref = transaction.managed_account_ref
      JOIN public.entity AS entity ON entity.id = account.entity_id
      JOIN public.audit_event AS transaction_audit
        ON transaction_audit.id = transaction.audit_event_id
      JOIN LATERAL (
            SELECT classification.*
              FROM public.company_transaction_classification AS classification
              JOIN public.audit_event AS classification_audit
                ON classification_audit.id = classification.audit_event_id
             WHERE classification.transaction_ref = transaction.transaction_ref
               AND classification_audit.sequence <= p_audit_horizon_sequence
             ORDER BY classification.revision DESC LIMIT 1
      ) AS current ON true
      JOIN LATERAL (
            SELECT statement.statement_ref, statement.evidence_ref,
                   statement.source_system, observation.source_row_number,
                   statement.created_at
              FROM public.bank_statement_observation AS observation
              JOIN public.bank_statement AS statement
                ON statement.statement_ref = observation.statement_ref
               AND statement.managed_account_ref = observation.managed_account_ref
              JOIN public.audit_event AS statement_audit
                ON statement_audit.id = statement.audit_event_id
              JOIN public.audit_event AS observation_audit
                ON observation_audit.id = observation.audit_event_id
              JOIN LATERAL (
                    SELECT review.status
                      FROM public.bank_statement_review AS review
                      JOIN public.audit_event AS review_audit
                        ON review_audit.id = review.audit_event_id
                     WHERE review.statement_ref = statement.statement_ref
                       AND review_audit.sequence <= p_audit_horizon_sequence
                     ORDER BY review.revision DESC LIMIT 1
              ) AS latest_review ON true
             WHERE observation.transaction_ref = transaction.transaction_ref
               AND statement_audit.sequence <= p_audit_horizon_sequence
               AND observation_audit.sequence <= p_audit_horizon_sequence
               AND latest_review.status = 'CONFIRMED'
             ORDER BY statement.period_end DESC, statement.statement_ref DESC
             LIMIT 1
      ) AS source ON true
      LEFT JOIN public.bank_statement_transaction_correction AS correction
        USING(transaction_ref)
      LEFT JOIN public.audit_event AS correction_audit
        ON correction_audit.id = correction.audit_event_id
      LEFT JOIN public.bank_statement_transaction_projection_correction AS projection
        USING(transaction_ref)
      LEFT JOIN public.audit_event AS projection_audit
        ON projection_audit.id = projection.audit_event_id
      CROSS JOIN LATERAL (
            SELECT CASE
                WHEN projection_audit.sequence <= p_audit_horizon_sequence
                    THEN projection.counterparty_account
                WHEN correction_audit.sequence <= p_audit_horizon_sequence
                    THEN coalesce(correction.counterparty_account,
                                  transaction.counterparty_account)
                ELSE transaction.counterparty_account END AS counterparty_account
      ) AS effective
     WHERE account.entity_id = p_entity_ref
       AND account.owner_kind = 'COMPANY'
       AND transaction_audit.sequence <= p_audit_horizon_sequence
       AND current.status = 'CONFIRMED' AND current.category_code = 'PAYROLL'
       AND to_char(
            (transaction.occurred_at AT TIME ZONE 'Asia/Shanghai') - interval '1 month',
            'YYYY-MM'
       ) = p_pay_period
     ORDER BY transaction.occurred_at, transaction.transaction_ref
     LIMIT p_limit;
END
$function$;

REVOKE ALL ON FUNCTION internal_read.list_payroll_disbursement_records_as_of(
    uuid,text,bigint,bytea,integer
) FROM PUBLIC, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;
GRANT EXECUTE ON FUNCTION internal_read.list_payroll_disbursement_records_as_of(
    uuid,text,bigint,bytea,integer
) TO ledgerbridge_reader;
"""


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP FUNCTION internal_read.list_payroll_disbursement_records_as_of("
            "uuid,text,bigint,bytea,integer)"
        )
    )
