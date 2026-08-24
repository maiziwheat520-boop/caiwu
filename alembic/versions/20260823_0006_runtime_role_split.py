"""Grant separate API enqueue and worker execution database roles.

Runtime roles are externally bootstrapped identities.  This migration never
creates a role or changes a credential.  A role-admin owner repairs role drift;
a restricted owner verifies the bootstrap contract and fails closed on drift.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260823_0006"
down_revision: str | None = "20260823_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Role attributes and direct memberships are cluster-global.  Only a
    # superuser/role-admin may repair them; a restricted owner must verify them.
    op.execute(
        """
        -- Keep public first so later migrations in the same Alembic
        -- transaction create unqualified tables in the application schema.
        -- Catalog objects are explicitly qualified below.
        SET LOCAL search_path = public, pg_catalog;
        DO $ledgerbridge$
        DECLARE
            v_can_manage_roles boolean;
            v_role_contract_ok boolean;
            v_membership RECORD;
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_roles
                WHERE rolname = 'ledgerbridge_api'
            ) OR NOT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_roles
                WHERE rolname = 'ledgerbridge_worker'
            ) THEN
                RAISE EXCEPTION
                    'runtime role split requires externally bootstrapped roles';
            END IF;

            SELECT (rolsuper OR rolcreaterole)
            INTO v_can_manage_roles
            FROM pg_catalog.pg_roles
            WHERE rolname = current_user;

            IF NOT COALESCE(v_can_manage_roles, false) THEN
                SELECT bool_and(
                    rolcanlogin
                    AND NOT rolsuper
                    AND NOT rolcreatedb
                    AND NOT rolcreaterole
                    AND NOT rolinherit
                    AND NOT rolreplication
                    AND NOT rolbypassrls
                )
                INTO v_role_contract_ok
                FROM pg_catalog.pg_roles
                WHERE rolname IN ('ledgerbridge_api', 'ledgerbridge_worker');

                IF NOT COALESCE(v_role_contract_ok, false) THEN
                    RAISE EXCEPTION
                        'runtime role bootstrap attributes are not approved';
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_auth_members AS membership
                    JOIN pg_catalog.pg_roles AS member_role
                        ON member_role.oid = membership.member
                    WHERE member_role.rolname IN ('ledgerbridge_api', 'ledgerbridge_worker')
                ) THEN
                    RAISE EXCEPTION
                        'runtime roles retain forbidden direct membership, '
                        'including owner membership';
                END IF;
            ELSE
                -- %I quotes catalog values; no runtime value becomes SQL syntax.
                FOR v_membership IN
                    SELECT member_role.rolname AS member_name,
                           granted_role.rolname AS granted_name
                    FROM pg_catalog.pg_auth_members AS membership
                    JOIN pg_catalog.pg_roles AS member_role
                        ON member_role.oid = membership.member
                    JOIN pg_catalog.pg_roles AS granted_role
                        ON granted_role.oid = membership.roleid
                    WHERE member_role.rolname IN ('ledgerbridge_api', 'ledgerbridge_worker')
                LOOP
                    EXECUTE pg_catalog.format(
                        'REVOKE %I FROM %I',
                        v_membership.granted_name,
                        v_membership.member_name
                    );
                END LOOP;

                -- Credentials remain in the external bootstrap/deployment path.
                ALTER ROLE ledgerbridge_api
                    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
                    NOREPLICATION NOBYPASSRLS;
                ALTER ROLE ledgerbridge_worker
                    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
                    NOREPLICATION NOBYPASSRLS;
            END IF;

            -- Verify both branches before touching object ACLs.
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.pg_roles
                WHERE rolname IN ('ledgerbridge_api', 'ledgerbridge_worker')
                  AND NOT (
                      rolcanlogin
                      AND NOT rolsuper
                      AND NOT rolcreatedb
                      AND NOT rolcreaterole
                      AND NOT rolinherit
                      AND NOT rolreplication
                      AND NOT rolbypassrls
                  )
            ) THEN
                RAISE EXCEPTION
                    'runtime role attributes do not match the approved bootstrap contract';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_catalog.pg_auth_members AS membership
                JOIN pg_catalog.pg_roles AS member_role
                    ON member_role.oid = membership.member
                WHERE member_role.rolname IN ('ledgerbridge_api', 'ledgerbridge_worker')
            ) THEN
                RAISE EXCEPTION
                    'runtime roles retain forbidden direct membership, '
                    'including owner membership';
            END IF;
        END
        $ledgerbridge$;
        """
    )

    # ACLs belong to the migration owner's schema/table/type/function objects.
    # Normalize them directly; insufficient privilege is intentionally an error
    # rather than a silent fallback to an unverified or pre-provisioned ACL.
    op.execute(
        """
        SET LOCAL search_path = public, pg_catalog;
        DO $ledgerbridge$
        DECLARE
            v_column RECORD;
        BEGIN
            FOR v_column IN
                SELECT relation.relname AS table_name,
                       column_info.attname AS column_name
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_attribute AS column_info
                    ON column_info.attrelid = relation.oid
                   AND column_info.attnum > 0
                   AND NOT column_info.attisdropped
                WHERE relation.relnamespace = 'public'::pg_catalog.regnamespace
                  AND relation.relname IN ('import_job', 'evidence_import_dispatch')
            LOOP
                EXECUTE pg_catalog.format(
                    'REVOKE ALL (%I) ON TABLE public.%I '
                    'FROM ledgerbridge_api, ledgerbridge_worker',
                    v_column.column_name,
                    v_column.table_name
                );
            END LOOP;

            REVOKE ALL ON TABLE public.raw_artifact, public.source_record,
                public.import_job, public.ingest_channel, public.source_system,
                public.audit_event, public.evidence_import_dispatch
                FROM ledgerbridge_api, ledgerbridge_worker;
            REVOKE ALL ON FUNCTION public.append_audit_event(text, text, text, text, jsonb)
                FROM ledgerbridge_api, ledgerbridge_worker;
            REVOKE ALL ON TYPE public.import_job_status, public.dispatch_state
                FROM ledgerbridge_api, ledgerbridge_worker;
            REVOKE ALL ON SCHEMA public FROM ledgerbridge_api, ledgerbridge_worker;

            GRANT USAGE ON SCHEMA public TO ledgerbridge_api, ledgerbridge_worker;

            GRANT SELECT, INSERT ON TABLE public.raw_artifact TO ledgerbridge_api;
            GRANT SELECT ON TABLE public.ingest_channel, public.source_system,
                public.audit_event, public.import_job, public.evidence_import_dispatch
                TO ledgerbridge_api;
            GRANT INSERT ON TABLE public.evidence_import_dispatch TO ledgerbridge_api;
            GRANT USAGE ON TYPE public.dispatch_state TO ledgerbridge_api;
            GRANT EXECUTE ON FUNCTION public.append_audit_event(text, text, text, text, jsonb)
                TO ledgerbridge_api;

            GRANT SELECT, INSERT ON TABLE public.raw_artifact, public.source_record,
                public.import_job TO ledgerbridge_worker;
            GRANT SELECT ON TABLE public.ingest_channel, public.source_system,
                public.audit_event, public.evidence_import_dispatch TO ledgerbridge_worker;
            GRANT UPDATE (
                status,
                started_at,
                completed_at,
                terminal_audit_event_id,
                parsed_count,
                created_count,
                duplicate_count,
                error_code,
                diagnostic_summary
            ) ON TABLE public.import_job TO ledgerbridge_worker;
            GRANT UPDATE (
                state,
                attempt_count,
                available_at,
                lease_owner,
                lease_until,
                started_at,
                completed_at,
                import_job_id,
                error_code,
                diagnostic_summary
            ) ON TABLE public.evidence_import_dispatch TO ledgerbridge_worker;
            GRANT USAGE ON TYPE public.import_job_status, public.dispatch_state
                TO ledgerbridge_worker;
            GRANT EXECUTE ON FUNCTION public.append_audit_event(text, text, text, text, jsonb)
                TO ledgerbridge_worker;
        END
        $ledgerbridge$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        SET LOCAL search_path = public, pg_catalog;
        DO $ledgerbridge$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'ledgerbridge_api'
            ) THEN
                REVOKE ALL ON TABLE public.raw_artifact, public.source_record,
                    public.import_job, public.ingest_channel, public.source_system,
                    public.audit_event, public.evidence_import_dispatch
                    FROM ledgerbridge_api;
                REVOKE ALL ON FUNCTION public.append_audit_event(text, text, text, text, jsonb)
                    FROM ledgerbridge_api;
                REVOKE ALL ON TYPE public.import_job_status, public.dispatch_state
                    FROM ledgerbridge_api;
                REVOKE USAGE ON SCHEMA public FROM ledgerbridge_api;
            END IF;
            IF EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'ledgerbridge_worker'
            ) THEN
                REVOKE ALL ON TABLE public.raw_artifact, public.source_record,
                    public.import_job, public.ingest_channel, public.source_system,
                    public.audit_event, public.evidence_import_dispatch
                    FROM ledgerbridge_worker;
                REVOKE ALL ON FUNCTION public.append_audit_event(text, text, text, text, jsonb)
                    FROM ledgerbridge_worker;
                REVOKE ALL ON TYPE public.import_job_status, public.dispatch_state
                    FROM ledgerbridge_worker;
                REVOKE USAGE ON SCHEMA public FROM ledgerbridge_worker;
            END IF;
        END
        $ledgerbridge$;
        """
    )
