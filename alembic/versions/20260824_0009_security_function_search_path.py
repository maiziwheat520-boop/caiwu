"""Harden previously deployed trigger functions against pg_temp shadowing.

Revision ID: 20260824_0009
Revises: 20260823_0008
Create Date: 2026-08-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260824_0009"
down_revision: str | None = "20260823_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $ledgerbridge$
        DECLARE
            v_missing text[];
        BEGIN
            SELECT array_agg(signature ORDER BY signature)
              INTO v_missing
              FROM unnest(ARRAY[
                  'public.account_block_protected_dimension_change()',
                  'public.append_audit_event(text,text,text,text,jsonb)',
                  'public.audit_event_block_mutation()',
                  'public.import_job_enforce_transition()',
                  'public.journal_entry_assert_posted_complete()',
                  'public.journal_entry_block_posted_mutation()',
                  'public.journal_entry_validate_post_audit()',
                  'public.journal_entry_validate_relationships()',
                  'public.posting_assert_balanced()',
                  'public.posting_block_posted_mutation()',
                  'public.posting_enforce_entity()',
                  'public.raw_artifact_block_mutation()',
                  'public.raw_artifact_validate_audit()',
                  'public.source_record_block_mutation()'
              ]::text[]) AS required(signature)
             WHERE to_regprocedure(signature) IS NULL;

            IF v_missing IS NOT NULL THEN
                RAISE EXCEPTION
                    'required security functions are missing: %',
                    array_to_string(v_missing, ', ');
            END IF;
        END
        $ledgerbridge$;

        ALTER FUNCTION public.append_audit_event(text, text, text, text, jsonb)
            SET search_path = pg_catalog;
        ALTER FUNCTION public.audit_event_block_mutation()
            SET search_path = pg_catalog;
        ALTER FUNCTION public.journal_entry_block_posted_mutation()
            SET search_path = pg_catalog;
        ALTER FUNCTION public.raw_artifact_block_mutation()
            SET search_path = pg_catalog;
        ALTER FUNCTION public.source_record_block_mutation()
            SET search_path = pg_catalog;
        ALTER FUNCTION public.import_job_enforce_transition()
            SET search_path = pg_catalog;

        CREATE OR REPLACE FUNCTION public.account_block_protected_dimension_change()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            IF NEW.entity_id IS DISTINCT FROM OLD.entity_id THEN
                RAISE EXCEPTION 'account entity_id is immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF NEW.account_class IS DISTINCT FROM OLD.account_class
               AND EXISTS (
                   SELECT 1
                   FROM public.posting AS p
                   JOIN public.journal_entry AS j ON j.id = p.entry_id
                   WHERE p.account_id = OLD.id
                     AND j.status = 'POSTED'
               ) THEN
                RAISE EXCEPTION
                    'account_class is immutable after POSTED use'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE OR REPLACE FUNCTION public.journal_entry_validate_relationships()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_target_entity uuid;
            v_target_status public.journal_status;
            v_target_id uuid;
        BEGIN
            IF NEW.adjusts_entry_id IS NOT NULL THEN
                v_target_id := NEW.adjusts_entry_id;
            ELSE
                v_target_id := NEW.reverses_entry_id;
            END IF;

            IF v_target_id IS NOT NULL THEN
                SELECT entity_id, status
                  INTO v_target_entity, v_target_status
                  FROM public.journal_entry
                 WHERE id = v_target_id;

                IF NOT FOUND THEN
                    RAISE EXCEPTION 'correction target % does not exist', v_target_id
                        USING ERRCODE = 'foreign_key_violation';
                END IF;
                IF v_target_entity <> NEW.entity_id THEN
                    RAISE EXCEPTION 'correction target must belong to the same entity'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF v_target_status <> 'POSTED' THEN
                    RAISE EXCEPTION 'correction target must be POSTED'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE OR REPLACE FUNCTION public.posting_enforce_entity()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_entry_entity uuid;
            v_account_entity uuid;
        BEGIN
            SELECT entity_id
              INTO v_entry_entity
              FROM public.journal_entry
             WHERE id = NEW.entry_id;

            SELECT entity_id
              INTO v_account_entity
              FROM public.account
             WHERE id = NEW.account_id;

            IF v_entry_entity IS NULL OR v_account_entity IS NULL THEN
                RAISE EXCEPTION 'posting references a missing entry or account'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            IF v_entry_entity <> v_account_entity THEN
                RAISE EXCEPTION 'posting entry and account must belong to the same entity'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE OR REPLACE FUNCTION public.posting_block_posted_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_entry_id uuid;
            v_status public.journal_status;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                v_entry_id := OLD.entry_id;
                SELECT status
                  INTO v_status
                  FROM public.journal_entry
                 WHERE id = v_entry_id;
                IF v_status = 'POSTED' THEN
                    RAISE EXCEPTION 'postings on POSTED entries are immutable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;

            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                v_entry_id := NEW.entry_id;
                SELECT status
                  INTO v_status
                  FROM public.journal_entry
                 WHERE id = v_entry_id;
                IF v_status = 'POSTED' THEN
                    RAISE EXCEPTION 'postings on POSTED entries are immutable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END IF;
            RETURN OLD;
        END
        $function$;

        CREATE OR REPLACE FUNCTION public.posting_assert_balanced()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_entry_ids uuid[] := ARRAY[]::uuid[];
            v_entry_id uuid;
            v_currency text;
            v_total bigint;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                v_entry_ids := array_append(v_entry_ids, OLD.entry_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                v_entry_ids := array_append(v_entry_ids, NEW.entry_id);
            END IF;

            FOREACH v_entry_id IN ARRAY v_entry_ids LOOP
                SELECT p.currency, SUM(p.amount_minor)
                  INTO v_currency, v_total
                  FROM public.posting AS p
                 WHERE p.entry_id = v_entry_id
                 GROUP BY p.currency
                HAVING SUM(p.amount_minor) <> 0
                 LIMIT 1;

                IF FOUND THEN
                    RAISE EXCEPTION
                        'journal entry % is unbalanced for currency %: % minor units',
                        v_entry_id,
                        v_currency,
                        v_total
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END LOOP;

            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE OR REPLACE FUNCTION public.journal_entry_assert_posted_complete()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_posting_count bigint;
            v_mismatched_account uuid;
        BEGIN
            IF NEW.status = 'POSTED' THEN
                SELECT COUNT(*)
                  INTO v_posting_count
                  FROM public.posting
                 WHERE entry_id = NEW.id;
                IF v_posting_count < 2 THEN
                    RAISE EXCEPTION 'POSTED journal entries require at least two postings'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;

                SELECT p.account_id
                  INTO v_mismatched_account
                  FROM public.posting AS p
                  JOIN public.account AS a ON a.id = p.account_id
                 WHERE p.entry_id = NEW.id
                   AND a.entity_id <> NEW.entity_id
                 ORDER BY p.account_id
                 LIMIT 1
                   FOR SHARE OF a;
                IF FOUND THEN
                    RAISE EXCEPTION
                        'POSTED journal entry has an account from another entity: %',
                        v_mismatched_account
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            RETURN NEW;
        END
        $function$;

        DO $ledgerbridge$
        DECLARE
            v_unhardened text[];
        BEGIN
            SELECT array_agg(signature ORDER BY signature)
              INTO v_unhardened
              FROM unnest(ARRAY[
                  'public.account_block_protected_dimension_change()',
                  'public.append_audit_event(text,text,text,text,jsonb)',
                  'public.audit_event_block_mutation()',
                  'public.import_job_enforce_transition()',
                  'public.journal_entry_assert_posted_complete()',
                  'public.journal_entry_block_posted_mutation()',
                  'public.journal_entry_validate_post_audit()',
                  'public.journal_entry_validate_relationships()',
                  'public.posting_assert_balanced()',
                  'public.posting_block_posted_mutation()',
                  'public.posting_enforce_entity()',
                  'public.raw_artifact_block_mutation()',
                  'public.raw_artifact_validate_audit()',
                  'public.source_record_block_mutation()'
              ]::text[]) AS required(signature)
              JOIN pg_proc AS function_definition
                ON function_definition.oid = to_regprocedure(signature)
             WHERE NOT (
                 COALESCE(function_definition.proconfig, ARRAY[]::text[])
                 @> ARRAY['search_path=pg_catalog']::text[]
             );

            IF v_unhardened IS NOT NULL THEN
                RAISE EXCEPTION
                    'security functions lack fixed search_path: %',
                    array_to_string(v_unhardened, ', ');
            END IF;
        END
        $ledgerbridge$;
        """
    )


def downgrade() -> None:
    # Keep the hardened definitions in place when stepping back to 0008.  They
    # are API-compatible with 0008, and restoring vulnerable definitions would
    # reopen the pg_temp shadow-table bypass.  Earlier schema downgrades still
    # drop the functions in their normal dependency order.
    return None
