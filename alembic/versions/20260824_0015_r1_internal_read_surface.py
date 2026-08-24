# ruff: noqa: E501

"""Install the closed R1 internal-read surface and exact runtime ACLs.

The reader login is an external bootstrap concern.  This migration only
validates that bootstrap, creates owner-controlled views/functions, and grants
the reader the narrow SELECT/EXECUTE surface.  It never creates a role or
stores a credential.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260824_0015"
down_revision: str | None = "20260824_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def _runtime_role_preflight() -> None:
    op.execute(
        """
        DO $roles$
        DECLARE
            v_owner oid;
            v_database_owner oid;
            v_owner_can_login boolean;
            role_name text;
            role_oid oid;
            controlled_roles text[] := ARRAY[
                'ledgerbridge_reader', 'ledgerbridge_api',
                'ledgerbridge_worker', 'ledgerbridge_app',
                'ledgerbridge_backup'
            ];
        BEGIN
            SELECT oid, rolcanlogin
              INTO v_owner, v_owner_can_login
              FROM pg_roles WHERE rolname = current_user;
            SELECT datdba INTO v_database_owner
              FROM pg_database WHERE datname = current_database();
            IF v_owner IS NULL THEN
                RAISE EXCEPTION 'current migration owner role is missing'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;
            IF v_owner IS NULL OR v_database_owner IS DISTINCT FROM v_owner THEN
                RAISE EXCEPTION 'migration must run as the fixed current database owner'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_api')
               OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_worker')
               OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_reader') THEN
                RAISE EXCEPTION 'required runtime reader/API/worker roles are missing'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;

            -- The migration owner is fixed by the database owner and is the
            -- only role allowed to run DDL here.  Its DDL-capable attributes
            -- are intentional; the security boundary is that it cannot be a
            -- controlled runtime role or a member of one.
            IF v_owner_can_login IS DISTINCT FROM (session_user = current_user)
               OR current_user = ANY(controlled_roles) THEN
                RAISE EXCEPTION
                    'fixed migration owner has invalid attributes or collides with a runtime role'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;

            -- Reader, API, and worker are direct, unprivileged LOGIN roles.
            -- The compatibility app role is optional and may be NOLOGIN after
            -- its production retirement, but it must remain NOINHERIT and
            -- unprivileged whenever it exists.  The optional backup role is
            -- held to the same unprivileged/NOINHERIT/no-membership/object-
            -- ownership boundary; it may be LOGIN or NOLOGIN because the
            -- database ACL does not grant it any fact or reader privilege.
            FOREACH role_name IN ARRAY controlled_roles LOOP
                SELECT oid INTO role_oid FROM pg_roles WHERE rolname = role_name;
                IF role_oid IS NULL THEN
                    IF role_name IN ('ledgerbridge_app', 'ledgerbridge_backup') THEN
                        CONTINUE;
                    END IF;
                    RAISE EXCEPTION 'required runtime role % is missing', role_name
                        USING ERRCODE = 'invalid_authorization_specification';
                END IF;
                IF EXISTS (
                    SELECT 1 FROM pg_roles AS runtime_role
                     WHERE runtime_role.oid = role_oid
                       AND (runtime_role.rolsuper
                            OR runtime_role.rolcreaterole
                            OR runtime_role.rolcreatedb
                            OR runtime_role.rolreplication
                            OR runtime_role.rolbypassrls
                            OR runtime_role.rolinherit IS DISTINCT FROM false)
                ) OR (
                    role_name = 'ledgerbridge_reader'
                    AND (SELECT rolcanlogin FROM pg_roles WHERE oid = role_oid)
                        IS DISTINCT FROM true
                ) THEN
                    IF role_name = 'ledgerbridge_reader' THEN
                        RAISE EXCEPTION
                            'ledgerbridge_reader must be an unprivileged NOINHERIT LOGIN role'
                            USING ERRCODE = 'invalid_authorization_specification';
                    END IF;
                    RAISE EXCEPTION 'runtime role has unexpected privilege or inheritance: %',
                        role_name USING ERRCODE = 'invalid_authorization_specification';
                END IF;
                IF role_name NOT IN ('ledgerbridge_app', 'ledgerbridge_backup')
                   AND (SELECT rolcanlogin FROM pg_roles WHERE oid = role_oid)
                       IS DISTINCT FROM true THEN
                    RAISE EXCEPTION 'runtime role % must be a LOGIN role', role_name
                        USING ERRCODE = 'invalid_authorization_specification';
                END IF;
            END LOOP;

            -- Membership is checked in both directions.  In particular, a
            -- stale GRANT owner TO api is not made harmless by NOINHERIT:
            -- the runtime could still activate it with SET ROLE.  Any direct
            -- membership involving a controlled role or the owner is drift.
            IF EXISTS (
                SELECT 1
                  FROM pg_auth_members AS membership
                  LEFT JOIN pg_roles AS member_role ON member_role.oid = membership.member
                  LEFT JOIN pg_roles AS granted_role ON granted_role.oid = membership.roleid
                 WHERE member_role.oid IS NULL
                    OR granted_role.oid IS NULL
                    OR member_role.rolname = 'ledgerbridge_reader'
                    OR granted_role.rolname = 'ledgerbridge_reader'
            ) THEN
                RAISE EXCEPTION
                    'ledgerbridge_reader has unexpected bidirectional role membership'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM pg_auth_members AS membership
                  LEFT JOIN pg_roles AS member_role ON member_role.oid = membership.member
                  LEFT JOIN pg_roles AS granted_role ON granted_role.oid = membership.roleid
                 WHERE member_role.oid = v_owner
                    OR granted_role.oid = v_owner
                    OR member_role.rolname = ANY(
                        ARRAY['ledgerbridge_api', 'ledgerbridge_worker', 'ledgerbridge_app',
                              'ledgerbridge_backup']
                    )
                    OR granted_role.rolname = ANY(
                        ARRAY['ledgerbridge_api', 'ledgerbridge_worker', 'ledgerbridge_app',
                              'ledgerbridge_backup']
                    )
            ) THEN
                RAISE EXCEPTION 'runtime roles must not have role membership'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;

            -- None of the runtime roles may own any database object.  This is
            -- deliberately broader than the new schema: a stale owner would
            -- otherwise make the runtime an implicit SECURITY DEFINER trust
            -- root after a restore.
            IF EXISTS (
                SELECT 1
                  FROM pg_roles AS r
                 WHERE r.rolname = ANY(controlled_roles)
                   AND (
                       EXISTS (SELECT 1 FROM pg_database AS d WHERE d.datdba = r.oid)
                       OR EXISTS (SELECT 1 FROM pg_namespace AS n WHERE n.nspowner = r.oid)
                       OR EXISTS (SELECT 1 FROM pg_class AS c WHERE c.relowner = r.oid)
                       OR EXISTS (SELECT 1 FROM pg_proc AS p WHERE p.proowner = r.oid)
                   )
            ) THEN
                RAISE EXCEPTION 'runtime roles must not own database objects'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;
            IF pg_get_userbyid(v_owner) IS DISTINCT FROM current_user
               OR pg_get_userbyid(v_database_owner) IS DISTINCT FROM current_user THEN
                RAISE EXCEPTION 'fixed migration owner identity is invalid'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;
        END
        $roles$;
        """
    )


def _database_acl() -> None:
    op.execute(
        """
        DO $acl$
        DECLARE
            v_database text := current_database();
            v_owner text := current_user;
            v_allowlist text[] := ARRAY[
                current_user, 'pg_database_owner',
                'ledgerbridge_reader', 'ledgerbridge_api',
                'ledgerbridge_worker', 'ledgerbridge_app',
                'ledgerbridge_backup'
            ];
            v_runtime_roles text[] := ARRAY[
                'ledgerbridge_reader', 'ledgerbridge_api',
                'ledgerbridge_worker', 'ledgerbridge_app',
                'ledgerbridge_backup'
            ];
            role_name text;
            grantee_name text;
            grantee_oid oid;
        BEGIN
            -- Rebuild the database ACL from an explicit allowlist.  A stale
            -- role is never silently ignored; an ACL whose role no longer
            -- exists is an unrecoverable restore/drift condition.
            EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', v_database);

            FOR grantee_oid, grantee_name IN
                SELECT acl.grantee, grantee_role.rolname
                  FROM pg_database AS database_row
                  CROSS JOIN LATERAL aclexplode(
                      COALESCE(database_row.datacl, '{}'::aclitem[])
                  ) AS acl
                  LEFT JOIN pg_roles AS grantee_role
                    ON grantee_role.oid = acl.grantee
                 WHERE database_row.datname = v_database
                   AND acl.grantee <> 0
            LOOP
                IF grantee_name IS NULL OR NOT (grantee_name = ANY(v_allowlist)) THEN
                    RAISE EXCEPTION 'database CONNECT allowlist contains a stale principal: %',
                        COALESCE(grantee_name, grantee_oid::text)
                        USING ERRCODE = 'invalid_authorization_specification';
                END IF;
            END LOOP;

            -- Runtime and backup roles retain CONNECT only.  The owner and
            -- pg_database_owner are intentionally not revoked: PostgreSQL
            -- ownership is the required migration/backup rule.
            FOREACH role_name IN ARRAY ARRAY[
                'ledgerbridge_reader', 'ledgerbridge_api',
                'ledgerbridge_worker', 'ledgerbridge_app',
                'ledgerbridge_backup'
            ] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format('REVOKE ALL ON DATABASE %I FROM %I', v_database, role_name);
                    EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', v_database, role_name);
                    EXECUTE format(
                        'REVOKE TEMPORARY, CREATE ON DATABASE %I FROM %I',
                        v_database, role_name
                    );
                END IF;
            END LOOP;
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', v_database, v_owner);

            IF EXISTS (
                SELECT 1
                  FROM pg_namespace AS namespace
                  CROSS JOIN LATERAL aclexplode(
                      COALESCE(namespace.nspacl, '{}'::aclitem[])
                  ) AS acl
                 WHERE namespace.nspname = 'public'
                   AND acl.grantee = 0
                   AND acl.privilege_type = 'CREATE'
            ) THEN
                RAISE EXCEPTION 'PUBLIC must not retain CREATE on schema public'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM pg_namespace AS namespace
                  CROSS JOIN LATERAL aclexplode(
                      COALESCE(namespace.nspacl, '{}'::aclitem[])
                  ) AS acl
                 WHERE namespace.nspname = 'public'
                   AND acl.privilege_type = 'CREATE'
                   AND acl.grantee IN (
                       SELECT oid FROM pg_roles WHERE rolname = ANY(v_runtime_roles)
                   )
            ) THEN
                RAISE EXCEPTION 'runtime role must not retain CREATE on schema public'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;

            -- CREATE is removed before this migration creates any new
            -- SECURITY DEFINER function in public.  API/worker (and the
            -- optional compatibility app) still need schema name resolution;
            -- reader deliberately receives no public-schema USAGE.
            EXECUTE 'REVOKE CREATE ON SCHEMA public FROM PUBLIC';
            FOREACH role_name IN ARRAY (v_runtime_roles || ARRAY['ledgerbridge_backup']) LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format('REVOKE CREATE ON SCHEMA public FROM %I', role_name);
                END IF;
            END LOOP;
            EXECUTE 'GRANT USAGE ON SCHEMA public TO ledgerbridge_api, ledgerbridge_worker';
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_app') THEN
                EXECUTE 'GRANT USAGE ON SCHEMA public TO ledgerbridge_app';
            END IF;
            EXECUTE 'REVOKE USAGE ON SCHEMA public FROM ledgerbridge_reader';
        END
        $acl$;
        """
    )
    _revoke_owner_default_acls("public")


def _revoke_owner_default_acls(schema_name: str) -> None:
    """Remove PUBLIC/runtime grants from the current owner's default ACLs."""

    if schema_name not in {"public", "internal_read"}:
        raise ValueError("default ACL schema is not allowlisted")
    sql = (
        ""  # nosec B608 - the schema literal is allowlisted before it is inserted.
        """
        DO $default_acl$
        DECLARE
            v_schema text := __SCHEMA_LITERAL__;
            role_name text;
            owner_oid oid;
            schema_oid oid;
        BEGIN
            SELECT oid INTO owner_oid FROM pg_roles WHERE rolname = current_user;
            SELECT oid INTO schema_oid FROM pg_namespace WHERE nspname = v_schema;
            IF owner_oid IS NULL OR schema_oid IS NULL THEN
                RAISE EXCEPTION 'default ACL owner or schema is missing'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM pg_default_acl AS defaults
                  CROSS JOIN LATERAL aclexplode(
                      COALESCE(defaults.defaclacl, '{}'::aclitem[])
                  ) AS acl
                 WHERE defaults.defaclrole = owner_oid
                   AND defaults.defaclnamespace IN (0, schema_oid)
                   AND (
                       acl.grantee = 0
                       OR acl.grantee IN (
                           SELECT oid FROM pg_roles
                            WHERE rolname IN ('ledgerbridge_reader',
                                              'ledgerbridge_api',
                                              'ledgerbridge_worker',
                                              'ledgerbridge_app', 'ledgerbridge_backup')
                       )
                   )
            ) THEN
                RAISE EXCEPTION 'default privileges grant PUBLIC or a runtime role'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;

            -- Global owner defaults are inherited by every schema.  Revoke
            -- the same PUBLIC/runtime entries there as well; a schema-local
            -- revoke alone would leave a future object exposed through the
            -- global default ACL.
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON TABLES FROM PUBLIC',
                current_user
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON SEQUENCES FROM PUBLIC',
                current_user
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON FUNCTIONS FROM PUBLIC',
                current_user
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
                'REVOKE ALL ON TABLES FROM PUBLIC', current_user, v_schema
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
                'REVOKE ALL ON SEQUENCES FROM PUBLIC', current_user, v_schema
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
                'REVOKE ALL ON FUNCTIONS FROM PUBLIC', current_user, v_schema
            );
            FOREACH role_name IN ARRAY ARRAY['PUBLIC', 'ledgerbridge_reader',
                                             'ledgerbridge_api', 'ledgerbridge_worker',
                                             'ledgerbridge_app', 'ledgerbridge_backup'] LOOP
                IF role_name = 'PUBLIC' THEN
                    CONTINUE;
                ELSIF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format(
                        'ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON TABLES FROM %I',
                        current_user, role_name
                    );
                    EXECUTE format(
                        'ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON SEQUENCES FROM %I',
                        current_user, role_name
                    );
                    EXECUTE format(
                        'ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON FUNCTIONS FROM %I',
                        current_user, role_name
                    );
                    EXECUTE format(
                        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
                        'REVOKE ALL ON TABLES FROM %I', current_user, v_schema, role_name
                    );
                    EXECUTE format(
                        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
                        'REVOKE ALL ON SEQUENCES FROM %I', current_user, v_schema, role_name
                    );
                    EXECUTE format(
                        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
                        'REVOKE ALL ON FUNCTIONS FROM %I', current_user, v_schema, role_name
                    );
                END IF;
            END LOOP;
            IF EXISTS (
                SELECT 1
                 FROM pg_default_acl AS defaults
                  CROSS JOIN LATERAL aclexplode(
                      COALESCE(defaults.defaclacl, '{}'::aclitem[])
                  ) AS acl
                 WHERE defaults.defaclrole = owner_oid
                   AND defaults.defaclnamespace IN (0, schema_oid)
                   AND (
                       acl.grantee = 0
                       OR acl.grantee IN (
                           SELECT oid FROM pg_roles
                            WHERE rolname IN ('ledgerbridge_reader',
                                              'ledgerbridge_api',
                                              'ledgerbridge_worker',
                                              'ledgerbridge_app', 'ledgerbridge_backup')
                       )
                   )
            ) THEN
                RAISE EXCEPTION 'owner default ACL for schema % is not closed', v_schema
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;
        END
        $default_acl$;
        """
    ).replace("__SCHEMA_LITERAL__", repr(schema_name))
    op.execute(sql)


