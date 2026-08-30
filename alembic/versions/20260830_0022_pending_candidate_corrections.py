# ruff: noqa: E501

"""Allow append-only posting-field corrections on pending candidates.

Revision ID: 20260830_0022
Revises: 20260830_0021

The deployed 0016 command function only allowed ``CORRECT_AND_CONFIRM`` to fill
missing values on an INCOMPLETE candidate.  This forward migration adds an
explicit PENDING -> CONFIRMED ``CORRECT_AND_CONFIRM`` event edge while keeping
all terminal states immutable and preserving the existing ref/code registry
lookups and entity/business-unit scope checks.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260830_0022"
down_revision: str | None = "20260830_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(_event_constraints_sql(allow_pending_corrections=True))
    op.execute(_candidate_closure_sql(allow_pending_corrections=True))
    op.execute(_command_functions_sql(allow_pending_corrections=True))
    op.execute(_accounting_dimensions_sql(install=True))


def downgrade() -> None:
    op.execute(
        """
        DO $guard$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.candidate_event
                 WHERE action = 'CORRECT_AND_CONFIRM'
                   AND from_status = 'PENDING'
                   AND to_status = 'CONFIRMED'
            ) THEN
                RAISE EXCEPTION
                    'pending candidate correction events prevent destructive downgrade';
            END IF;
        END
        $guard$;
        """
    )
    op.execute(
        """
        DO $guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.audit_event)
               OR EXISTS (SELECT 1 FROM public.business_unit)
               OR EXISTS (SELECT 1 FROM public.reporting_category)
               OR EXISTS (SELECT 1 FROM public.evidence_object)
               OR EXISTS (SELECT 1 FROM public.encrypted_blob_version)
               OR EXISTS (SELECT 1 FROM public.candidate)
               OR EXISTS (SELECT 1 FROM public.candidate_source)
               OR EXISTS (SELECT 1 FROM public.candidate_revision)
               OR EXISTS (SELECT 1 FROM public.candidate_blocker)
               OR EXISTS (SELECT 1 FROM public.candidate_event)
               OR EXISTS (SELECT 1 FROM public.candidate_field_change)
               OR EXISTS (SELECT 1 FROM public.candidate_conflict_resolution)
               OR EXISTS (SELECT 1 FROM public.candidate_evidence)
               OR EXISTS (SELECT 1 FROM public.encrypted_object_identity)
               OR EXISTS (SELECT 1 FROM public.journal_entry_attribution)
               OR EXISTS (SELECT 1 FROM public.posting_attribution)
               OR EXISTS (SELECT 1 FROM public.reconciliation_snapshot)
               OR EXISTS (SELECT 1 FROM public.reconciliation_snapshot_blocker)
               OR EXISTS (SELECT 1 FROM public.reconciliation_snapshot_proposal)
               OR EXISTS (SELECT 1 FROM public.reconciliation_snapshot_suspense)
               OR EXISTS (SELECT 1 FROM public.reconciliation_leg)
               OR EXISTS (SELECT 1 FROM internal_read.evidence_read_receipt)
               OR EXISTS (SELECT 1 FROM internal_import.controlled_batch_receipt)
               OR EXISTS (SELECT 1 FROM public.candidate_evidence_link)
               OR EXISTS (SELECT 1 FROM internal_import.hotel_payout_cutover_receipt)
               OR EXISTS (SELECT 1 FROM public.counterparty_identity)
               OR EXISTS (SELECT 1 FROM public.counterparty_classification)
               OR EXISTS (SELECT 1 FROM public.candidate_counterparty)
               OR EXISTS (SELECT 1 FROM public.managed_account)
               OR EXISTS (SELECT 1 FROM public.managed_account_lifecycle)
               OR EXISTS (SELECT 1 FROM public.bank_statement)
               OR EXISTS (SELECT 1 FROM public.bank_statement_transaction)
               OR EXISTS (SELECT 1 FROM public.bank_statement_observation)
               OR EXISTS (SELECT 1 FROM public.bank_statement_review) THEN
                RAISE EXCEPTION
                    'nonempty R1 fact database prevents destructive downgrade';
            END IF;
        END
        $guard$;
        """
    )
    op.execute(_accounting_dimensions_sql(install=False))
    op.execute(_command_functions_sql(allow_pending_corrections=False))
    op.execute(_candidate_closure_sql(allow_pending_corrections=False))
    op.execute(_event_constraints_sql(allow_pending_corrections=False))


def _event_constraints_sql(*, allow_pending_corrections: bool) -> str:
    correction = ",'CORRECT_AND_CONFIRM'" if allow_pending_corrections else ""
    return f"""
        ALTER TABLE public.candidate_event
            DROP CONSTRAINT candidate_event_type_allowed,
            DROP CONSTRAINT candidate_event_action_allowed;
        ALTER TABLE public.candidate_event
            ADD CONSTRAINT candidate_event_type_allowed CHECK (
                event_type IN (
                    'CREATE','COMPLETE_FIELDS','RESOLVE_CONFLICT'{correction},
                    'CONFIRM','IGNORE','SUPERSEDE'
                )
            ),
            ADD CONSTRAINT candidate_event_action_allowed CHECK (
                action IS NULL OR action IN (
                    'COMPLETE_FIELDS','RESOLVE_CONFLICT'{correction},
                    'CONFIRM','IGNORE','SUPERSEDE'
                )
            );
    """


def _candidate_closure_sql(*, allow_pending_corrections: bool) -> str:
    old_edge = "AND v_event.action = 'CONFIRM')"
    new_edge = "AND v_event.action IN ('CONFIRM','CORRECT_AND_CONFIRM'))"
    old_change = "IF v_event.event_type = 'COMPLETE_FIELDS'\n                       AND v_normalized_changes < 1 THEN"
    new_change = "IF v_event.event_type IN ('COMPLETE_FIELDS','CORRECT_AND_CONFIRM')\n                       AND v_normalized_changes < 1 THEN"
    old_children = "IF v_event.event_type IN ('COMPLETE_FIELDS','CONFIRM','IGNORE','SUPERSEDE')"
    new_children = "IF v_event.event_type IN ('COMPLETE_FIELDS','CORRECT_AND_CONFIRM','CONFIRM','IGNORE','SUPERSEDE')"
    replacements = (
        ((old_edge, new_edge), (old_change, new_change), (old_children, new_children))
        if allow_pending_corrections
        else ((new_edge, old_edge), (new_change, old_change), (new_children, old_children))
    )
    statements = []
    for index, (source, target) in enumerate(replacements, start=1):
        statements.append(
            f"""
            IF position($source_{index}${source}$source_{index}$ IN v_definition) = 0 THEN
                RAISE EXCEPTION 'candidate closure patch source {index} is not installed';
            END IF;
            v_definition := replace(
                v_definition,
                $source_{index}${source}$source_{index}$,
                $target_{index}${target}$target_{index}$
            );
            """
        )
    return f"""
        DO $patch$
        DECLARE v_definition text;
        BEGIN
            SELECT pg_get_functiondef(
                to_regprocedure('public.r1_check_candidate_closure(uuid,uuid)')
            ) INTO v_definition;
            IF v_definition IS NULL THEN
                RAISE EXCEPTION 'candidate closure validator is not installed';
            END IF;
            {"".join(statements)}
            EXECUTE v_definition;
        END
        $patch$;
    """


def _accounting_dimensions_sql(*, install: bool) -> str:
    if not install:
        return """
            REVOKE ALL ON FUNCTION internal_read.get_accounting_dimensions(uuid, uuid[], varchar[])
                FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker;
            DO $revoke_optional$
            DECLARE role_name text;
            BEGIN
                FOREACH role_name IN ARRAY ARRAY['ledgerbridge_app','ledgerbridge_backup'] LOOP
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                        EXECUTE format(
                            'REVOKE ALL ON FUNCTION '
                            'internal_read.get_accounting_dimensions(uuid, uuid[], varchar[]) '
                            'FROM %I', role_name
                        );
                    END IF;
                END LOOP;
            END
            $revoke_optional$;
            DROP FUNCTION internal_read.get_accounting_dimensions(uuid, uuid[], varchar[]);
        """
    return """
        CREATE FUNCTION internal_read.get_accounting_dimensions(
            p_entity_id uuid,
            p_business_unit_ids uuid[],
            p_business_unit_refs varchar[]
        ) RETURNS jsonb
        LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE v_dimensions jsonb;
        BEGIN
            IF p_entity_id IS NULL OR p_business_unit_ids IS NULL
               OR p_business_unit_refs IS NULL
               OR cardinality(p_business_unit_ids) > 1000
               OR cardinality(p_business_unit_ids)
                    IS DISTINCT FROM cardinality(p_business_unit_refs)
               OR array_position(p_business_unit_ids, NULL) IS NOT NULL
               OR array_position(p_business_unit_refs, NULL) IS NOT NULL
               OR cardinality(p_business_unit_ids) IS DISTINCT FROM (
                    SELECT count(DISTINCT value)::integer
                      FROM unnest(p_business_unit_ids) AS ids(value)
               )
               OR cardinality(p_business_unit_refs) IS DISTINCT FROM (
                    SELECT count(DISTINCT value)::integer
                      FROM unnest(p_business_unit_refs) AS refs(value)
               ) THEN
                RAISE EXCEPTION 'invalid accounting dimension scope' USING ERRCODE = '22023';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM public.entity AS e WHERE e.id = p_entity_id) THEN
                RAISE EXCEPTION 'accounting dimension entity is not visible'
                    USING ERRCODE = 'LB004';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM unnest(p_business_unit_ids, p_business_unit_refs)
                       AS bindings(id, ref)
                  LEFT JOIN public.business_unit AS bu
                    ON bu.id = bindings.id
                   AND bu.entity_id = p_entity_id
                   AND bu.ref = bindings.ref
                 WHERE bu.id IS NULL
            ) THEN
                RAISE EXCEPTION 'accounting dimension registry binding has drifted'
                    USING ERRCODE = '55000';
            END IF;
            IF EXISTS (
                SELECT bu.label FROM public.business_unit AS bu
                 WHERE bu.entity_id = p_entity_id AND bu.retired_at IS NULL
                 GROUP BY bu.label HAVING count(*) > 1
            ) OR EXISTS (
                SELECT rc.label FROM public.reporting_category AS rc
                 WHERE rc.entity_id = p_entity_id AND rc.retired_at IS NULL
                 GROUP BY rc.label HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'active accounting dimension labels require registry governance'
                    USING ERRCODE = 'LB005';
            END IF;
            IF (
                SELECT count(*) FROM public.reporting_category AS rc
                 WHERE rc.entity_id = p_entity_id AND rc.retired_at IS NULL
            ) > 1000 THEN
                RAISE EXCEPTION 'accounting dimension category limit exceeded'
                    USING ERRCODE = '54000';
            END IF;
            SELECT jsonb_build_object(
                'contract_version', 'ledgerbridge.accounting-dimensions.v1',
                'entity_ref', p_entity_id,
                'business_units', coalesce((
                    SELECT jsonb_agg(
                        jsonb_build_object('ref', bu.ref, 'label', bu.label)
                        ORDER BY bu.ref
                    )
                     FROM public.business_unit AS bu
                     WHERE bu.entity_id = p_entity_id
                       AND bu.id = ANY(p_business_unit_ids)
                       AND bu.retired_at IS NULL
                ), '[]'::jsonb),
                'categories', coalesce((
                    SELECT jsonb_agg(
                        jsonb_build_object('code', rc.code, 'label', rc.label)
                        ORDER BY rc.code
                    )
                     FROM public.reporting_category AS rc
                     WHERE rc.entity_id = p_entity_id
                       AND rc.retired_at IS NULL
                ), '[]'::jsonb)
            ) INTO v_dimensions;
            RETURN v_dimensions;
        END
        $function$;

        REVOKE ALL ON FUNCTION internal_read.get_accounting_dimensions(uuid, uuid[], varchar[])
            FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker;
        DO $revoke_optional$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY['ledgerbridge_app','ledgerbridge_backup'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format(
                        'REVOKE ALL ON FUNCTION '
                        'internal_read.get_accounting_dimensions(uuid, uuid[], varchar[]) '
                        'FROM %I', role_name
                    );
                END IF;
            END LOOP;
        END
        $revoke_optional$;
        GRANT EXECUTE ON FUNCTION internal_read.get_accounting_dimensions(uuid, uuid[], varchar[])
            TO ledgerbridge_reader;
    """


def _command_functions_sql(*, allow_pending_corrections: bool) -> str:
    active_business_unit = " AND bu.retired_at IS NULL" if allow_pending_corrections else ""
    active_category = " AND rc.retired_at IS NULL" if allow_pending_corrections else ""
    dimension_label_guard = (
        """
            IF p_decision = 'CORRECT_AND_CONFIRM' AND (EXISTS (
                SELECT bu.label FROM public.business_unit AS bu
                 WHERE bu.entity_id = v_candidate.entity_id AND bu.retired_at IS NULL
                 GROUP BY bu.label HAVING count(*) > 1
            ) OR EXISTS (
                SELECT rc.label FROM public.reporting_category AS rc
                 WHERE rc.entity_id = v_candidate.entity_id AND rc.retired_at IS NULL
                 GROUP BY rc.label HAVING count(*) > 1
            )) THEN
                RAISE EXCEPTION
                    'active accounting dimension labels require registry governance'
                    USING ERRCODE = 'LB005';
            END IF;
        """
        if allow_pending_corrections
        else ""
    )
    transition_case = (
        "WHEN 'CORRECT_AND_CONFIRM' THEN 'CONFIRMED'" if allow_pending_corrections else ""
    )
    transition_edge = (
        "OR (p_action = 'CORRECT_AND_CONFIRM' AND v_previous.status = 'PENDING')"
        if allow_pending_corrections
        else ""
    )
    normalized_change_guard = (
        """
            IF p_action = 'CORRECT_AND_CONFIRM'
               AND v_previous.business_unit_ref_snapshot IS NOT DISTINCT FROM p_business_unit_ref
               AND v_previous.business_unit_label_snapshot IS NOT DISTINCT FROM p_business_unit_label
               AND v_previous.category_code_snapshot IS NOT DISTINCT FROM p_category_code
               AND v_previous.category_label_snapshot IS NOT DISTINCT FROM p_category_label
               AND v_previous.amount_minor IS NOT DISTINCT FROM p_amount_minor
               AND v_previous.accounting_month IS NOT DISTINCT FROM p_accounting_month THEN
                RAISE EXCEPTION 'pending correction must change a normalized field'
                    USING ERRCODE = 'LB003';
            END IF;
        """
        if allow_pending_corrections
        else ""
    )
    final_dimension_guard = (
        """
            IF p_decision = 'CORRECT_AND_CONFIRM' AND NOT EXISTS (
                SELECT 1 FROM public.business_unit AS bu
                 WHERE bu.id = v_business_unit_id
                   AND bu.entity_id = v_candidate.entity_id
                   AND bu.ref = v_business_unit_ref
                   AND bu.retired_at IS NULL
            ) THEN
                RAISE EXCEPTION 'final business unit is not an active candidate dimension'
                    USING ERRCODE = 'LB004';
            END IF;
            IF p_decision = 'CORRECT_AND_CONFIRM' AND NOT EXISTS (
                SELECT 1 FROM public.reporting_category AS rc
                 WHERE rc.id = v_category_id
                   AND rc.entity_id = v_candidate.entity_id
                   AND rc.code = v_category_code
                   AND rc.retired_at IS NULL
            ) THEN
                RAISE EXCEPTION 'final category is not an active candidate dimension'
                    USING ERRCODE = 'LB004';
            END IF;
        """
        if allow_pending_corrections
        else ""
    )
    correction_branch = (
        """
                IF v_current.status = 'INCOMPLETE' THEN
                    IF (p_set_business_unit AND v_current.business_unit_id IS NOT NULL)
                       OR (p_set_category AND v_current.category_id IS NOT NULL)
                       OR (p_set_amount AND v_current.amount_minor IS NOT NULL)
                       OR (p_set_month AND v_current.accounting_month IS NOT NULL)
                       OR v_business_unit_id IS NULL OR v_category_id IS NULL
                       OR v_amount_minor IS NULL OR v_accounting_month IS NULL THEN
                        RAISE EXCEPTION 'incomplete correction cannot close required fields'
                            USING ERRCODE = 'LB003';
                    END IF;
                    v_event_one := internal_command.append_candidate_transition(
                        p_candidate_id, v_current.revision, 'COMPLETE_FIELDS', p_actor_ref,
                        p_reason, p_decided_at, v_business_unit_id, v_business_unit_ref,
                        v_business_unit_label, v_category_id, v_category_code,
                        v_category_label, v_amount_minor, v_accounting_month, NULL
                    );
                    v_event_two := internal_command.append_candidate_transition(
                        p_candidate_id, v_current.revision + 1, 'CONFIRM', p_actor_ref,
                        p_reason, p_decided_at, v_business_unit_id, v_business_unit_ref,
                        v_business_unit_label, v_category_id, v_category_code,
                        v_category_label, v_amount_minor, v_accounting_month, NULL
                    );
                    v_final_revision := v_current.revision + 2;
                    v_events := ARRAY[v_event_one, v_event_two];
                ELSIF v_current.status = 'PENDING' THEN
                    IF v_business_unit_id IS NULL OR v_category_id IS NULL
                       OR v_amount_minor IS NULL OR v_accounting_month IS NULL
                       OR (
                           v_current.business_unit_id IS NOT DISTINCT FROM v_business_unit_id
                           AND v_current.business_unit_ref_snapshot IS NOT DISTINCT FROM v_business_unit_ref
                           AND v_current.business_unit_label_snapshot IS NOT DISTINCT FROM v_business_unit_label
                           AND v_current.category_id IS NOT DISTINCT FROM v_category_id
                           AND v_current.category_code_snapshot IS NOT DISTINCT FROM v_category_code
                           AND v_current.category_label_snapshot IS NOT DISTINCT FROM v_category_label
                           AND v_current.amount_minor IS NOT DISTINCT FROM v_amount_minor
                           AND v_current.accounting_month IS NOT DISTINCT FROM v_accounting_month
                       ) THEN
                        RAISE EXCEPTION 'pending correction must change a normalized field'
                            USING ERRCODE = 'LB003';
                    END IF;
                    v_event_one := internal_command.append_candidate_transition(
                        p_candidate_id, v_current.revision, 'CORRECT_AND_CONFIRM', p_actor_ref,
                        p_reason, p_decided_at, v_business_unit_id, v_business_unit_ref,
                        v_business_unit_label, v_category_id, v_category_code,
                        v_category_label, v_amount_minor, v_accounting_month, NULL
                    );
                    v_final_revision := v_current.revision + 1;
                    v_events := ARRAY[v_event_one];
                ELSE
                    RAISE EXCEPTION 'only open candidates can be corrected'
                        USING ERRCODE = 'LB003';
                END IF;
        """
        if allow_pending_corrections
        else """
                IF v_current.status IS DISTINCT FROM 'INCOMPLETE'
                   OR (p_set_business_unit AND v_current.business_unit_id IS NOT NULL)
                   OR (p_set_category AND v_current.category_id IS NOT NULL)
                   OR (p_set_amount AND v_current.amount_minor IS NOT NULL)
                   OR (p_set_month AND v_current.accounting_month IS NOT NULL)
                   OR v_business_unit_id IS NULL OR v_category_id IS NULL
                   OR v_amount_minor IS NULL OR v_accounting_month IS NULL THEN
                    RAISE EXCEPTION 'incomplete correction cannot close required fields'
                        USING ERRCODE = 'LB003';
                END IF;
                v_event_one := internal_command.append_candidate_transition(
                    p_candidate_id, v_current.revision, 'COMPLETE_FIELDS', p_actor_ref,
                    p_reason, p_decided_at, v_business_unit_id, v_business_unit_ref,
                    v_business_unit_label, v_category_id, v_category_code,
                    v_category_label, v_amount_minor, v_accounting_month, NULL
                );
                v_event_two := internal_command.append_candidate_transition(
                    p_candidate_id, v_current.revision + 1, 'CONFIRM', p_actor_ref,
                    p_reason, p_decided_at, v_business_unit_id, v_business_unit_ref,
                    v_business_unit_label, v_category_id, v_category_code,
                    v_category_label, v_amount_minor, v_accounting_month, NULL
                );
                v_final_revision := v_current.revision + 2;
                v_events := ARRAY[v_event_one, v_event_two];
        """
    )
    return f"""
        CREATE OR REPLACE FUNCTION internal_command.append_candidate_transition(
            p_candidate_id uuid, p_from_revision integer, p_action varchar(32),
            p_actor_ref varchar(200), p_reason varchar(1000), p_decided_at timestamptz,
            p_business_unit_id uuid, p_business_unit_ref varchar(100),
            p_business_unit_label varchar(200), p_category_id uuid,
            p_category_code varchar(100), p_category_label varchar(200),
            p_amount_minor bigint, p_accounting_month date,
            p_conflict_resolution varchar(1000)
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_previous public.candidate_revision%ROWTYPE;
            v_to_status varchar(16);
            v_event_ref uuid := gen_random_uuid();
            v_event_operation_id uuid := gen_random_uuid();
            v_fingerprint bytea;
            v_field_changes jsonb;
            v_conflict_resolutions jsonb := '[]'::jsonb;
            v_audit_id uuid;
        BEGIN
            SELECT * INTO STRICT v_previous
              FROM public.candidate_revision
             WHERE candidate_id = p_candidate_id AND revision = p_from_revision;
            v_to_status := CASE p_action
                WHEN 'COMPLETE_FIELDS' THEN 'PENDING'
                WHEN 'RESOLVE_CONFLICT' THEN 'PENDING'
                {transition_case}
                WHEN 'CONFIRM' THEN 'CONFIRMED'
                WHEN 'IGNORE' THEN 'IGNORED'
                ELSE NULL END;
            IF v_to_status IS NULL
               OR NOT (
                    (p_action = 'COMPLETE_FIELDS' AND v_previous.status = 'INCOMPLETE')
                 OR (p_action = 'RESOLVE_CONFLICT' AND v_previous.status = 'CONFLICTED')
                 {transition_edge}
                 OR (p_action = 'CONFIRM' AND v_previous.status = 'PENDING')
                 OR (p_action = 'IGNORE' AND v_previous.status IN ('INCOMPLETE','CONFLICTED','PENDING'))
               ) THEN
                RAISE EXCEPTION 'candidate state transition is rejected' USING ERRCODE = 'LB003';
            END IF;
            {normalized_change_guard}
            SELECT coalesce(jsonb_agg(jsonb_build_object(
                       'field', changed.field,
                       'previous_value', changed.previous_value,
                       'new_value', changed.new_value
                   ) ORDER BY changed.field), '[]'::jsonb)
              INTO v_field_changes
              FROM (VALUES
                    ('status'::text, to_jsonb(v_previous.status), to_jsonb(v_to_status)),
                    ('business_unit_ref', to_jsonb(v_previous.business_unit_ref_snapshot), to_jsonb(p_business_unit_ref)),
                    ('business_unit_label', to_jsonb(v_previous.business_unit_label_snapshot), to_jsonb(p_business_unit_label)),
                    ('category_code', to_jsonb(v_previous.category_code_snapshot), to_jsonb(p_category_code)),
                    ('category_label', to_jsonb(v_previous.category_label_snapshot), to_jsonb(p_category_label)),
                    ('amount_minor', to_jsonb(v_previous.amount_minor), to_jsonb(p_amount_minor)),
                    ('accounting_month', to_jsonb(v_previous.accounting_month), to_jsonb(p_accounting_month))
              ) AS changed(field, previous_value, new_value)
             WHERE changed.previous_value IS DISTINCT FROM changed.new_value;
            IF p_action = 'RESOLVE_CONFLICT' THEN
                SELECT coalesce(jsonb_agg(jsonb_build_object(
                           'conflict_ref', conflicts.conflict_ref,
                           'resolution', p_conflict_resolution
                       ) ORDER BY conflicts.conflict_ref), '[]'::jsonb)
                  INTO v_conflict_resolutions
                  FROM (
                      SELECT DISTINCT b.conflict_ref
                        FROM public.candidate_blocker AS b
                       WHERE b.candidate_id = p_candidate_id
                         AND b.revision = p_from_revision
                         AND b.conflict_ref IS NOT NULL
                  ) AS conflicts;
                IF v_conflict_resolutions = '[]'::jsonb THEN
                    RAISE EXCEPTION 'conflict candidate has no resolvable blocker'
                        USING ERRCODE = 'LB003';
                END IF;
            ELSIF p_conflict_resolution IS NOT NULL THEN
                RAISE EXCEPTION 'resolution is exclusive to conflict decisions'
                    USING ERRCODE = 'LB003';
            END IF;
            v_fingerprint := public.digest(convert_to(jsonb_build_object(
                'candidate_ref', p_candidate_id,
                'operation_id', v_event_operation_id,
                'action', p_action,
                'from_revision', p_from_revision,
                'to_status', v_to_status,
                'reason', p_reason,
                'field_changes', v_field_changes,
                'conflict_resolutions', v_conflict_resolutions
            )::text, 'UTF8'), 'sha256');
            v_audit_id := public.append_audit_event(
                p_actor_ref::text,
                'candidate.transition'::text,
                p_reason::text,
                'ledgerbridge.candidate-event.v1'::text,
                jsonb_build_object(
                    'event_ref', v_event_ref::text,
                    'candidate_id', p_candidate_id::text,
                    'candidate_ref', p_candidate_id::text,
                    'operation_id', v_event_operation_id::text,
                    'command_fingerprint', encode(v_fingerprint, 'hex'),
                    'event_type', p_action,
                    'action', p_action,
                    'from_revision', p_from_revision,
                    'to_revision', p_from_revision + 1,
                    'from_status', v_previous.status,
                    'to_status', v_to_status,
                    'field_changes', v_field_changes,
                    'conflict_resolutions', v_conflict_resolutions,
                    'actor_ref', p_actor_ref,
                    'reason', p_reason,
                    'derived_candidate_id', NULL
                )::jsonb
            );
            INSERT INTO public.candidate_revision (
                candidate_id, revision, status, business_unit_id,
                business_unit_ref_snapshot, business_unit_label_snapshot,
                category_id, category_code_snapshot, category_label_snapshot,
                amount_minor, currency, accounting_month, summary,
                confidence_basis_points, created_at, updated_at
            ) VALUES (
                p_candidate_id, p_from_revision + 1, v_to_status, p_business_unit_id,
                p_business_unit_ref, p_business_unit_label,
                p_category_id, p_category_code, p_category_label,
                p_amount_minor, v_previous.currency, p_accounting_month, v_previous.summary,
                v_previous.confidence_basis_points, v_previous.created_at, p_decided_at
            );
            INSERT INTO public.candidate_event (
                event_ref, candidate_id, operation_id, command_fingerprint,
                event_type, action, from_revision, to_revision, from_status, to_status,
                actor_ref, reason, occurred_at, audit_event_id
            ) VALUES (
                v_event_ref, p_candidate_id, v_event_operation_id, v_fingerprint,
                p_action, p_action, p_from_revision, p_from_revision + 1,
                v_previous.status, v_to_status, p_actor_ref, p_reason, p_decided_at, v_audit_id
            );
            INSERT INTO public.candidate_field_change (
                event_ref, field, previous_value, new_value
            )
            SELECT v_event_ref, changed.field, changed.previous_value, changed.new_value
              FROM (VALUES
                    ('status'::text, to_jsonb(v_previous.status), to_jsonb(v_to_status)),
                    ('business_unit_ref', to_jsonb(v_previous.business_unit_ref_snapshot), to_jsonb(p_business_unit_ref)),
                    ('business_unit_label', to_jsonb(v_previous.business_unit_label_snapshot), to_jsonb(p_business_unit_label)),
                    ('category_code', to_jsonb(v_previous.category_code_snapshot), to_jsonb(p_category_code)),
                    ('category_label', to_jsonb(v_previous.category_label_snapshot), to_jsonb(p_category_label)),
                    ('amount_minor', to_jsonb(v_previous.amount_minor), to_jsonb(p_amount_minor)),
                    ('accounting_month', to_jsonb(v_previous.accounting_month), to_jsonb(p_accounting_month))
              ) AS changed(field, previous_value, new_value)
             WHERE changed.previous_value IS DISTINCT FROM changed.new_value;
            IF p_action = 'RESOLVE_CONFLICT' THEN
                INSERT INTO public.candidate_conflict_resolution (
                    event_ref, conflict_ref, resolution
                )
                SELECT v_event_ref, conflicts.conflict_ref, p_conflict_resolution
                  FROM (
                      SELECT DISTINCT b.conflict_ref
                        FROM public.candidate_blocker AS b
                       WHERE b.candidate_id = p_candidate_id
                         AND b.revision = p_from_revision
                         AND b.conflict_ref IS NOT NULL
                  ) AS conflicts;
            END IF;
            RETURN v_event_operation_id;
        END
        $function$;

        CREATE OR REPLACE FUNCTION internal_command.apply_candidate_decision(
            p_operation_id uuid, p_assertion_jti uuid, p_candidate_id uuid,
            p_actor_ref varchar(200), p_workload_principal_ref varchar(200),
            p_verified_san varchar(200), p_authorized_entity_id uuid,
            p_current_business_unit_id uuid, p_target_business_unit_id uuid,
            p_decision varchar(32), p_expected_revision integer, p_reason varchar(1000),
            p_set_business_unit boolean, p_business_unit_ref varchar(100),
            p_set_category boolean, p_category_code varchar(100),
            p_set_amount boolean, p_amount_minor bigint,
            p_set_month boolean, p_accounting_month date,
            p_conflict_resolution varchar(1000), p_decided_at timestamptz
        ) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_candidate public.candidate%ROWTYPE;
            v_current public.candidate_revision%ROWTYPE;
            v_receipt internal_command.candidate_decision_receipt%ROWTYPE;
            v_existing_assertion uuid;
            v_request_fingerprint bytea;
            v_business_unit_id uuid;
            v_business_unit_ref varchar(100);
            v_business_unit_label varchar(200);
            v_category_id uuid;
            v_category_code varchar(100);
            v_category_label varchar(200);
            v_amount_minor bigint;
            v_accounting_month date;
            v_event_one uuid;
            v_event_two uuid;
            v_final_revision integer;
            v_events uuid[];
        BEGIN
            IF p_operation_id IS NULL OR p_assertion_jti IS NULL OR p_candidate_id IS NULL
               OR p_actor_ref IS NULL OR btrim(p_actor_ref) = ''
               OR p_workload_principal_ref IS NULL OR btrim(p_workload_principal_ref) = ''
               OR p_verified_san IS NULL
               OR p_verified_san !~ '^spiffe://ledgerbridge(\\.test|\\.local)?/[a-z0-9/_-]+$'
               OR p_authorized_entity_id IS NULL OR p_decision IS NULL
               OR p_decision NOT IN ('CONFIRM','IGNORE','CORRECT_AND_CONFIRM','RESOLVE_CONFLICT')
               OR p_expected_revision IS NULL OR p_expected_revision < 1
               OR p_reason IS NULL OR btrim(p_reason) = ''
               OR p_set_business_unit IS NULL OR p_set_category IS NULL
               OR p_set_amount IS NULL OR p_set_month IS NULL
               OR p_decided_at IS NULL THEN
                RAISE EXCEPTION 'invalid candidate decision request' USING ERRCODE = 'LB003';
            END IF;
            IF (p_set_business_unit AND (p_business_unit_ref IS NULL OR btrim(p_business_unit_ref) = ''))
               OR (p_set_category AND (p_category_code IS NULL OR btrim(p_category_code) = ''))
               OR (p_set_amount AND p_amount_minor IS NULL)
               OR (p_set_month AND (p_accounting_month IS NULL
                                    OR p_accounting_month <> date_trunc('month', p_accounting_month)::date))
               OR (p_amount_minor IS NOT NULL
                   AND p_amount_minor NOT BETWEEN -9007199254740991 AND 9007199254740991)
               OR (p_decision IN ('CONFIRM','IGNORE') AND (
                    p_set_business_unit OR p_set_category OR p_set_amount OR p_set_month
                    OR p_conflict_resolution IS NOT NULL))
               OR (p_decision = 'CORRECT_AND_CONFIRM' AND (
                    NOT (p_set_business_unit OR p_set_category OR p_set_amount OR p_set_month)
                    OR p_conflict_resolution IS NOT NULL))
               OR (p_decision = 'RESOLVE_CONFLICT'
                   AND (p_conflict_resolution IS NULL OR btrim(p_conflict_resolution) = '')) THEN
                RAISE EXCEPTION 'candidate decision shape is rejected' USING ERRCODE = 'LB003';
            END IF;

            PERFORM pg_advisory_xact_lock(hashtextextended(p_operation_id::text, 0));
            v_request_fingerprint := public.digest(convert_to(jsonb_build_object(
                'candidate_ref', p_candidate_id,
                'decision', p_decision,
                'expected_revision', p_expected_revision,
                'reason', p_reason,
                'set_business_unit', p_set_business_unit,
                'business_unit_ref', p_business_unit_ref,
                'set_category', p_set_category,
                'category_code', p_category_code,
                'set_amount', p_set_amount,
                'amount_minor', p_amount_minor,
                'set_month', p_set_month,
                'accounting_month', p_accounting_month,
                'conflict_resolution', p_conflict_resolution
            )::text, 'UTF8'), 'sha256');

            SELECT operation_id INTO v_existing_assertion
              FROM internal_command.candidate_assertion_use
             WHERE assertion_jti = p_assertion_jti;
            IF FOUND AND v_existing_assertion IS DISTINCT FROM p_operation_id THEN
                RAISE EXCEPTION 'assertion JTI was reused for another operation'
                    USING ERRCODE = 'LB001';
            END IF;
            SELECT * INTO v_receipt
              FROM internal_command.candidate_decision_receipt
             WHERE operation_id = p_operation_id;
            IF FOUND THEN
                IF v_receipt.candidate_id IS DISTINCT FROM p_candidate_id
                   OR v_receipt.actor_ref IS DISTINCT FROM p_actor_ref
                   OR v_receipt.workload_principal_ref IS DISTINCT FROM p_workload_principal_ref
                   OR v_receipt.verified_san IS DISTINCT FROM p_verified_san
                   OR v_receipt.request_fingerprint IS DISTINCT FROM v_request_fingerprint THEN
                    RAISE EXCEPTION 'operation ID was reused with different content or actor'
                        USING ERRCODE = 'LB001';
                END IF;
                INSERT INTO internal_command.candidate_assertion_use(assertion_jti, operation_id)
                VALUES (p_assertion_jti, p_operation_id) ON CONFLICT DO NOTHING;
                SELECT operation_id INTO v_existing_assertion
                  FROM internal_command.candidate_assertion_use
                 WHERE assertion_jti = p_assertion_jti;
                IF v_existing_assertion IS DISTINCT FROM p_operation_id THEN
                    RAISE EXCEPTION 'assertion JTI was reused for another operation'
                        USING ERRCODE = 'LB001';
                END IF;
                RETURN internal_command.render_candidate_decision_receipt(p_operation_id, true);
            END IF;

            SELECT * INTO v_candidate
              FROM public.candidate WHERE id = p_candidate_id FOR UPDATE;
            IF NOT FOUND OR v_candidate.entity_id IS DISTINCT FROM p_authorized_entity_id THEN
                RAISE EXCEPTION 'candidate is outside authorized entity scope' USING ERRCODE = 'LB004';
            END IF;
            SELECT * INTO v_current
              FROM public.candidate_revision
             WHERE candidate_id = p_candidate_id
             ORDER BY revision DESC LIMIT 1;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'candidate revision is not visible' USING ERRCODE = 'LB004';
            END IF;
            IF v_current.revision IS DISTINCT FROM p_expected_revision THEN
                RAISE EXCEPTION 'candidate revision is stale' USING ERRCODE = 'LB002';
            END IF;
            IF v_current.business_unit_id IS DISTINCT FROM p_current_business_unit_id THEN
                RAISE EXCEPTION 'candidate is outside authorized business-unit scope'
                    USING ERRCODE = 'LB004';
            END IF;
            {dimension_label_guard}

            v_business_unit_id := v_current.business_unit_id;
            v_business_unit_ref := v_current.business_unit_ref_snapshot;
            v_business_unit_label := v_current.business_unit_label_snapshot;
            v_category_id := v_current.category_id;
            v_category_code := v_current.category_code_snapshot;
            v_category_label := v_current.category_label_snapshot;
            v_amount_minor := v_current.amount_minor;
            v_accounting_month := v_current.accounting_month;
            IF p_set_business_unit THEN
                SELECT bu.id, bu.ref, bu.label
                 INTO v_business_unit_id, v_business_unit_ref, v_business_unit_label
                  FROM public.business_unit AS bu
                 WHERE bu.entity_id = v_candidate.entity_id
                   AND bu.ref = p_business_unit_ref{active_business_unit};
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'referenced business unit is not visible'
                        USING ERRCODE = 'LB004';
                END IF;
            END IF;
            IF v_business_unit_id IS DISTINCT FROM p_target_business_unit_id THEN
                RAISE EXCEPTION 'target business unit is outside authorized scope'
                    USING ERRCODE = 'LB004';
            END IF;
            IF p_set_category THEN
                SELECT rc.id, rc.code, rc.label
                  INTO v_category_id, v_category_code, v_category_label
                  FROM public.reporting_category AS rc
                 WHERE rc.entity_id = v_candidate.entity_id
                   AND rc.code = p_category_code{active_category};
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'referenced category is not visible'
                        USING ERRCODE = 'LB004';
                END IF;
            END IF;
            {final_dimension_guard}
            IF p_set_amount THEN v_amount_minor := p_amount_minor; END IF;
            IF p_set_month THEN v_accounting_month := p_accounting_month; END IF;

            IF p_decision = 'CONFIRM' THEN
                IF v_current.status IS DISTINCT FROM 'PENDING' THEN
                    RAISE EXCEPTION 'only pending candidates can be confirmed' USING ERRCODE = 'LB003';
                END IF;
                v_event_one := internal_command.append_candidate_transition(
                    p_candidate_id, v_current.revision, 'CONFIRM', p_actor_ref, p_reason,
                    p_decided_at, v_business_unit_id, v_business_unit_ref,
                    v_business_unit_label, v_category_id, v_category_code,
                    v_category_label, v_amount_minor, v_accounting_month, NULL
                );
                v_final_revision := v_current.revision + 1;
                v_events := ARRAY[v_event_one];
            ELSIF p_decision = 'IGNORE' THEN
                IF v_current.status NOT IN ('INCOMPLETE','CONFLICTED','PENDING') THEN
                    RAISE EXCEPTION 'only open candidates can be ignored' USING ERRCODE = 'LB003';
                END IF;
                v_event_one := internal_command.append_candidate_transition(
                    p_candidate_id, v_current.revision, 'IGNORE', p_actor_ref, p_reason,
                    p_decided_at, v_business_unit_id, v_business_unit_ref,
                    v_business_unit_label, v_category_id, v_category_code,
                    v_category_label, v_amount_minor, v_accounting_month, NULL
                );
                v_final_revision := v_current.revision + 1;
                v_events := ARRAY[v_event_one];
            ELSIF p_decision = 'CORRECT_AND_CONFIRM' THEN
                {correction_branch}
            ELSE
                IF v_current.status IS DISTINCT FROM 'CONFLICTED'
                   OR v_business_unit_id IS NULL OR v_category_id IS NULL
                   OR v_amount_minor IS NULL OR v_accounting_month IS NULL THEN
                    RAISE EXCEPTION 'conflict resolution cannot close candidate'
                        USING ERRCODE = 'LB003';
                END IF;
                v_event_one := internal_command.append_candidate_transition(
                    p_candidate_id, v_current.revision, 'RESOLVE_CONFLICT', p_actor_ref,
                    p_reason, p_decided_at, v_business_unit_id, v_business_unit_ref,
                    v_business_unit_label, v_category_id, v_category_code,
                    v_category_label, v_amount_minor, v_accounting_month,
                    p_conflict_resolution
                );
                v_event_two := internal_command.append_candidate_transition(
                    p_candidate_id, v_current.revision + 1, 'CONFIRM', p_actor_ref,
                    p_reason, p_decided_at, v_business_unit_id, v_business_unit_ref,
                    v_business_unit_label, v_category_id, v_category_code,
                    v_category_label, v_amount_minor, v_accounting_month, NULL
                );
                v_final_revision := v_current.revision + 2;
                v_events := ARRAY[v_event_one, v_event_two];
            END IF;

            INSERT INTO internal_command.candidate_decision_receipt (
                operation_id, candidate_id, actor_ref, workload_principal_ref,
                verified_san, request_fingerprint, final_revision,
                event_operation_ids, decided_at
            ) VALUES (
                p_operation_id, p_candidate_id, p_actor_ref, p_workload_principal_ref,
                p_verified_san, v_request_fingerprint, v_final_revision, v_events, p_decided_at
            );
            INSERT INTO internal_command.candidate_assertion_use(assertion_jti, operation_id)
            VALUES (p_assertion_jti, p_operation_id) ON CONFLICT DO NOTHING;
            SELECT operation_id INTO v_existing_assertion
              FROM internal_command.candidate_assertion_use
             WHERE assertion_jti = p_assertion_jti;
            IF v_existing_assertion IS DISTINCT FROM p_operation_id THEN
                RAISE EXCEPTION 'assertion JTI was reused for another operation'
                    USING ERRCODE = 'LB001';
            END IF;
            RETURN internal_command.render_candidate_decision_receipt(p_operation_id, false);
        END
        $function$;
    """
