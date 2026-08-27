"""Grant separate API enqueue and worker execution database roles.

The runtime roles are bootstrap-owned identities.  This migration therefore
never creates a role or changes a credential.  A role-admin migration owner
can repair drift; a restricted owner can only proceed after the bootstrap
contract and the complete runtime ACL boundary have been verified.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260823_0006"
down_revision: str | None = "20260823_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Role attributes and memberships are cluster-global.  The migration only
    # repairs them when the current owner is a superuser/role administrator;
    # otherwise the bootstrap contract is verified and any drift fails closed.
    op.execute(
        """
        SET LOCAL search_path = pg_catalog, public;
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
                -- A restricted owner cannot safely repair role attributes or
                -- memberships.  Check every approved bootstrap attribute,
                -- including LOGIN, before allowing the migration to proceed.
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

                -- No runtime role may be able to activate an inherited owner,
                -- compatibility, or other privileged role through SET ROLE.
                IF EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_auth_members AS membership
                    JOIN pg_catalog.pg_roles AS member_role
                        ON member_role.oid = membership.member
                    WHERE member_role.rolname IN ('ledgerbridge_api', 'ledgerbridge_worker')
                ) THEN
                    RAISE EXCEPTION
                        'runtime roles retain forbidden role membership, '
                        'including owner membership';
                END IF;
            ELSE
                -- Role administrators are allowed to repair only these fixed
                -- deployment-contract identities.  %I quotes catalog values
                -- and never interpolates runtime input into SQL syntax.
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

                -- Credentials remain solely in the external bootstrap/deployment
                -- secret path; this migration never changes them.
                ALTER ROLE ledgerbridge_api
                    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
                    NOREPLICATION NOBYPASSRLS;
                ALTER ROLE ledgerbridge_worker
                    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
                    NOREPLICATION NOBYPASSRLS;
            END IF;

            -- This check is intentionally repeated after the repair path so a
            -- role-admin that could not revoke a membership cannot continue.
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
                    'runtime roles retain forbidden role membership, including owner membership';
            END IF;
        END
        $ledgerbridge$;
        """
    )

    # Object owners may repair the ACLs even when they cannot administer
    # roles.  If the migration owner is not an owner/grantor, PostgreSQL raises
    # insufficient_privilege; the subtransaction rolls the attempted changes
    # back and the verifier below decides whether a pre-provisioned ACL is safe.
    op.execute(
        """
        SET LOCAL search_path = pg_catalog, public;
        DO $ledgerbridge$
        DECLARE
            v_column RECORD;
        BEGIN
            BEGIN
                -- Table-level REVOKE does not make an historical column ACL
                -- assumption safe.  Clear every column grant explicitly with
                -- identifiers sourced from the fixed public tables.
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
            EXCEPTION
                WHEN insufficient_privilege THEN
                    -- The subtransaction has rolled back every ACL change.
                    -- The exact verifier below is mandatory; this is not a
                    -- best-effort or silently skipped grant path.
                    NULL;
            END;
        END
        $ledgerbridge$;
        """
    )

    # Verify the ACL boundary after both the repair and restricted-owner paths.
    # The checks include PUBLIC for objects whose earlier migrations revoked it,
    # and reject grant-option ACLs that would let a runtime role delegate power.
    op.execute(
        """
        SET LOCAL search_path = pg_catalog, public;
        DO $ledgerbridge$
        DECLARE
            v_contract_ok boolean;
        BEGIN
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
                    'runtime roles retain forbidden role membership, including owner membership';
            END IF;

            WITH expected(object_name, grantee, privilege_type) AS (
                VALUES
                    ('raw_artifact', 'ledgerbridge_api', 'SELECT'),
                    ('raw_artifact', 'ledgerbridge_api', 'INSERT'),
                    ('raw_artifact', 'ledgerbridge_worker', 'SELECT'),
                    ('raw_artifact', 'ledgerbridge_worker', 'INSERT'),
                    ('source_record', 'ledgerbridge_worker', 'SELECT'),
                    ('source_record', 'ledgerbridge_worker', 'INSERT'),
                    ('import_job', 'ledgerbridge_api', 'SELECT'),
                    ('import_job', 'ledgerbridge_worker', 'SELECT'),
                    ('import_job', 'ledgerbridge_worker', 'INSERT'),
                    ('ingest_channel', 'ledgerbridge_api', 'SELECT'),
                    ('ingest_channel', 'ledgerbridge_worker', 'SELECT'),
                    ('source_system', 'ledgerbridge_api', 'SELECT'),
                    ('source_system', 'ledgerbridge_worker', 'SELECT'),
                    ('audit_event', 'ledgerbridge_api', 'SELECT'),
                    ('audit_event', 'ledgerbridge_worker', 'SELECT'),
                    ('evidence_import_dispatch', 'ledgerbridge_api', 'SELECT'),
                    ('evidence_import_dispatch', 'ledgerbridge_api', 'INSERT'),
                    ('evidence_import_dispatch', 'ledgerbridge_worker', 'SELECT')
            ), actual(object_name, grantee, privilege_type, is_grantable) AS (
                SELECT
                    relation.relname::text,
                    CASE
                        WHEN privilege.grantee = 0::pg_catalog.oid THEN '<PUBLIC>'
                        ELSE granted_role.rolname
                    END,
                    privilege.privilege_type::text,
                    privilege.is_grantable
                FROM pg_catalog.pg_class AS relation
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    coalesce(
                        relation.relacl,
                        pg_catalog.acldefault('r', relation.relowner)
                    )
                ) AS privilege
                LEFT JOIN pg_catalog.pg_roles AS granted_role
                    ON granted_role.oid = privilege.grantee
                WHERE relation.relnamespace = 'public'::pg_catalog.regnamespace
                  AND relation.relname IN (
                      'raw_artifact',
                      'source_record',
                      'import_job',
                      'ingest_channel',
                      'source_system',
                      'audit_event',
                      'evidence_import_dispatch'
                  )
                  AND (
                      privilege.grantee = 0::pg_catalog.oid
                      OR granted_role.rolname IN ('ledgerbridge_api', 'ledgerbridge_worker')
                  )
            ), problems AS (
                SELECT actual.object_name, actual.grantee, actual.privilege_type
                FROM actual
                WHERE actual.is_grantable
                   OR NOT EXISTS (
                       SELECT 1
                       FROM expected
                       WHERE expected.object_name = actual.object_name
                         AND expected.grantee = actual.grantee
                         AND expected.privilege_type = actual.privilege_type
                   )
                UNION ALL
                SELECT expected.object_name, expected.grantee, expected.privilege_type
                FROM expected
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM actual
                    WHERE actual.object_name = expected.object_name
                      AND actual.grantee = expected.grantee
                      AND actual.privilege_type = expected.privilege_type
                      AND NOT actual.is_grantable
                )
            )
            SELECT NOT EXISTS (SELECT 1 FROM problems)
            INTO v_contract_ok;

            IF NOT v_contract_ok THEN
                RAISE EXCEPTION
                    'runtime table ACLs are not the approved API/worker boundary';
            END IF;

            WITH expected(type_name, grantee, privilege_type) AS (
                VALUES
                    ('import_job_status', 'ledgerbridge_worker', 'USAGE'),
                    ('dispatch_state', 'ledgerbridge_api', 'USAGE'),
                    ('dispatch_state', 'ledgerbridge_worker', 'USAGE')
            ), actual(type_name, grantee, privilege_type, is_grantable) AS (
                SELECT
                    object_type.typname::text,
                    CASE
                        WHEN privilege.grantee = 0::pg_catalog.oid THEN '<PUBLIC>'
                        ELSE granted_role.rolname
                    END,
                    privilege.privilege_type::text,
                    privilege.is_grantable
                FROM pg_catalog.pg_type AS object_type
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    coalesce(
                        object_type.typacl,
                        pg_catalog.acldefault('T', object_type.typowner)
                    )
                ) AS privilege
                LEFT JOIN pg_catalog.pg_roles AS granted_role
                    ON granted_role.oid = privilege.grantee
                WHERE object_type.typnamespace = 'public'::pg_catalog.regnamespace
                  AND object_type.typname IN ('import_job_status', 'dispatch_state')
                  AND (
                      privilege.grantee = 0::pg_catalog.oid
                      OR granted_role.rolname IN ('ledgerbridge_api', 'ledgerbridge_worker')
                  )
            ), problems AS (
                SELECT actual.type_name, actual.grantee, actual.privilege_type
                FROM actual
                WHERE actual.is_grantable
                   OR NOT EXISTS (
                       SELECT 1
                       FROM expected
                       WHERE expected.type_name = actual.type_name
                         AND expected.grantee = actual.grantee
                         AND expected.privilege_type = actual.privilege_type
                   )
                UNION ALL
                SELECT expected.type_name, expected.grantee, expected.privilege_type
                FROM expected
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM actual
                    WHERE actual.type_name = expected.type_name
                      AND actual.grantee = expected.grantee
                      AND actual.privilege_type = expected.privilege_type
                      AND NOT actual.is_grantable
                )
            )
            SELECT NOT EXISTS (SELECT 1 FROM problems)
            INTO v_contract_ok;

            IF NOT v_contract_ok THEN
                RAISE EXCEPTION
                    'runtime type ACLs are not the approved API/worker boundary';
            END IF;

            WITH expected(grantee, privilege_type) AS (
                VALUES
                    ('ledgerbridge_api', 'EXECUTE'),
                    ('ledgerbridge_worker', 'EXECUTE')
            ), actual(grantee, privilege_type, is_grantable) AS (
                SELECT
                    CASE
                        WHEN privilege.grantee = 0::pg_catalog.oid THEN '<PUBLIC>'
                        ELSE granted_role.rolname
                    END,
                    privilege.privilege_type::text,
                    privilege.is_grantable
                FROM pg_catalog.pg_proc AS routine
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    coalesce(
                        routine.proacl,
                        pg_catalog.acldefault('f', routine.proowner)
                    )
                ) AS privilege
                LEFT JOIN pg_catalog.pg_roles AS granted_role
                    ON granted_role.oid = privilege.grantee
                WHERE routine.oid = pg_catalog.to_regprocedure(
                    'public.append_audit_event(text, text, text, text, jsonb)'
                )
                  AND (
                      privilege.grantee = 0::pg_catalog.oid
                      OR granted_role.rolname IN ('ledgerbridge_api', 'ledgerbridge_worker')
                  )
            ), problems AS (
                SELECT actual.grantee, actual.privilege_type
                FROM actual
                WHERE actual.is_grantable
                   OR NOT EXISTS (
                       SELECT 1
                       FROM expected
                       WHERE expected.grantee = actual.grantee
                         AND expected.privilege_type = actual.privilege_type
                   )
                UNION ALL
                SELECT expected.grantee, expected.privilege_type
                FROM expected
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM actual
                    WHERE actual.grantee = expected.grantee
                      AND actual.privilege_type = expected.privilege_type
                      AND NOT actual.is_grantable
                )
            )
            SELECT NOT EXISTS (SELECT 1 FROM problems)
            INTO v_contract_ok;

            IF NOT v_contract_ok THEN
                RAISE EXCEPTION
                    'runtime audit function ACLs are not the approved API/worker boundary';
            END IF;

            WITH expected(grantee, privilege_type) AS (
                VALUES
                    ('ledgerbridge_api', 'USAGE'),
                    ('ledgerbridge_worker', 'USAGE')
            ), actual(grantee, privilege_type, is_grantable) AS (
                SELECT
                    granted_role.rolname,
                    privilege.privilege_type::text,
                    privilege.is_grantable
                FROM pg_catalog.pg_namespace AS namespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    coalesce(
                        namespace.nspacl,
                        pg_catalog.acldefault('n', namespace.nspowner)
                    )
                ) AS privilege
                JOIN pg_catalog.pg_roles AS granted_role
                    ON granted_role.oid = privilege.grantee
                WHERE namespace.nspname = 'public'
                  AND granted_role.rolname IN ('ledgerbridge_api', 'ledgerbridge_worker')
            ), problems AS (
                SELECT actual.grantee, actual.privilege_type
                FROM actual
                WHERE actual.is_grantable
                   OR NOT EXISTS (
                       SELECT 1
                       FROM expected
                       WHERE expected.grantee = actual.grantee
                         AND expected.privilege_type = actual.privilege_type
                   )
                UNION ALL
                SELECT expected.grantee, expected.privilege_type
                FROM expected
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM actual
                    WHERE actual.grantee = expected.grantee
                      AND actual.privilege_type = expected.privilege_type
                      AND NOT actual.is_grantable
                )
            )
            SELECT NOT EXISTS (SELECT 1 FROM problems)
            INTO v_contract_ok;

            IF NOT v_contract_ok THEN
                RAISE EXCEPTION
                    'runtime schema ACLs are not the approved API/worker boundary';
            END IF;

            WITH expected(table_name, column_name, grantee, privilege_type) AS (
                VALUES
                    ('import_job', 'status', 'ledgerbridge_worker', 'UPDATE'),
                    ('import_job', 'started_at', 'ledgerbridge_worker', 'UPDATE'),
                    ('import_job', 'completed_at', 'ledgerbridge_worker', 'UPDATE'),
                    ('import_job', 'terminal_audit_event_id', 'ledgerbridge_worker', 'UPDATE'),
                    ('import_job', 'parsed_count', 'ledgerbridge_worker', 'UPDATE'),
                    ('import_job', 'created_count', 'ledgerbridge_worker', 'UPDATE'),
                    ('import_job', 'duplicate_count', 'ledgerbridge_worker', 'UPDATE'),
                    ('import_job', 'error_code', 'ledgerbridge_worker', 'UPDATE'),
                    ('import_job', 'diagnostic_summary', 'ledgerbridge_worker', 'UPDATE'),
                    ('evidence_import_dispatch', 'state', 'ledgerbridge_worker', 'UPDATE'),
                    ('evidence_import_dispatch', 'attempt_count', 'ledgerbridge_worker', 'UPDATE'),
                    ('evidence_import_dispatch', 'available_at', 'ledgerbridge_worker', 'UPDATE'),
                    ('evidence_import_dispatch', 'lease_owner', 'ledgerbridge_worker', 'UPDATE'),
                    ('evidence_import_dispatch', 'lease_until', 'ledgerbridge_worker', 'UPDATE'),
                    ('evidence_import_dispatch', 'started_at', 'ledgerbridge_worker', 'UPDATE'),
                    ('evidence_import_dispatch', 'completed_at', 'ledgerbridge_worker', 'UPDATE'),
                    ('evidence_import_dispatch', 'import_job_id', 'ledgerbridge_worker', 'UPDATE'),
                    ('evidence_import_dispatch', 'error_code', 'ledgerbridge_worker', 'UPDATE'),
                    (
                        'evidence_import_dispatch',
                        'diagnostic_summary',
                        'ledgerbridge_worker',
                        'UPDATE'
                    )
            ), actual(table_name, column_name, grantee, privilege_type, is_grantable) AS (
                SELECT
                    relation.relname::text,
                    column_info.attname::text,
                    CASE
                        WHEN privilege.grantee = 0::pg_catalog.oid THEN '<PUBLIC>'
                        ELSE granted_role.rolname
                    END,
                    privilege.privilege_type::text,
                    privilege.is_grantable
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_attribute AS column_info
                    ON column_info.attrelid = relation.oid
                   AND NOT column_info.attisdropped
                   AND column_info.attnum > 0
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    coalesce(
                        column_info.attacl,
                        '{}'::pg_catalog.aclitem[]
                    )
                ) AS privilege
                LEFT JOIN pg_catalog.pg_roles AS granted_role
                    ON granted_role.oid = privilege.grantee
                WHERE relation.relnamespace = 'public'::pg_catalog.regnamespace
                  AND relation.relname IN ('import_job', 'evidence_import_dispatch')
                  AND (
                      privilege.grantee = 0::pg_catalog.oid
                      OR granted_role.rolname IN ('ledgerbridge_api', 'ledgerbridge_worker')
                  )
            ), problems AS (
                SELECT actual.table_name, actual.column_name, actual.grantee, actual.privilege_type
                FROM actual
                WHERE actual.is_grantable
                   OR NOT EXISTS (
                       SELECT 1
                       FROM expected
                       WHERE expected.table_name = actual.table_name
                         AND expected.column_name = actual.column_name
                         AND expected.grantee = actual.grantee
                         AND expected.privilege_type = actual.privilege_type
                   )
                UNION ALL
                SELECT expected.table_name, expected.column_name,
                       expected.grantee, expected.privilege_type
                FROM expected
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM actual
                    WHERE actual.table_name = expected.table_name
                      AND actual.column_name = expected.column_name
                      AND actual.grantee = expected.grantee
                      AND actual.privilege_type = expected.privilege_type
                      AND NOT actual.is_grantable
                )
            )
            SELECT NOT EXISTS (SELECT 1 FROM problems)
            INTO v_contract_ok;

            IF NOT v_contract_ok THEN
                RAISE EXCEPTION
                    'runtime column ACLs are not the approved API/worker boundary';
            END IF;
        END
        $ledgerbridge$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        SET LOCAL search_path = pg_catalog, public;
        DO $ledgerbridge$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'ledgerbridge_api') THEN
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
            IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'ledgerbridge_worker') THEN
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