def _grant_exact_surface() -> None:
    op.execute(
        """
        DO $grant$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY[
                'ledgerbridge_reader', 'ledgerbridge_api',
                'ledgerbridge_worker', 'ledgerbridge_app'
            ] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format(
                        'REVOKE ALL ON TABLE '
                        'public.encrypted_object_identity, '
                        'public.reconciliation_snapshot_blocker, '
                        'public.business_unit, public.reporting_category, '
                        'public.evidence_object, public.encrypted_blob_version, '
                        'public.candidate, public.candidate_source, '
                        'public.candidate_revision, public.candidate_blocker, '
                        'public.candidate_event, public.candidate_field_change, '
                        'public.candidate_conflict_resolution, public.candidate_evidence, '
                        'public.journal_entry_attribution, public.posting_attribution, '
                        'public.reconciliation_leg, '
                        'public.reconciliation_snapshot, '
                        'public.reconciliation_snapshot_proposal, '
                        'public.reconciliation_snapshot_suspense FROM %I', role_name
                    );
                END IF;
            END LOOP;
        END
        $grant$;
        REVOKE ALL ON TABLE
            public.encrypted_object_identity, public.reconciliation_snapshot_blocker,
            public.business_unit, public.reporting_category, public.evidence_object,
            public.encrypted_blob_version, public.candidate, public.candidate_source,
            public.candidate_revision, public.candidate_blocker, public.candidate_event,
            public.candidate_field_change, public.candidate_conflict_resolution,
            public.candidate_evidence, public.journal_entry_attribution,
            public.posting_attribution, public.reconciliation_leg,
            public.reconciliation_snapshot,
            public.reconciliation_snapshot_proposal, public.reconciliation_snapshot_suspense
        FROM PUBLIC;
        REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ledgerbridge_reader;
        REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ledgerbridge_reader;
        REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM ledgerbridge_reader;
        REVOKE ALL ON FUNCTION public.append_audit_event(text, text, text, text, jsonb)
            FROM ledgerbridge_reader;
        REVOKE ALL ON FUNCTION public.r1_assert_posted_total_integrity()
            FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker;
        DO $helper_acl$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY['ledgerbridge_app', 'ledgerbridge_backup'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format(
                        'REVOKE ALL ON FUNCTION public.r1_assert_posted_total_integrity() FROM %I',
                        role_name
                    );
                END IF;
            END LOOP;
        END
        $helper_acl$;
        REVOKE USAGE ON SCHEMA public FROM ledgerbridge_reader;
        DO $schema_acl$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY[
                'ledgerbridge_api', 'ledgerbridge_worker',
                'ledgerbridge_app', 'ledgerbridge_reader', 'ledgerbridge_backup'
            ] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format('REVOKE ALL ON SCHEMA internal_read FROM %I', role_name);
                END IF;
            END LOOP;
        END
        $schema_acl$;
        REVOKE ALL ON SCHEMA internal_read FROM PUBLIC;
        GRANT USAGE ON SCHEMA internal_read TO ledgerbridge_reader;
        REVOKE ALL ON ALL FUNCTIONS IN SCHEMA internal_read FROM PUBLIC, ledgerbridge_reader;
        DO $function_acl$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY[
                'ledgerbridge_api', 'ledgerbridge_worker', 'ledgerbridge_app',
                'ledgerbridge_backup'
            ] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format(
                        'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA internal_read FROM %I',
                        role_name
                    );
                END IF;
            END LOOP;
        END
        $function_acl$;
        REVOKE ALL ON ALL TABLES IN SCHEMA internal_read
            FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker;
        DO $table_acl$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY['ledgerbridge_app', 'ledgerbridge_backup'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format(
                        'REVOKE ALL ON ALL TABLES IN SCHEMA internal_read FROM %I',
                        role_name
                    );
                END IF;
            END LOOP;
        END
        $table_acl$;
        -- These views are owner-controlled projection helpers, not an
        -- authorization boundary.  The reader must use the SECURITY DEFINER
        -- functions below, which require entity/scope and audit-horizon
        -- parameters; leaving direct SELECT revoked is the fail-closed
        -- default until every projection has an equivalent scoped function.
        REVOKE ALL ON internal_read.candidate_current_v,
            internal_read.candidate_evidence_v, internal_read.evidence_metadata_v,
            internal_read.reconciliation_current_v,
            internal_read.reconciliation_blocker_v,
            internal_read.reconciliation_proposal_v,
            internal_read.reconciliation_suspense_v,
            internal_read.ledger_posted_total_v FROM ledgerbridge_reader;
        GRANT EXECUTE ON FUNCTION internal_read.current_audit_horizon(),
            internal_read.list_candidates_as_of(
                uuid, uuid, varchar(16), bigint, bytea, timestamptz, uuid, integer
            ),
            internal_read.get_reconciliation_as_of(uuid, uuid, date, bigint, bytea),
            internal_read.get_ledger_summary_as_of(uuid, uuid, date, date, bigint, bytea),
            internal_read.resolve_active_evidence_blob(uuid),
            internal_read.append_internal_evidence_read_audit(
                uuid, varchar(200), varchar(200), varchar(128), uuid, uuid, uuid, uuid, bigint, bytea
            ) TO ledgerbridge_reader;
        """
    )


