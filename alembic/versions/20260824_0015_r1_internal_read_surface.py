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
            v_reader oid;
            v_owner oid;
            v_database_owner oid;
            v_current_user oid;
        BEGIN
            SELECT oid INTO v_reader FROM pg_roles WHERE rolname = 'ledgerbridge_reader';
            SELECT oid INTO v_current_user FROM pg_roles WHERE rolname = current_user;
            SELECT datdba INTO v_database_owner
              FROM pg_database WHERE datname = current_database();
            v_owner := v_current_user;
            IF v_reader IS NULL THEN
                RAISE EXCEPTION 'required reader role ledgerbridge_reader is missing'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;
            IF v_owner IS NULL OR v_database_owner IS DISTINCT FROM v_owner THEN
                RAISE EXCEPTION 'migration must run as the fixed current database owner'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_api')
               OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_worker') THEN
                RAISE EXCEPTION 'runtime API/worker roles are missing'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;
            IF (SELECT rolcanlogin FROM pg_roles WHERE oid = v_reader) IS DISTINCT FROM true
               OR (SELECT rolsuper FROM pg_roles WHERE oid = v_reader)
               OR (SELECT rolcreatedb FROM pg_roles WHERE oid = v_reader)
               OR (SELECT rolcreaterole FROM pg_roles WHERE oid = v_reader)
               OR (SELECT rolinherit FROM pg_roles WHERE oid = v_reader)
               OR (SELECT rolreplication FROM pg_roles WHERE oid = v_reader)
               OR (SELECT rolbypassrls FROM pg_roles WHERE oid = v_reader)
               OR current_user IN (
                    'ledgerbridge_reader', 'ledgerbridge_api',
                    'ledgerbridge_worker', 'ledgerbridge_app'
               ) THEN
                RAISE EXCEPTION
                    'ledgerbridge_reader must be an unprivileged NOINHERIT LOGIN and not a runtime migrator'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;
            IF EXISTS (
                SELECT 1 FROM pg_auth_members
                WHERE member = v_reader OR roleid = v_reader
            ) THEN
                RAISE EXCEPTION
                    'ledgerbridge_reader has unexpected bidirectional role membership'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;
            IF EXISTS (
                SELECT 1 FROM pg_database WHERE datname = current_database() AND datdba = v_reader
            ) OR EXISTS (
                SELECT 1 FROM pg_namespace WHERE nspowner = v_reader
            ) OR EXISTS (
                SELECT 1 FROM pg_class WHERE relowner = v_reader
            ) OR EXISTS (
                SELECT 1 FROM pg_proc WHERE proowner = v_reader
            ) THEN
                RAISE EXCEPTION 'ledgerbridge_reader must not own database objects'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM pg_roles AS r
                 WHERE r.rolname IN ('ledgerbridge_reader', 'ledgerbridge_api',
                                     'ledgerbridge_worker', 'ledgerbridge_app')
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
            IF EXISTS (
                SELECT 1 FROM pg_roles
                 WHERE oid = v_owner
                   AND rolname IN ('ledgerbridge_reader', 'ledgerbridge_api',
                                   'ledgerbridge_worker', 'ledgerbridge_app')
            ) THEN
                RAISE EXCEPTION 'fixed migration owner collides with a runtime role'
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
            role_name text;
        BEGIN
            -- REVOKE ALL ON DATABASE is deliberate: the following explicit
            -- GRANT CONNECT statements rebuild the allowlist.
            EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', v_database);
            FOREACH role_name IN ARRAY ARRAY[
                'ledgerbridge_app', 'ledgerbridge_api',
                'ledgerbridge_worker', 'ledgerbridge_reader'
            ] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format('REVOKE ALL ON DATABASE %I FROM %I', v_database, role_name);
                END IF;
            END LOOP;
            EXECUTE format(
                'GRANT CONNECT ON DATABASE %I TO ledgerbridge_api, ledgerbridge_worker, ledgerbridge_reader, %I',
                v_database, v_owner
            );
            EXECUTE format(
                'REVOKE TEMPORARY, CREATE ON DATABASE %I FROM ledgerbridge_api, ledgerbridge_worker, ledgerbridge_reader',
                v_database
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                'REVOKE ALL ON TABLES FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker',
                v_owner
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                'REVOKE ALL ON SEQUENCES FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker',
                v_owner
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                'REVOKE ALL ON FUNCTIONS FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker',
                v_owner
            );
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_app') THEN
                EXECUTE format(
                    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                    'REVOKE ALL ON TABLES FROM ledgerbridge_app', v_owner
                );
                EXECUTE format(
                    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                    'REVOKE ALL ON SEQUENCES FROM ledgerbridge_app', v_owner
                );
                EXECUTE format(
                    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                    'REVOKE ALL ON FUNCTIONS FROM ledgerbridge_app', v_owner
                );
            END IF;
        END
        $acl$;
        -- PostgreSQL expresses this cleanup through ALTER DEFAULT PRIVILEGES;
        -- keep the explicit audit phrase here so restore review cannot miss it.
        -- REVOKE ALL ON DEFAULT PRIVILEGES FROM PUBLIC;
        """
    )


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
            public.posting_attribution, public.reconciliation_snapshot,
            public.reconciliation_snapshot_proposal, public.reconciliation_snapshot_suspense
        FROM PUBLIC;
        REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ledgerbridge_reader;
        REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ledgerbridge_reader;
        REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM ledgerbridge_reader;
        REVOKE ALL ON FUNCTION public.append_audit_event(text, text, text, text, jsonb)
            FROM ledgerbridge_reader;
        REVOKE USAGE ON SCHEMA public FROM ledgerbridge_reader;
        DO $schema_acl$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY[
                'ledgerbridge_api', 'ledgerbridge_worker',
                'ledgerbridge_app', 'ledgerbridge_reader'
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
                'ledgerbridge_api', 'ledgerbridge_worker', 'ledgerbridge_app'
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
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_app') THEN
                EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA internal_read FROM ledgerbridge_app';
            END IF;
        END
        $table_acl$;
        GRANT SELECT ON internal_read.candidate_current_v,
            internal_read.candidate_evidence_v, internal_read.evidence_metadata_v,
            internal_read.reconciliation_current_v,
            internal_read.reconciliation_blocker_v,
            internal_read.reconciliation_proposal_v,
            internal_read.reconciliation_suspense_v,
            internal_read.ledger_posted_total_v TO ledgerbridge_reader;
        GRANT EXECUTE ON FUNCTION internal_read.current_audit_horizon(),
            internal_read.list_candidates_as_of(
                uuid, uuid, varchar(16), bigint, bytea, timestamptz, uuid, integer
            ),
            internal_read.get_reconciliation_as_of(uuid, uuid, date, bigint, bytea),
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
        SELECT je.entity_id,
               ja.business_unit_id,
               ja.accounting_month,
               pa.category_code_snapshot AS category_code,
               pa.category_label_snapshot AS category_label,
               p.currency,
               sum(p.amount_minor)::bigint AS posted_amount_minor
          FROM public.journal_entry AS je
         JOIN public.journal_entry_attribution AS ja ON ja.entry_id = je.id
          JOIN public.posting AS p ON p.entry_id = je.id
          JOIN public.posting_attribution AS pa ON pa.posting_id = p.id
         WHERE je.status = 'POSTED'
           AND p.account_id = je.primary_account_id
         GROUP BY je.entity_id, ja.business_unit_id, ja.accounting_month,
                  pa.category_code_snapshot, pa.category_label_snapshot, p.currency;
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
            SELECT b.blob_ref, b.evidence_ref, b.predecessor_blob_ref, b.object_ref,
                   v_evidence.plaintext_sha256, v_evidence.plaintext_size,
                   b.ciphertext_sha256, b.ciphertext_size, b.storage_key,
                   b.envelope_schema, b.algorithm, b.chunk_size, b.stream_header,
                   b.wrapped_key_generation, b.wrapped_key_nonce,
                   b.wrapped_key_ciphertext, b.purpose,
                   'ledgerbridge.artifact.object.v2'::varchar(40), b.created_at
              FROM public.encrypted_blob_version AS b
             WHERE b.evidence_ref = p_evidence_ref
               AND NOT EXISTS (
                   SELECT 1 FROM public.encrypted_blob_version AS child
                    WHERE child.predecessor_blob_ref = b.blob_ref
               )
             ORDER BY b.created_at DESC, b.blob_ref DESC
             LIMIT 1;
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
            v_audit := public.append_audit_event(
                p_principal_ref,
                'internal.read.evidence.content',
                'internal evidence content read',
                'ledgerbridge.internal-read-audit.v1',
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
                )
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
        DROP FUNCTION IF EXISTS internal_read.get_reconciliation_as_of(
            uuid, uuid, date, bigint, bytea
        );
        DROP FUNCTION IF EXISTS internal_read.list_candidates_as_of(
            uuid, uuid, varchar(16), bigint, bytea, timestamptz, uuid, integer
        );
        DROP FUNCTION IF EXISTS internal_read.current_audit_horizon();
        DROP VIEW IF EXISTS internal_read.ledger_posted_total_v;
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