def upgrade() -> None:
    _runtime_role_preflight()
    _database_acl()
    op.execute(
        """
        DO $schema$
        BEGIN
            EXECUTE format('CREATE SCHEMA internal_read AUTHORIZATION %I', current_user);
        END
        $schema$;
        REVOKE ALL ON SCHEMA internal_read FROM PUBLIC;
        """
    )
    _revoke_owner_default_acls("internal_read")
    op.execute(
        """
        CREATE TABLE internal_read.evidence_read_receipt (
            operation_id uuid NOT NULL,
            audit_event_id uuid NOT NULL,
            principal_ref varchar(200) NOT NULL,
            verified_san varchar(200) NOT NULL,
            policy_generation varchar(128) NOT NULL,
            evidence_ref uuid NOT NULL,
            entity_id uuid NOT NULL,
            business_unit_id uuid NOT NULL,
            blob_ref uuid NOT NULL,
            byte_size bigint NOT NULL,
            plaintext_sha256 bytea NOT NULL,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT pk_evidence_read_receipt PRIMARY KEY (operation_id),
            CONSTRAINT uq_evidence_read_receipt_audit UNIQUE (audit_event_id),
            CONSTRAINT ck_evidence_read_receipt_principal
                CHECK (btrim(principal_ref) <> ''),
            CONSTRAINT ck_evidence_read_receipt_san
                CHECK (verified_san ~ '^spiffe://ledgerbridge(\\.test)?/[a-z0-9/_-]+$'),
            CONSTRAINT ck_evidence_read_receipt_policy
                CHECK (policy_generation ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
            CONSTRAINT ck_evidence_read_receipt_size
                CHECK (byte_size BETWEEN 0 AND 134217728),
            CONSTRAINT ck_evidence_read_receipt_sha
                CHECK (octet_length(plaintext_sha256) = 32),
            CONSTRAINT fk_evidence_read_receipt_audit
                FOREIGN KEY (audit_event_id) REFERENCES public.audit_event(id),
            CONSTRAINT fk_evidence_read_receipt_evidence
                FOREIGN KEY (evidence_ref) REFERENCES public.evidence_object(evidence_ref),
            CONSTRAINT fk_evidence_read_receipt_blob
                FOREIGN KEY (blob_ref) REFERENCES public.encrypted_blob_version(blob_ref)
        );
        CREATE FUNCTION public.r1_evidence_read_receipt_append_only()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION 'evidence read receipts are append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $function$;
        CREATE FUNCTION public.r1_validate_evidence_read_receipt()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_xid xid;
            v_action text;
            v_payload jsonb;
            v_business_unit_ref varchar(100);
            v_expected jsonb;
        BEGIN
            SELECT a.xmin, a.action, a.payload
              INTO v_xid, v_action, v_payload
              FROM public.audit_event AS a
             WHERE a.id = NEW.audit_event_id;
            SELECT ref INTO STRICT v_business_unit_ref
              FROM public.business_unit
             WHERE id = NEW.business_unit_id AND entity_id = NEW.entity_id;
            v_expected := jsonb_build_object(
                'receipt_type', 'ledgerbridge.evidence_read_receipt.v1',
                'operation_id', NEW.operation_id::text,
                'event_type', 'EVIDENCE_CONTENT_READ',
                'principal_san_uri', NEW.verified_san,
                'policy_generation', NEW.policy_generation,
                'evidence_ref', NEW.evidence_ref::text,
                'entity_ref', NEW.entity_id::text,
                'business_unit_ref', v_business_unit_ref,
                'blob_ref', NEW.blob_ref::text,
                'byte_size', NEW.byte_size,
                'sha256', encode(NEW.plaintext_sha256, 'hex'),
                'outcome', 'SUCCEEDED'
            );
            IF v_xid IS NULL
               OR pg_xact_status(v_xid::text::xid8) IS DISTINCT FROM 'in progress'
               OR v_action IS DISTINCT FROM 'internal.read.evidence.content'
               OR v_payload IS DISTINCT FROM v_expected THEN
                RAISE EXCEPTION 'evidence read receipt audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER evidence_read_receipt_audit_binding
        AFTER INSERT ON internal_read.evidence_read_receipt
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_evidence_read_receipt();
        CREATE TRIGGER evidence_read_receipt_append_only
        BEFORE UPDATE OR DELETE ON internal_read.evidence_read_receipt
        FOR EACH ROW EXECUTE FUNCTION public.r1_evidence_read_receipt_append_only();

        -- Migration 0014 makes attribution rows immutable, but it still
        -- permits legacy POSTED rows that have not opted into the R1
        -- attribution boundary.  A plain inner join here would silently drop
        -- those facts.  The owner-only guard turns every missing, duplicate,
        -- or mismatched attribution/primary posting into a fail-closed error
        -- before the view can materialize a total.
        CREATE FUNCTION public.r1_assert_posted_total_integrity()
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE v_bad_count bigint;
        BEGIN
            SELECT count(*) INTO v_bad_count
              FROM (
                  SELECT je.id, je.primary_account_id,
                         (SELECT count(*)
                            FROM public.journal_entry_attribution AS ja
                           WHERE ja.entry_id = je.id) AS attribution_count,
                         (SELECT count(*)
                            FROM public.posting AS p
                           WHERE p.entry_id = je.id
                             AND p.account_id = je.primary_account_id)
                             AS primary_posting_count
                    FROM public.journal_entry AS je
                   WHERE je.status = 'POSTED'
              ) AS entry_shape
             WHERE entry_shape.primary_account_id IS NULL
                OR entry_shape.attribution_count <> 1
                OR entry_shape.primary_posting_count <> 1;
            IF v_bad_count <> 0 THEN
                RAISE EXCEPTION 'POSTED total has incomplete scope or primary posting attribution'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            SELECT count(*) INTO v_bad_count
              FROM (
                  SELECT p.id
                    FROM public.posting AS p
                    JOIN public.journal_entry AS je ON je.id = p.entry_id
                    LEFT JOIN public.posting_attribution AS pa
                      ON pa.posting_id = p.id
                   WHERE je.status = 'POSTED'
                   GROUP BY p.id
                  HAVING count(pa.posting_id) <> 1
              ) AS posting_shape;
            IF v_bad_count <> 0 THEN
                RAISE EXCEPTION 'POSTED total has incomplete posting category attribution'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM public.journal_entry_attribution AS ja
                  JOIN public.journal_entry AS je ON je.id = ja.entry_id
                 WHERE je.status = 'POSTED'
                   AND (
                       ja.entity_id IS DISTINCT FROM je.entity_id
                       OR
                       ja.business_unit_id IS NULL
                       OR ja.accounting_month IS NULL
                       OR NOT EXISTS (
                           SELECT 1
                             FROM public.business_unit AS bu
                            WHERE bu.id = ja.business_unit_id
                              AND bu.entity_id = je.entity_id
                       )
                   )
            ) THEN
                RAISE EXCEPTION 'POSTED total has an invalid business-unit scope'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM public.posting_attribution AS pa
                  JOIN public.posting AS p ON p.id = pa.posting_id
                  JOIN public.journal_entry AS je ON je.id = p.entry_id
                  JOIN public.reporting_category AS rc
                    ON rc.id = pa.reporting_category_id
                 WHERE je.status = 'POSTED'
                   AND (
                       rc.entity_id IS DISTINCT FROM je.entity_id
                       OR pa.category_code_snapshot IS DISTINCT FROM rc.code
                       OR pa.category_label_snapshot IS DISTINCT FROM rc.label
                   )
            ) THEN
                RAISE EXCEPTION 'POSTED total has an invalid category attribution'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN true;
        END
        $function$;

        CREATE VIEW internal_read.candidate_current_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT c.contract_version,
               c.id AS candidate_ref,
               c.short_id,
               r.revision,
               r.status,
               c.entity_id AS entity_ref,
               r.business_unit_ref_snapshot AS business_unit_ref,
               r.business_unit_label_snapshot AS business_unit_label,
               r.category_code_snapshot AS category_code,
               r.category_label_snapshot AS category_label,
               r.amount_minor,
               r.currency,
               to_char(r.accounting_month, 'YYYY-MM')::varchar(7) AS accounting_month,
               r.summary,
               r.confidence_basis_points,
               c.created_at,
               r.updated_at,
               c.supersedes_candidate_id AS supersedes_candidate_ref
          FROM public.candidate AS c
          JOIN LATERAL (
               SELECT cr.revision, cr.status, cr.business_unit_ref_snapshot,
                      cr.business_unit_label_snapshot, cr.category_code_snapshot,
                      cr.category_label_snapshot, cr.amount_minor, cr.currency,
                      cr.accounting_month, cr.summary, cr.confidence_basis_points,
                      cr.updated_at
                 FROM public.candidate_revision AS cr
                 JOIN public.candidate_event AS ce
                   ON ce.candidate_id = cr.candidate_id
                  AND ce.to_revision = cr.revision
                  AND ce.to_status = cr.status
                 JOIN public.audit_event AS ae ON ae.id = ce.audit_event_id
                WHERE cr.candidate_id = c.id
                  AND ae.action = CASE WHEN ce.event_type = 'CREATE'
                                       THEN 'candidate.create'
                                       ELSE 'candidate.transition' END
                  AND EXISTS (
                      SELECT 1
                        FROM public.candidate_revision AS cr0
                        JOIN public.candidate_event AS ce0
                          ON ce0.candidate_id = cr0.candidate_id
                         AND ce0.to_revision = cr0.revision
                         AND ce0.to_status = cr0.status
                        JOIN public.audit_event AS ae0 ON ae0.id = ce0.audit_event_id
                       WHERE cr0.candidate_id = c.id
                         AND cr0.revision = 1
                         AND ce0.event_type = 'CREATE'
                         AND ae0.action = 'candidate.create'
                  )
                ORDER BY cr.revision DESC
               LIMIT 1
          ) AS r ON TRUE;

        CREATE VIEW internal_read.candidate_evidence_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT candidate_id, ordinal, evidence_ref, kind, media_type_snapshot,
               display_name_snapshot, download_available
          FROM public.candidate_evidence;

        CREATE VIEW internal_read.evidence_metadata_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT evidence_ref, entity_id, business_unit_id, media_type, display_name,
               plaintext_sha256, plaintext_size, created_at
          FROM public.evidence_object;

        CREATE VIEW internal_read.reconciliation_current_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT s.entity_id AS entity_ref,
               bu.ref AS business_unit_ref,
               to_char(s.accounting_month, 'YYYY-MM')::varchar(7) AS month,
               s.snapshot_revision, s.posted_amount_minor, s.currency
          FROM public.reconciliation_snapshot AS s
          JOIN public.business_unit AS bu
            ON bu.id = s.business_unit_id AND bu.entity_id = s.entity_id
          JOIN LATERAL (
              SELECT max(s2.snapshot_revision) AS revision
                FROM public.reconciliation_snapshot AS s2
               WHERE s2.entity_id = s.entity_id
                 AND s2.business_unit_id = s.business_unit_id
                 AND s2.accounting_month = s.accounting_month
          ) AS tip ON tip.revision = s.snapshot_revision;

        CREATE VIEW internal_read.reconciliation_blocker_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT snapshot_ref, ordinal, code, message, field, conflict_ref, evidence_ref
          FROM public.reconciliation_snapshot_blocker;

        CREATE VIEW internal_read.reconciliation_proposal_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT snapshot_ref, proposal_ref, reconciliation_group_id, relation, status,
               amount_minor, currency, amount_basis
          FROM public.reconciliation_snapshot_proposal;

        CREATE VIEW internal_read.reconciliation_suspense_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT snapshot_ref, suspense_ref, suspense_item_id, status, reason,
               amount_minor, currency
          FROM public.reconciliation_snapshot_suspense;

        CREATE VIEW internal_read.ledger_posted_total_v
        WITH (security_barrier = true, security_invoker = false) AS
        -- Keep the owner-only guard as a MATERIALIZED, outer LATERAL driver:
        -- it must execute before the fact joins even when a malformed POSTED
        -- row would otherwise match no join and disappear from the result.
        WITH posted_guard AS MATERIALIZED (
            SELECT public.r1_assert_posted_total_integrity() AS ok
        )
        SELECT posted.entity_id,
               posted.business_unit_id,
               posted.accounting_month,
               posted.category_code,
               posted.category_label,
               posted.currency,
               sum(posted.amount_minor)::bigint AS posted_amount_minor
          FROM posted_guard AS guard
         CROSS JOIN LATERAL (
            SELECT je.entity_id,
                   ja.business_unit_id,
                   ja.accounting_month,
                   pa.category_code_snapshot AS category_code,
                   pa.category_label_snapshot AS category_label,
                   p.currency,
                   p.amount_minor
              FROM public.journal_entry AS je
              JOIN public.journal_entry_attribution AS ja ON ja.entry_id = je.id
              JOIN public.posting AS p
                ON p.entry_id = je.id AND p.account_id = je.primary_account_id
              JOIN public.posting_attribution AS pa ON pa.posting_id = p.id
             WHERE guard.ok IS TRUE AND je.status = 'POSTED'
         ) AS posted
         GROUP BY posted.entity_id, posted.business_unit_id, posted.accounting_month,
                  posted.category_code, posted.category_label, posted.currency;
        """
    )

    op.execute(
        """
        CREATE FUNCTION internal_read.current_audit_horizon()
        RETURNS TABLE (sequence bigint, hash bytea)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE v_sequence bigint; v_hash bytea;
        BEGIN
            SELECT ae.sequence, ae.hash INTO v_sequence, v_hash
              FROM public.audit_event AS ae
             ORDER BY ae.sequence DESC LIMIT 1;
            IF NOT FOUND OR v_sequence IS NULL OR v_hash IS NULL
               OR octet_length(v_hash) <> 32 THEN
                RAISE EXCEPTION 'audit chain is empty or malformed'
                    USING ERRCODE = '22023';
            END IF;
            RETURN QUERY SELECT v_sequence, v_hash;
        END
        $function$;

        CREATE FUNCTION internal_read.list_candidates_as_of(
            p_entity_id uuid,
            p_business_unit_id uuid,
            p_status varchar(16),
            p_audit_horizon_sequence bigint,
            p_audit_horizon_hash bytea,
            p_last_created_at timestamptz,
            p_last_candidate_id uuid,
            p_limit integer
        )
        RETURNS TABLE (
            contract_version varchar(32), candidate_ref uuid, short_id varchar(10),
            revision integer, status varchar(16), entity_ref uuid,
            business_unit_ref varchar(100), business_unit_label varchar(200),
            category_code varchar(100), category_label varchar(200), amount_minor bigint,
            currency varchar(3), accounting_month varchar(7), summary varchar(500),
            confidence_basis_points smallint, source jsonb, evidence jsonb,
            blockers jsonb, review_summary jsonb, created_at timestamptz,
            updated_at timestamptz, supersedes_candidate_ref uuid,
            superseded_by_candidate_ref uuid
        ) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        BEGIN
            IF p_entity_id IS NULL OR p_audit_horizon_sequence IS NULL
               OR p_audit_horizon_sequence <= 0 OR p_audit_horizon_hash IS NULL
               OR octet_length(p_audit_horizon_hash) <> 32
               OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
               OR (p_last_created_at IS NULL) <> (p_last_candidate_id IS NULL) THEN
                RAISE EXCEPTION 'invalid candidate read parameters' USING ERRCODE = '22023';
            END IF;
            IF p_status IS NOT NULL AND p_status NOT IN
               ('INCOMPLETE','CONFLICTED','PENDING','CONFIRMED','IGNORED','SUPERSEDED') THEN
                RAISE EXCEPTION 'invalid candidate status' USING ERRCODE = '22023';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.audit_event AS horizon
                 WHERE horizon.sequence = p_audit_horizon_sequence
                   AND horizon.hash = p_audit_horizon_hash
                   AND octet_length(horizon.hash) = 32
            ) THEN
                RAISE EXCEPTION 'audit horizon is not an exact chain row'
                    USING ERRCODE = '22023';
            END IF;
            IF p_business_unit_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.business_unit AS bu
                 WHERE bu.id = p_business_unit_id AND bu.entity_id = p_entity_id
            ) THEN
                RAISE EXCEPTION 'business unit does not belong to entity'
                    USING ERRCODE = '22023';
            END IF;
            IF p_last_created_at IS NOT NULL AND NOT EXISTS (
                SELECT 1
                  FROM public.candidate AS cursor_candidate
                  JOIN LATERAL (
                      SELECT cr.revision, cr.status, cr.business_unit_id
                        FROM public.candidate_revision AS cr
                        JOIN public.candidate_event AS ce
                         ON ce.candidate_id = cr.candidate_id
                         AND ce.to_revision = cr.revision
                         AND ce.to_status = cr.status
                        JOIN public.audit_event AS ae ON ae.id = ce.audit_event_id
                       WHERE cr.candidate_id = cursor_candidate.id
                         AND ae.sequence <= p_audit_horizon_sequence
                         AND EXISTS (
                             SELECT 1
                               FROM public.candidate_revision AS cr0
                               JOIN public.candidate_event AS ce0
                                 ON ce0.candidate_id = cr0.candidate_id
                                AND ce0.to_revision = cr0.revision
                                AND ce0.to_status = cr0.status
                               JOIN public.audit_event AS ae0 ON ae0.id = ce0.audit_event_id
                              WHERE cr0.candidate_id = cursor_candidate.id
                                AND cr0.revision = 1
                                AND ce0.event_type = 'CREATE'
                                AND ae0.sequence <= p_audit_horizon_sequence
                         )
                       ORDER BY cr.revision DESC LIMIT 1
                  ) AS cursor_tip ON TRUE
                 WHERE cursor_candidate.id = p_last_candidate_id
                   AND cursor_candidate.entity_id = p_entity_id
                   AND cursor_candidate.created_at = p_last_created_at
                   AND ((p_business_unit_id IS NULL AND cursor_tip.business_unit_id IS NULL)
                        OR cursor_tip.business_unit_id = p_business_unit_id)
                   AND (p_status IS NULL OR cursor_tip.status = p_status)
            ) THEN
                RAISE EXCEPTION 'candidate cursor is outside requested scope'
                    USING ERRCODE = '22023';
            END IF;
            RETURN QUERY
            SELECT c.contract_version, c.id, c.short_id, r.revision, r.status,
                   c.entity_id, r.business_unit_ref_snapshot,
                   r.business_unit_label_snapshot, r.category_code_snapshot,
                   r.category_label_snapshot, r.amount_minor, r.currency,
                   to_char(r.accounting_month, 'YYYY-MM')::varchar(7), r.summary,
                   r.confidence_basis_points,
                   jsonb_build_object(
                       'ingest_channel', cs.ingest_channel_id,
                       'source_system', cs.source_system_id,
                       'source_event_ref', cs.source_event_ref,
                       'display_label', cs.display_label
                   ),
                   CASE WHEN r.business_unit_id IS NULL THEN '[]'::jsonb ELSE coalesce((
                       SELECT jsonb_agg(jsonb_build_object(
                           'evidence_ref', ce.evidence_ref,
                           'kind', ce.kind,
                           'media_type', ce.media_type_snapshot,
                           'display_name', ce.display_name_snapshot,
                           'download_available', ce.download_available
                       ) ORDER BY ce.ordinal)
                       FROM public.candidate_evidence AS ce
                       WHERE ce.candidate_id = c.id
                         AND ce.evidence_entity_id = c.entity_id
                         AND ce.evidence_business_unit_id = r.business_unit_id
                   ), '[]'::jsonb) END,
                   coalesce((
                       SELECT jsonb_agg(jsonb_build_object(
                           'code', b.code, 'message', b.message, 'field', b.field,
                           'conflict_ref', b.conflict_ref, 'evidence_ref', b.evidence_ref
                       ) ORDER BY b.ordinal)
                       FROM public.candidate_blocker AS b
                       WHERE b.candidate_id = c.id AND b.revision = r.revision
                   ), '[]'::jsonb),
                   jsonb_build_object(
                       'event_count', greatest(r.revision - 1, 0),
                       'last_action', (
                           SELECT ce2.action FROM public.candidate_event AS ce2
                            WHERE ce2.candidate_id = c.id
                              AND ce2.to_revision = r.revision
                              AND ce2.event_type <> 'CREATE'
                       ),
                       'last_decided_at', (
                           SELECT ce3.occurred_at FROM public.candidate_event AS ce3
                            WHERE ce3.candidate_id = c.id
                              AND ce3.to_revision = r.revision
                              AND ce3.event_type <> 'CREATE'
                       ),
                       'current_revision', r.revision
                   ),
                   c.created_at, r.updated_at, c.supersedes_candidate_id,
                   CASE WHEN r.status = 'SUPERSEDED' THEN (
                       SELECT successor.id
                         FROM public.candidate AS successor
                        WHERE successor.supersedes_candidate_id = c.id
                          AND successor.entity_id = c.entity_id
                          AND EXISTS (
                              SELECT 1
                                FROM public.candidate_revision AS sr
                                JOIN public.candidate_event AS se
                                  ON se.candidate_id = sr.candidate_id
                                 AND se.to_revision = sr.revision
                                 AND se.to_status = sr.status
                                JOIN public.audit_event AS sae ON sae.id = se.audit_event_id
                               WHERE sr.candidate_id = successor.id
                                 AND sr.revision = 1
                                 AND se.event_type = 'CREATE'
                                 AND sae.sequence <= p_audit_horizon_sequence
                          )
                        LIMIT 1
                   ) END
              FROM public.candidate AS c
              JOIN public.candidate_source AS cs ON cs.candidate_id = c.id
              JOIN LATERAL (
                  SELECT cr.revision, cr.status, cr.business_unit_id,
                         cr.business_unit_ref_snapshot, cr.business_unit_label_snapshot,
                         cr.category_code_snapshot, cr.category_label_snapshot,
                         cr.amount_minor, cr.currency, cr.accounting_month, cr.summary,
                         cr.confidence_basis_points, cr.created_at, cr.updated_at
                    FROM public.candidate_revision AS cr
                    JOIN public.candidate_event AS ev
                      ON ev.candidate_id = cr.candidate_id
                     AND ev.to_revision = cr.revision
                     AND ev.to_status = cr.status
                    JOIN public.audit_event AS ae ON ae.id = ev.audit_event_id
                   WHERE cr.candidate_id = c.id
                     AND ae.sequence <= p_audit_horizon_sequence
                     AND EXISTS (
                         SELECT 1
                           FROM public.candidate_revision AS cr0
                           JOIN public.candidate_event AS ev0
                             ON ev0.candidate_id = cr0.candidate_id
                            AND ev0.to_revision = cr0.revision
                            AND ev0.to_status = cr0.status
                           JOIN public.audit_event AS ae0 ON ae0.id = ev0.audit_event_id
                          WHERE cr0.candidate_id = c.id
                            AND cr0.revision = 1
                            AND ev0.event_type = 'CREATE'
                            AND ae0.sequence <= p_audit_horizon_sequence
                     )
                   ORDER BY cr.revision DESC
                   LIMIT 1
              ) AS r ON TRUE
             WHERE c.entity_id = p_entity_id
               AND ((p_business_unit_id IS NULL AND r.business_unit_id IS NULL)
                    OR r.business_unit_id = p_business_unit_id)
               AND (p_status IS NULL OR r.status = p_status)
               AND (p_last_created_at IS NULL
                    OR (c.created_at, c.id) > (p_last_created_at, p_last_candidate_id))
             ORDER BY c.created_at, c.id
             LIMIT (p_limit + 1);
        END
        $function$;

        CREATE FUNCTION internal_read.get_reconciliation_as_of(
            p_entity_id uuid,
            p_business_unit_id uuid,
            p_accounting_month date,
            p_audit_horizon_sequence bigint,
            p_audit_horizon_hash bytea
        )
        RETURNS TABLE (
            entity_ref uuid, business_unit_ref varchar(100), month varchar(7),
            snapshot_revision integer, blockers jsonb, proposals jsonb,
            suspense jsonb, posted_amount_minor bigint, currency varchar(3)
        ) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        BEGIN
            IF p_entity_id IS NULL OR p_business_unit_id IS NULL
               OR p_accounting_month IS NULL
               OR p_accounting_month <> date_trunc('month', p_accounting_month)::date
               OR p_audit_horizon_sequence IS NULL OR p_audit_horizon_sequence <= 0
               OR p_audit_horizon_hash IS NULL OR octet_length(p_audit_horizon_hash) <> 32
               OR NOT EXISTS (
                   SELECT 1 FROM public.audit_event AS horizon
                    WHERE horizon.sequence = p_audit_horizon_sequence
                      AND horizon.hash = p_audit_horizon_hash
                      AND octet_length(horizon.hash) = 32
               ) THEN
                RAISE EXCEPTION 'invalid reconciliation read parameters'
                    USING ERRCODE = '22023';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.business_unit AS bu
                 WHERE bu.id = p_business_unit_id AND bu.entity_id = p_entity_id
            ) THEN
                RAISE EXCEPTION 'business unit does not belong to entity'
                    USING ERRCODE = '22023';
            END IF;
            RETURN QUERY
            SELECT s.entity_id, bu.ref,
                   to_char(s.accounting_month, 'YYYY-MM')::varchar(7),
                   s.snapshot_revision,
                   coalesce((
                       SELECT jsonb_agg(jsonb_build_object(
                           'code', b.code, 'message', b.message, 'field', b.field,
                           'conflict_ref', b.conflict_ref, 'evidence_ref', b.evidence_ref
                       ) ORDER BY b.ordinal)
                       FROM public.reconciliation_snapshot_blocker AS b
                       WHERE b.snapshot_ref = s.snapshot_ref
                   ), '[]'::jsonb),
                   coalesce((
                       SELECT jsonb_agg(jsonb_build_object(
                           'proposal_ref', p.proposal_ref,
                           'relation', p.relation, 'status', p.status,
                           'amount_minor', p.amount_minor, 'currency', p.currency
                       ) ORDER BY p.proposal_ref)
                       FROM public.reconciliation_snapshot_proposal AS p
                       WHERE p.snapshot_ref = s.snapshot_ref
                   ), '[]'::jsonb),
                   coalesce((
                       SELECT jsonb_agg(jsonb_build_object(
                           'suspense_ref', x.suspense_ref, 'status', x.status,
                           'reason', x.reason, 'amount_minor', x.amount_minor,
                           'currency', x.currency
                       ) ORDER BY x.suspense_ref)
                       FROM public.reconciliation_snapshot_suspense AS x
                       WHERE x.snapshot_ref = s.snapshot_ref
                   ), '[]'::jsonb),
                   s.posted_amount_minor, s.currency
              FROM public.reconciliation_snapshot AS s
              JOIN public.business_unit AS bu
                ON bu.id = s.business_unit_id AND bu.entity_id = s.entity_id
              JOIN public.audit_event AS snapshot_audit ON snapshot_audit.id = s.audit_event_id
             WHERE s.entity_id = p_entity_id
               AND s.business_unit_id = p_business_unit_id
               AND s.accounting_month = p_accounting_month
               AND snapshot_audit.sequence <= p_audit_horizon_sequence
               AND EXISTS (
                   SELECT 1 FROM public.audit_event AS watermark
                    WHERE watermark.sequence = s.ledger_audit_sequence
                      AND watermark.hash = s.ledger_audit_hash
                      AND watermark.sequence <= p_audit_horizon_sequence
               )
             ORDER BY s.snapshot_revision DESC
             LIMIT 1;
        END
        $function$;

        CREATE FUNCTION internal_read.resolve_active_evidence_blob(p_evidence_ref uuid)
        RETURNS TABLE (
            blob_ref uuid, evidence_ref uuid, predecessor_blob_ref uuid,
            entity_id uuid, business_unit_id uuid, business_unit_ref varchar(100),
            media_type varchar(200), display_name varchar(200),
            object_ref varchar(64), plaintext_sha256 bytea, plaintext_size bigint,
            ciphertext_sha256 bytea, ciphertext_size bigint, storage_key varchar(77),
            envelope_schema varchar(28), algorithm varchar(40), chunk_size integer,
            stream_header bytea, wrapped_key_generation varchar(128),
            wrapped_key_nonce bytea, wrapped_key_ciphertext bytea, purpose varchar(32),
            aad_scheme varchar(40), created_at timestamptz
        ) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE v_tip_count bigint; v_evidence public.evidence_object%ROWTYPE;
        BEGIN
            IF p_evidence_ref IS NULL THEN
                RAISE EXCEPTION 'evidence reference is required' USING ERRCODE = '22023';
            END IF;
            -- Unknown references are intentionally indistinguishable from an
            -- out-of-scope object at the reader boundary.  Integrity checks
            -- below apply only after the immutable evidence row exists.
            IF NOT EXISTS (
                SELECT 1 FROM public.evidence_object AS e
                 WHERE e.evidence_ref = p_evidence_ref
            ) THEN
                RETURN;
            END IF;
            SELECT count(*) INTO v_tip_count
              FROM public.encrypted_blob_version AS b
             WHERE b.evidence_ref = p_evidence_ref
               AND b.predecessor_blob_ref IS NULL;
            IF v_tip_count <> 1 THEN
                RAISE EXCEPTION 'active blob chain has no unique genesis'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.encrypted_blob_version AS b
                  LEFT JOIN public.encrypted_object_identity AS oi
                    ON oi.object_ref = b.object_ref
                   AND oi.evidence_ref = b.evidence_ref
                 WHERE b.evidence_ref = p_evidence_ref
                   AND oi.object_ref IS NULL
            ) THEN
                RAISE EXCEPTION 'active blob identity binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.encrypted_blob_version AS b
                 WHERE b.evidence_ref = p_evidence_ref
                   AND b.predecessor_blob_ref IS NOT NULL
                 GROUP BY b.predecessor_blob_ref
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION 'active blob chain branches'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (
                WITH RECURSIVE walk(node_ref, path, cycle) AS (
                    SELECT b.predecessor_blob_ref, ARRAY[b.blob_ref], false
                      FROM public.encrypted_blob_version AS b
                     WHERE b.evidence_ref = p_evidence_ref
                       AND b.predecessor_blob_ref IS NOT NULL
                    UNION ALL
                    SELECT b.predecessor_blob_ref, w.path || b.blob_ref,
                           b.blob_ref = ANY(w.path)
                      FROM walk AS w
                      JOIN public.encrypted_blob_version AS b
                        ON b.blob_ref = w.node_ref
                     WHERE w.node_ref IS NOT NULL AND NOT w.cycle
                )
                SELECT 1 FROM walk WHERE cycle
            ) THEN
                RAISE EXCEPTION 'active blob chain contains a cycle'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT count(*) INTO v_tip_count
              FROM public.encrypted_blob_version AS b
             WHERE b.evidence_ref = p_evidence_ref
               AND NOT EXISTS (
                   SELECT 1 FROM public.encrypted_blob_version AS child
                    WHERE child.predecessor_blob_ref = b.blob_ref
               );
            IF v_tip_count <> 1 THEN
                RAISE EXCEPTION 'evidence has no unique active encrypted blob tip'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT e.* INTO STRICT v_evidence
              FROM public.evidence_object AS e
             WHERE e.evidence_ref = p_evidence_ref;
            RETURN QUERY
            SELECT b.blob_ref, b.evidence_ref, b.predecessor_blob_ref,
                   v_evidence.entity_id, v_evidence.business_unit_id, bu.ref,
                   v_evidence.media_type, v_evidence.display_name, b.object_ref,
                   v_evidence.plaintext_sha256, v_evidence.plaintext_size,
                   b.ciphertext_sha256, b.ciphertext_size, b.storage_key,
                   b.envelope_schema, b.algorithm, b.chunk_size, b.stream_header,
                   b.wrapped_key_generation, b.wrapped_key_nonce,
                   b.wrapped_key_ciphertext, b.purpose,
                   'ledgerbridge.artifact.object.v2'::varchar(40), b.created_at
              FROM public.encrypted_blob_version AS b
              JOIN public.business_unit AS bu
                ON bu.id = v_evidence.business_unit_id
               AND bu.entity_id = v_evidence.entity_id
             WHERE b.evidence_ref = p_evidence_ref
               AND NOT EXISTS (
                   SELECT 1 FROM public.encrypted_blob_version AS child
                    WHERE child.predecessor_blob_ref = b.blob_ref
               )
             ORDER BY b.created_at DESC, b.blob_ref DESC
             LIMIT 1;
        END
        $function$;

        CREATE FUNCTION internal_read.get_ledger_summary_as_of(
            p_entity_id uuid, p_business_unit_id uuid,
            p_from_month date, p_to_month date,
            p_audit_horizon_sequence bigint, p_audit_horizon_hash bytea
        )
        RETURNS TABLE (
            entity_ref uuid, business_unit_ref varchar(100),
            from_month varchar(7), to_month varchar(7),
            posting_status varchar(6), currency varchar(3),
            category_code varchar(100), amount_minor bigint
        ) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        BEGIN
            IF p_entity_id IS NULL OR p_business_unit_id IS NULL
               OR p_from_month IS NULL OR p_to_month IS NULL
               OR p_from_month <> date_trunc('month', p_from_month)::date
               OR p_to_month <> date_trunc('month', p_to_month)::date
               OR p_from_month > p_to_month
               OR p_audit_horizon_sequence IS NULL
               OR p_audit_horizon_sequence <= 0
               OR p_audit_horizon_hash IS NULL
               OR octet_length(p_audit_horizon_hash) <> 32 THEN
                RAISE EXCEPTION 'invalid ledger summary parameters'
                    USING ERRCODE = '22023';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.audit_event AS horizon
                 WHERE horizon.sequence = p_audit_horizon_sequence
                   AND horizon.hash = p_audit_horizon_hash
                   AND octet_length(horizon.hash) = 32
            ) THEN
                RAISE EXCEPTION 'audit horizon is not an exact chain row'
                    USING ERRCODE = '22023';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.business_unit AS bu
                 WHERE bu.id = p_business_unit_id
                   AND bu.entity_id = p_entity_id
            ) THEN
                RAISE EXCEPTION 'business unit does not belong to entity'
                    USING ERRCODE = '22023';
            END IF;

            -- Never let an inner join silently omit a legacy or malformed
            -- POSTED fact.  The owner-only guard raises before aggregation.
            PERFORM public.r1_assert_posted_total_integrity();

            RETURN QUERY
            SELECT je.entity_id, bu.ref,
                   to_char(p_from_month, 'YYYY-MM')::varchar(7),
                   to_char(p_to_month, 'YYYY-MM')::varchar(7),
                   'POSTED'::varchar(6), p.currency,
                   pa.category_code_snapshot,
                   sum(p.amount_minor)::bigint
              FROM public.journal_entry AS je
              JOIN public.audit_event AS posted_audit
                ON posted_audit.id = je.posted_audit_event_id
               AND posted_audit.sequence <= p_audit_horizon_sequence
              JOIN public.journal_entry_attribution AS ja
                ON ja.entry_id = je.id
              JOIN public.business_unit AS bu
                ON bu.id = ja.business_unit_id
               AND bu.entity_id = ja.entity_id
              JOIN public.posting AS p
                ON p.entry_id = je.id
               AND p.account_id = je.primary_account_id
              JOIN public.posting_attribution AS pa
                ON pa.posting_id = p.id
             WHERE je.status = 'POSTED'
               AND ja.entity_id = p_entity_id
               AND ja.business_unit_id = p_business_unit_id
               AND ja.accounting_month BETWEEN p_from_month AND p_to_month
             GROUP BY je.entity_id, bu.ref, p.currency,
                      pa.category_code_snapshot
             ORDER BY pa.category_code_snapshot;
        END
        $function$;

        CREATE FUNCTION internal_read.append_internal_evidence_read_audit(
            p_operation_id uuid, p_principal_ref varchar(200), p_verified_san varchar(200),
            p_policy_generation varchar(128), p_evidence_ref uuid, p_entity_id uuid,
            p_business_unit_id uuid, p_blob_ref uuid, p_byte_size bigint,
            p_plaintext_sha256 bytea
        )
        RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_evidence public.evidence_object%ROWTYPE;
            v_blob public.encrypted_blob_version%ROWTYPE;
            v_audit uuid;
            v_tip_count bigint;
            v_business_unit_ref varchar(100);
        BEGIN
            IF p_operation_id IS NULL
               OR p_principal_ref IS NULL OR btrim(p_principal_ref) = ''
               OR p_verified_san IS NULL
               OR p_verified_san !~ '^spiffe://ledgerbridge(\\.test)?/[a-z0-9/_-]+$'
               OR p_policy_generation IS NULL
               OR p_policy_generation !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
               OR p_evidence_ref IS NULL OR p_entity_id IS NULL OR p_business_unit_id IS NULL
               OR p_blob_ref IS NULL OR p_byte_size IS NULL
               OR p_byte_size < 0 OR p_byte_size > 134217728
               OR p_plaintext_sha256 IS NULL OR octet_length(p_plaintext_sha256) <> 32 THEN
                RAISE EXCEPTION 'invalid evidence audit receipt' USING ERRCODE = '22023';
            END IF;
            SELECT e.* INTO STRICT v_evidence
              FROM public.evidence_object AS e
             WHERE e.evidence_ref = p_evidence_ref
               AND e.entity_id = p_entity_id
               AND e.business_unit_id = p_business_unit_id;
            PERFORM 1
              FROM internal_read.resolve_active_evidence_blob(p_evidence_ref);
            SELECT count(*) INTO v_tip_count
              FROM public.encrypted_blob_version AS b
             WHERE b.evidence_ref = p_evidence_ref
               AND NOT EXISTS (
                   SELECT 1 FROM public.encrypted_blob_version AS child
                    WHERE child.predecessor_blob_ref = b.blob_ref
               );
            IF v_tip_count <> 1 THEN
                RAISE EXCEPTION 'active blob tip is ambiguous'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT * INTO STRICT v_blob
              FROM public.encrypted_blob_version AS b
             WHERE b.blob_ref = p_blob_ref
               AND b.evidence_ref = p_evidence_ref
               AND NOT EXISTS (
                   SELECT 1 FROM public.encrypted_blob_version AS child
                    WHERE child.predecessor_blob_ref = b.blob_ref
               );
            IF v_evidence.plaintext_size <> p_byte_size
               OR v_evidence.plaintext_sha256 IS DISTINCT FROM p_plaintext_sha256 THEN
                RAISE EXCEPTION 'plaintext digest or size does not match immutable evidence'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT ref INTO STRICT v_business_unit_ref
              FROM public.business_unit
             WHERE id = p_business_unit_id AND entity_id = p_entity_id;
            -- Every argument is explicitly cast to the existing exact
            -- signature.  Schema qualification alone is insufficient when a
            -- future public overload accepts varchar/unknown arguments.
            v_audit := public.append_audit_event(
                p_principal_ref::text,
                'internal.read.evidence.content'::text,
                'internal evidence content read'::text,
                'ledgerbridge.internal-read-audit.v1'::text,
                jsonb_build_object(
                    'receipt_type', 'ledgerbridge.evidence_read_receipt.v1',
                    'operation_id', p_operation_id::text,
                    'event_type', 'EVIDENCE_CONTENT_READ',
                    'principal_san_uri', p_verified_san,
                    'policy_generation', p_policy_generation,
                    'evidence_ref', p_evidence_ref::text,
                    'entity_ref', p_entity_id::text,
                    'business_unit_ref', v_business_unit_ref,
                    'blob_ref', v_blob.blob_ref::text,
                    'byte_size', p_byte_size,
                    'sha256', encode(p_plaintext_sha256, 'hex'),
                    'outcome', 'SUCCEEDED'
                )::jsonb
            );
            INSERT INTO internal_read.evidence_read_receipt (
                operation_id, audit_event_id, principal_ref, verified_san,
                policy_generation, evidence_ref, entity_id, business_unit_id,
                blob_ref, byte_size, plaintext_sha256
            ) VALUES (
                p_operation_id, v_audit, p_principal_ref, p_verified_san,
                p_policy_generation, p_evidence_ref, p_entity_id, p_business_unit_id,
                v_blob.blob_ref, p_byte_size, p_plaintext_sha256
            );
            RETURN v_audit;
        END
        $function$;
        """
    )

    _grant_exact_surface()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1 FROM public.audit_event
                 WHERE action = 'internal.read.evidence.content'
            )
            OR EXISTS (SELECT 1 FROM public.encrypted_object_identity)
            OR EXISTS (SELECT 1 FROM public.encrypted_blob_version)
            OR EXISTS (SELECT 1 FROM public.evidence_object)
            OR EXISTS (SELECT 1 FROM public.candidate)
            OR EXISTS (SELECT 1 FROM public.reconciliation_snapshot)
            OR EXISTS (SELECT 1 FROM internal_read.evidence_read_receipt)
            """
        )
    ).scalar_one():
        raise RuntimeError("R1 internal-read data prevents destructive downgrade")
    op.execute(
        """
        REVOKE ALL ON ALL FUNCTIONS IN SCHEMA internal_read
            FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker;
        REVOKE ALL ON ALL TABLES IN SCHEMA internal_read
            FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker;
        DO $downgrade_acl$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_app') THEN
                EXECUTE 'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA internal_read FROM ledgerbridge_app';
                EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA internal_read FROM ledgerbridge_app';
            END IF;
        END
        $downgrade_acl$;
        DROP FUNCTION IF EXISTS internal_read.append_internal_evidence_read_audit(
            uuid, varchar(200), varchar(200), varchar(128), uuid, uuid, uuid, uuid, bigint, bytea
        );
        DROP TRIGGER IF EXISTS evidence_read_receipt_append_only
            ON internal_read.evidence_read_receipt;
        DROP TRIGGER IF EXISTS evidence_read_receipt_audit_binding
            ON internal_read.evidence_read_receipt;
        DROP FUNCTION IF EXISTS public.r1_validate_evidence_read_receipt();
        DROP FUNCTION IF EXISTS public.r1_evidence_read_receipt_append_only();
        DROP TABLE IF EXISTS internal_read.evidence_read_receipt;
        DROP FUNCTION IF EXISTS internal_read.resolve_active_evidence_blob(uuid);
        DROP FUNCTION IF EXISTS internal_read.get_ledger_summary_as_of(
            uuid, uuid, date, date, bigint, bytea
        );
        DROP FUNCTION IF EXISTS internal_read.get_reconciliation_as_of(
            uuid, uuid, date, bigint, bytea
        );
        DROP FUNCTION IF EXISTS internal_read.list_candidates_as_of(
            uuid, uuid, varchar(16), bigint, bytea, timestamptz, uuid, integer
        );
        DROP FUNCTION IF EXISTS internal_read.current_audit_horizon();
        DROP VIEW IF EXISTS internal_read.ledger_posted_total_v;
        DROP FUNCTION IF EXISTS public.r1_assert_posted_total_integrity();
        DROP VIEW IF EXISTS internal_read.reconciliation_suspense_v;
        DROP VIEW IF EXISTS internal_read.reconciliation_proposal_v;
        DROP VIEW IF EXISTS internal_read.reconciliation_blocker_v;
        DROP VIEW IF EXISTS internal_read.reconciliation_current_v;
        DROP VIEW IF EXISTS internal_read.evidence_metadata_v;
        DROP VIEW IF EXISTS internal_read.candidate_evidence_v;
        DROP VIEW IF EXISTS internal_read.candidate_current_v;
        DROP SCHEMA IF EXISTS internal_read;
        """
    )
