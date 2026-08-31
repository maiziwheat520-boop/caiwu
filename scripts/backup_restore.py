# ruff: noqa: E501

"""Create encrypted LedgerBridge backups and rehearse isolated restores on Hermes."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import quote, unquote, urlsplit, urlunsplit

BACKUP_FORMAT_V1 = "ledgerbridge-encrypted-backup-v1"
BACKUP_FORMAT_V2 = "ledgerbridge-encrypted-backup-v2"
BACKUP_FORMAT_V3 = "ledgerbridge-encrypted-backup-v3"
BACKUP_FORMAT = BACKUP_FORMAT_V3
SUPPORTED_BACKUP_FORMATS = frozenset({BACKUP_FORMAT_V1, BACKUP_FORMAT_V2, BACKUP_FORMAT_V3})
RESTORE_REPORT_FORMAT = "ledgerbridge-restore-rehearsal-v3"
POSTGRES_IMAGE = (
    "postgres:15-alpine@sha256:fe0737ba566a2c5b2a28f34433c0a423261900ec17b9bf7ad115e1aae7e57f1b"
)
REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
FINGERPRINT_PATTERN = re.compile(r"[0-9A-F]{40,64}")
SUFFIX_PATTERN = re.compile(r"[0-9a-f]{8}")
PAYLOAD_COMPONENTS = (
    "database.dump",
    "roles.sql",
    "artifacts.tar",
    "deployment-tree.tar",
    "metadata.json",
)
TAR_NORMALIZATION = (
    "--sort=name",
    "--format=posix",
    "--pax-option=delete=atime,delete=ctime",
    "--mtime=@0",
    "--owner=0",
    "--group=0",
    "--numeric-owner",
)
DATABASE_METADATA_SQL = """
SELECT json_build_object(
    'database_name', current_database(),
    'database_owner', (
        SELECT pg_get_userbyid(datdba)
        FROM pg_database
        WHERE datname = current_database()
    ),
    'alembic_version', (SELECT version_num FROM alembic_version),
    'data_checksums', current_setting('data_checksums'),
    'role_grant_count', (
        SELECT count(*)
        FROM information_schema.role_table_grants
        WHERE grantee = 'ledgerbridge_app'
          AND table_schema = 'public'
          AND table_name IN (
              'entity', 'account', 'journal_entry', 'posting', 'audit_event',
              'raw_artifact', 'source_record', 'import_job',
              'ingest_channel', 'source_system'
          )
    ),
    'runtime_role_valid', (
        SELECT rolcanlogin
            AND NOT rolsuper
            AND NOT rolcreatedb
            AND NOT rolcreaterole
            AND NOT rolreplication
            AND NOT rolbypassrls
            AND NOT EXISTS (
                SELECT 1 FROM pg_auth_members WHERE member = pg_roles.oid
            )
            AND NOT EXISTS (
                SELECT 1
                FROM pg_database
                WHERE datname = current_database() AND datdba = pg_roles.oid
            )
        FROM pg_roles
        WHERE rolname = 'ledgerbridge_app'
    ),
    'audit_select_only', (
        has_table_privilege('ledgerbridge_app', 'audit_event', 'SELECT')
        AND NOT has_table_privilege('ledgerbridge_app', 'audit_event', 'INSERT')
        AND NOT has_table_privilege('ledgerbridge_app', 'audit_event', 'UPDATE')
        AND NOT has_table_privilege('ledgerbridge_app', 'audit_event', 'DELETE')
    ),
    'schema_create_denied', NOT has_schema_privilege(
        'ledgerbridge_app', 'public', 'CREATE'
    ),
    'function_count', (
        SELECT count(*)
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname IN (
              'account_block_protected_dimension_change',
              'append_audit_event',
              'audit_event_block_mutation',
              'journal_entry_assert_posted_complete',
              'journal_entry_block_posted_mutation',
              'journal_entry_validate_relationships',
              'posting_assert_balanced',
              'posting_block_posted_mutation',
              'posting_enforce_entity'
          )
    ),
    'trigger_count', (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal),
    'row_counts', json_build_object(
        'entity', (SELECT count(*) FROM entity),
        'account', (SELECT count(*) FROM account),
        'journal_entry', (SELECT count(*) FROM journal_entry),
        'posting', (SELECT count(*) FROM posting),
        'audit_event', (SELECT count(*) FROM audit_event)
    )
)::text;
""".strip()
DATABASE_SECURITY_SQL = """
SELECT json_build_object(
    'database_temp_denied', NOT has_database_privilege(
        'ledgerbridge_app', current_database(), 'TEMPORARY'
    ),
    'security_functions', COALESCE((
        SELECT json_agg(
            json_build_object(
                'name', p.proname,
                'identity_arguments', pg_get_function_identity_arguments(p.oid),
                'proconfig', COALESCE(to_json(p.proconfig), '[]'::json)
            ) ORDER BY p.proname, pg_get_function_identity_arguments(p.oid)
        )
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND NOT EXISTS (
              SELECT 1
              FROM pg_depend AS dependency
              WHERE dependency.classid = 'pg_proc'::regclass
                AND dependency.objid = p.oid
                AND dependency.deptype = 'e'
          )
    ), '[]'::json),
    'public_triggers', COALESCE((
        SELECT json_agg(
            json_build_object(
                'table', table_class.relname,
                'name', trigger.tgname,
                'enabled', trigger.tgenabled
            ) ORDER BY table_class.relname, trigger.tgname
        )
        FROM pg_trigger AS trigger
        JOIN pg_class AS table_class ON table_class.oid = trigger.tgrelid
        JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace
        WHERE namespace.nspname = 'public' AND NOT trigger.tgisinternal
    ), '[]'::json),
    'table_grants', COALESCE((
        SELECT json_agg(
            json_build_object(
                'table', table_name,
                'privilege', privilege_type,
                'grantable', is_grantable
            ) ORDER BY table_name, privilege_type
        )
        FROM information_schema.role_table_grants
        WHERE table_schema = 'public' AND grantee = 'ledgerbridge_app'
    ), '[]'::json),
    'column_grants', COALESCE((
        SELECT json_agg(
            json_build_object(
                'table', table_name,
                'column', column_name,
                'privilege', privilege_type,
                'grantable', is_grantable
            ) ORDER BY table_name, column_name, privilege_type
        )
        FROM information_schema.role_column_grants
        WHERE table_schema = 'public'
          AND grantee = 'ledgerbridge_app'
          AND NOT has_table_privilege(
              grantee,
              format('%I.%I', table_schema, table_name),
              privilege_type
          )
    ), '[]'::json),
    'sequence_grants', COALESCE((
        SELECT json_agg(
            json_build_object(
                'sequence', object_name,
                'privilege', privilege_type,
                'grantable', is_grantable
            ) ORDER BY object_name, privilege_type
        )
        FROM information_schema.role_usage_grants
        WHERE object_schema = 'public'
          AND object_type = 'SEQUENCE'
          AND grantee = 'ledgerbridge_app'
    ), '[]'::json),
    'function_grants', COALESCE((
        SELECT json_agg(
            json_build_object(
                'function', routine_name,
                'grantee', grantee,
                'privilege', privilege_type,
                'grantable', is_grantable
            ) ORDER BY routine_name, grantee, privilege_type
        )
        FROM information_schema.routine_privileges
        WHERE specific_schema = 'public'
          AND grantee IN ('ledgerbridge_app', 'PUBLIC')
    ), '[]'::json)
)::text;
""".strip()
ARTIFACT_MANIFEST_SQL = """
SELECT json_build_object(
    'artifact_count', (SELECT count(*) FROM public.raw_artifact),
    'artifact_manifest_sha256', encode(
        digest(
            COALESCE((
                SELECT string_agg(
                    encode(sha256, 'hex') || ':' || byte_size::text || ':' || storage_key,
                    E'\\n' ORDER BY encode(sha256, 'hex')
                )
                FROM public.raw_artifact
            ), ''),
            'sha256'
        ),
        'hex'
    )
)::text;
""".strip()
R1_ARTIFACT_MANIFEST_SQL = """
WITH artifacts AS (
    SELECT sha256, byte_size, storage_key
      FROM public.raw_artifact
    UNION
    SELECT ciphertext_sha256 AS sha256,
           ciphertext_size AS byte_size,
           storage_key
      FROM public.encrypted_blob_version
)
SELECT json_build_object(
    'artifact_count', (SELECT count(*) FROM artifacts),
    'artifact_manifest_sha256', encode(
        digest(
            COALESCE((
                SELECT string_agg(
                    encode(sha256, 'hex') || ':' || byte_size::text || ':' || storage_key,
                    E'\\n' ORDER BY encode(sha256, 'hex')
                )
                FROM artifacts
            ), ''),
            'sha256'
        ),
        'hex'
    )
)::text;
""".strip()

# R1 facts are deliberately kept out of the legacy role-grant baseline above.
# The compatibility role still owns the Phase 1/2/3 runtime grants, but it must
# not acquire a direct grant on any R1 fact table.  These fixed object lists are
# also used by the restore verifier so a backup cannot silently omit a new R1
# view, function, trigger, or effective privilege observation.
R1_RUNTIME_ROLES = ("ledgerbridge_app", "ledgerbridge_api", "ledgerbridge_worker")
R1_ROLES = (*R1_RUNTIME_ROLES, "ledgerbridge_reader")
R1_OPTIONAL_ROLES = ("ledgerbridge_backup",)
R1_CONTROLLED_ROLES = (*R1_ROLES, *R1_OPTIONAL_ROLES)
R1_PUBLIC_TABLES = (
    "business_unit",
    "reporting_category",
    "evidence_object",
    "encrypted_blob_version",
    "encrypted_object_identity",
    "candidate",
    "candidate_source",
    "candidate_revision",
    "candidate_blocker",
    "candidate_event",
    "candidate_field_change",
    "candidate_conflict_resolution",
    "candidate_evidence",
    "journal_entry_attribution",
    "posting_attribution",
    "reconciliation_leg",
    "reconciliation_snapshot",
    "reconciliation_snapshot_blocker",
    "reconciliation_snapshot_proposal",
    "reconciliation_snapshot_suspense",
)
R1_INTERNAL_READ_VIEWS = (
    "candidate_current_v",
    "candidate_evidence_v",
    "evidence_metadata_v",
    "reconciliation_current_v",
    "reconciliation_blocker_v",
    "reconciliation_proposal_v",
    "reconciliation_suspense_v",
    "ledger_posted_total_v",
)
R1_INTERNAL_READ_FUNCTIONS = (
    "current_audit_horizon",
    "get_accounting_dimensions",
    "list_candidates_as_of",
    "get_reconciliation_as_of",
    "resolve_active_evidence_blob",
    "get_ledger_summary_as_of",
    "append_internal_evidence_read_audit",
)
R1_ACCOUNTING_DIMENSIONS_REVISION = "20260830_0022"
# These are the exact strings emitted by PostgreSQL's
# pg_get_function_identity_arguments().  Function identity does not include
# varchar typmods, so the allowlist intentionally uses "character varying"
# rather than the migration's varchar(N) declarations.
R1_INTERNAL_READ_FUNCTION_SIGNATURES = {
    "current_audit_horizon": "",
    "get_accounting_dimensions": (
        "p_entity_id uuid, p_business_unit_ids uuid[], p_business_unit_refs character varying[]"
    ),
    "list_candidates_as_of": (
        "p_entity_id uuid, p_business_unit_id uuid, p_status character varying, "
        "p_audit_horizon_sequence bigint, p_audit_horizon_hash bytea, "
        "p_last_created_at timestamp with time zone, p_last_candidate_id uuid, p_limit integer"
    ),
    "get_reconciliation_as_of": (
        "p_entity_id uuid, p_business_unit_id uuid, p_accounting_month date, "
        "p_audit_horizon_sequence bigint, p_audit_horizon_hash bytea"
    ),
    "resolve_active_evidence_blob": "p_evidence_ref uuid",
    "get_ledger_summary_as_of": (
        "p_entity_id uuid, p_business_unit_id uuid, p_from_month date, p_to_month date, "
        "p_audit_horizon_sequence bigint, p_audit_horizon_hash bytea"
    ),
    "append_internal_evidence_read_audit": (
        "p_operation_id uuid, p_principal_ref character varying, "
        "p_verified_san character varying, p_policy_generation character varying, "
        "p_evidence_ref uuid, p_entity_id uuid, p_business_unit_id uuid, "
        "p_blob_ref uuid, p_byte_size bigint, p_plaintext_sha256 bytea"
    ),
}
R1_SECURITY_REVISION = "20260824_0015"
R1_REQUIRED_CONSTRAINTS = frozenset(
    {
        "uq_encrypted_blob_evidence_predecessor",
        "pk_evidence_read_receipt",
        "uq_evidence_read_receipt_audit",
        "ck_evidence_read_receipt_principal",
        "ck_evidence_read_receipt_san",
        "ck_evidence_read_receipt_policy",
        "ck_evidence_read_receipt_size",
        "ck_evidence_read_receipt_sha",
        "fk_evidence_read_receipt_audit",
        "fk_evidence_read_receipt_evidence",
        "fk_evidence_read_receipt_blob",
    }
)
R1_REQUIRED_TRIGGERS = frozenset(
    {
        "r1_encrypted_blob_lineage",
        "r1_candidate_event_audit",
        "r1_candidate_history",
        "r1_candidate_revision_history",
        "r1_reconciliation_leg_exactly_one_primary",
        "r1_snapshot_audit_binding",
        "evidence_read_receipt_audit_binding",
        "evidence_read_receipt_append_only",
    }
)

COUNTERPARTY_SECURITY_REVISION = "20260830_0020"
COUNTERPARTY_TABLES = (
    "counterparty_identity",
    "counterparty_classification",
    "candidate_counterparty",
)
COUNTERPARTY_PROTECTED_TABLES = (*COUNTERPARTY_TABLES, "candidate_evidence_link")
COUNTERPARTY_FUNCTION_SIGNATURES = {
    ("public", "r1_counterparty_append_only"): "",
    ("public", "r1_validate_counterparty_identity"): "",
    ("public", "r1_validate_counterparty_classification"): "",
    ("public", "r1_validate_candidate_counterparty"): "",
    ("public", "r1_candidate_evidence_link_append_only"): "",
    ("public", "r1_validate_candidate_evidence_link"): "",
    ("internal_read", "list_candidate_counterparty_facts"): (
        "p_entity_id uuid, p_business_unit_id uuid, p_candidate_ids uuid[], "
        "p_audit_horizon_sequence bigint, p_audit_horizon_hash bytea"
    ),
    ("internal_read", "list_candidate_evidence_satisfactions"): (
        "p_entity_id uuid, p_business_unit_id uuid, p_candidate_ids uuid[], "
        "p_audit_horizon_sequence bigint, p_audit_horizon_hash bytea"
    ),
}
COUNTERPARTY_FUNCTION_RESULTS = {
    ("public", name): "trigger"
    for name in (
        "r1_counterparty_append_only",
        "r1_validate_counterparty_identity",
        "r1_validate_counterparty_classification",
        "r1_validate_candidate_counterparty",
        "r1_candidate_evidence_link_append_only",
        "r1_validate_candidate_evidence_link",
    )
} | {
    ("internal_read", "list_candidate_counterparty_facts"): (
        "TABLE(candidate_id uuid, counterparty_ref character varying, "
        "counterparty_class character varying)"
    ),
    ("internal_read", "list_candidate_evidence_satisfactions"): (
        "TABLE(candidate_id uuid, risk_code character varying)"
    ),
}
COUNTERPARTY_TRIGGER_CONTRACT = {
    "r1_counterparty_identity_append_only_trigger": (
        "counterparty_identity",
        False,
        27,
        "r1_counterparty_append_only",
    ),
    "r1_counterparty_classification_append_only_trigger": (
        "counterparty_classification",
        False,
        27,
        "r1_counterparty_append_only",
    ),
    "r1_candidate_counterparty_append_only_trigger": (
        "candidate_counterparty",
        False,
        27,
        "r1_counterparty_append_only",
    ),
    "r1_validate_counterparty_identity_trigger": (
        "counterparty_identity",
        False,
        7,
        "r1_validate_counterparty_identity",
    ),
    "r1_validate_counterparty_classification_trigger": (
        "counterparty_classification",
        False,
        7,
        "r1_validate_counterparty_classification",
    ),
    "r1_validate_candidate_counterparty_trigger": (
        "candidate_counterparty",
        False,
        7,
        "r1_validate_candidate_counterparty",
    ),
    "r1_candidate_evidence_link_append_only_trigger": (
        "candidate_evidence_link",
        False,
        27,
        "r1_candidate_evidence_link_append_only",
    ),
    "r1_validate_candidate_evidence_link_trigger": (
        "candidate_evidence_link",
        False,
        7,
        "r1_validate_candidate_evidence_link",
    ),
}
COUNTERPARTY_CONSTRAINT_CONTRACT = {
    "fk_candidate_counterparty_audit_event_id_audit_event": (
        "candidate_counterparty",
        "f",
        "FOREIGN KEY (audit_event_id) REFERENCES audit_event(id) ON DELETE RESTRICT",
    ),
    "fk_candidate_counterparty_candidate_id_candidate": (
        "candidate_counterparty",
        "f",
        "FOREIGN KEY (candidate_id) REFERENCES candidate(id) ON DELETE RESTRICT",
    ),
    "fk_candidate_counterparty_entity_id_counterparty_identity": (
        "candidate_counterparty",
        "f",
        "FOREIGN KEY (entity_id, counterparty_ref) REFERENCES "
        "counterparty_identity(entity_id, counterparty_ref) ON DELETE RESTRICT",
    ),
    "pk_candidate_counterparty": ("candidate_counterparty", "p", "PRIMARY KEY (candidate_id)"),
    "uq_candidate_counterparty_audit": (
        "candidate_counterparty",
        "u",
        "UNIQUE (audit_event_id)",
    ),
    "ck_candidate_evidence_link_candidate_evidence_link_rela_edfc": (
        "candidate_evidence_link",
        "c",
        "CHECK (((relation)::text = ANY ((ARRAY['SAME_ECONOMIC_TRANSACTION'::character "
        "varying, 'PARTIAL_REFUND'::character varying])::text[])))",
    ),
    "ck_candidate_evidence_link_candidate_evidence_link_risk_allowed": (
        "candidate_evidence_link",
        "c",
        "CHECK (((risk_code)::text = ANY ((ARRAY['HOTEL_PAYOUT_STATEMENT_REQUIRED'::character "
        "varying, 'REVERSAL_MATCH_REQUIRED'::character varying])::text[])))",
    ),
    "ck_counterparty_classification_counterparty_classificat_056b": (
        "counterparty_classification",
        "c",
        "CHECK ((classification_revision > 0))",
    ),
    "ck_counterparty_classification_counterparty_classificat_f4f6": (
        "counterparty_classification",
        "c",
        "CHECK (((counterparty_class)::text = ANY ((ARRAY['self_managed'::character varying, "
        "'related_party'::character varying, 'known_business'::character varying, "
        "'unknown'::character varying])::text[])))",
    ),
    "fk_counterparty_classification_audit_event_id_audit_event": (
        "counterparty_classification",
        "f",
        "FOREIGN KEY (audit_event_id) REFERENCES audit_event(id) ON DELETE RESTRICT",
    ),
    "fk_counterparty_classification_entity_id_counterparty_identity": (
        "counterparty_classification",
        "f",
        "FOREIGN KEY (entity_id, counterparty_ref) REFERENCES "
        "counterparty_identity(entity_id, counterparty_ref) ON DELETE RESTRICT",
    ),
    "pk_counterparty_classification": (
        "counterparty_classification",
        "p",
        "PRIMARY KEY (entity_id, counterparty_ref, classification_revision)",
    ),
    "uq_counterparty_classification_audit": (
        "counterparty_classification",
        "u",
        "UNIQUE (audit_event_id)",
    ),
    "ck_counterparty_identity_counterparty_identity_ref_format": (
        "counterparty_identity",
        "c",
        "CHECK (((counterparty_ref)::text ~ '^cp_[a-z0-9_]{1,96}$'::text))",
    ),
    "fk_counterparty_identity_audit_event_id_audit_event": (
        "counterparty_identity",
        "f",
        "FOREIGN KEY (audit_event_id) REFERENCES audit_event(id) ON DELETE RESTRICT",
    ),
    "fk_counterparty_identity_entity_id_entity": (
        "counterparty_identity",
        "f",
        "FOREIGN KEY (entity_id) REFERENCES entity(id) ON DELETE RESTRICT",
    ),
    "pk_counterparty_identity": (
        "counterparty_identity",
        "p",
        "PRIMARY KEY (entity_id, counterparty_ref)",
    ),
    "uq_counterparty_identity_audit": (
        "counterparty_identity",
        "u",
        "UNIQUE (audit_event_id)",
    ),
}

_COUNTERPARTY_NEW_TABLES_SQL = ", ".join(f"'{name}'" for name in COUNTERPARTY_TABLES)
_COUNTERPARTY_TABLES_SQL = ", ".join(f"'{name}'" for name in COUNTERPARTY_PROTECTED_TABLES)
_COUNTERPARTY_ROLES_SQL = ", ".join(f"('{name}'::name)" for name in R1_CONTROLLED_ROLES)
_COUNTERPARTY_FUNCTIONS_SQL = ", ".join(
    f"('{schema}', '{name}', '{args}')"
    for (schema, name), args in COUNTERPARTY_FUNCTION_SIGNATURES.items()
)
_COUNTERPARTY_CONSTRAINTS_SQL = ", ".join(f"'{name}'" for name in COUNTERPARTY_CONSTRAINT_CONTRACT)
COUNTERPARTY_SECURITY_SQL = (
    ""  # nosec B608 - replacements use only fixed allowlists.
    """
WITH expected_roles(role_name) AS (VALUES __R1_ROLE_SQL__),
present_roles(role_name) AS (
 SELECT e.role_name FROM expected_roles e JOIN pg_roles r ON r.rolname=e.role_name
), expected_functions(schema_name,function_name,identity_arguments) AS (
 VALUES __COUNTERPARTY_FUNCTIONS_SQL__
), observed_tables AS (
 SELECT c.relname table_name,pg_get_userbyid(c.relowner) owner,c.relkind kind
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' AND c.relname IN (__COUNTERPARTY_TABLES_SQL__)
), observed_functions AS (
 SELECT n.nspname schema_name,p.proname function_name,
  pg_get_function_identity_arguments(p.oid) identity_arguments,
  pg_get_function_result(p.oid) result,
  pg_get_userbyid(p.proowner) owner,p.prosecdef security_definer,
  COALESCE(to_json(p.proconfig),'[]'::json) proconfig,p.oid,p.proacl acl
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE EXISTS (SELECT 1 FROM expected_functions e
  WHERE e.schema_name=n.nspname AND e.function_name=p.proname)
), observed_triggers AS (
 SELECT c.relname table_name,t.tgname trigger_name,t.tgenabled enabled,
  t.tgconstraint<>0 is_constraint,t.tgtype trigger_type,
  fn.proname function_name,fnn.nspname function_schema
 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
 JOIN pg_namespace n ON n.oid=c.relnamespace
 JOIN pg_proc fn ON fn.oid=t.tgfoid
 JOIN pg_namespace fnn ON fnn.oid=fn.pronamespace
 WHERE n.nspname='public' AND NOT t.tgisinternal
  AND c.relname IN (__COUNTERPARTY_TABLES_SQL__)
), observed_constraints AS (
 SELECT c.relname table_name,con.conname constraint_name,con.contype constraint_type,
  con.convalidated is_validated,con.condeferrable is_deferrable,
  con.condeferred is_initially_deferred,pg_get_constraintdef(con.oid,false) definition
 FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
 JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' AND (
  c.relname IN (__COUNTERPARTY_NEW_TABLES_SQL__)
  OR (c.relname='candidate_evidence_link'
      AND con.conname IN (__COUNTERPARTY_CONSTRAINTS_SQL__))
 )
), table_acls AS (
 SELECT c.relname table_name,
  CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END grantee,
  a.privilege_type privilege,a.is_grantable grantable
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 CROSS JOIN LATERAL aclexplode(c.relacl) a
 WHERE n.nspname='public' AND c.relname IN (__COUNTERPARTY_TABLES_SQL__)
), function_acls AS (
 SELECT f.schema_name,f.function_name,f.identity_arguments,
  CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END grantee,
  a.privilege_type privilege,a.is_grantable grantable
 FROM observed_functions f
 CROSS JOIN LATERAL aclexplode(f.acl) a
), table_privileges AS (
 SELECT r.role_name role,o.object_name,
  has_table_privilege(r.role_name,format('public.%I',o.object_name),'SELECT') can_select,
  has_table_privilege(r.role_name,format('public.%I',o.object_name),'INSERT') can_insert,
  has_table_privilege(r.role_name,format('public.%I',o.object_name),'UPDATE') can_update,
  has_table_privilege(r.role_name,format('public.%I',o.object_name),'DELETE') can_delete,
  has_table_privilege(r.role_name,format('public.%I',o.object_name),'TRUNCATE') can_truncate,
  has_table_privilege(r.role_name,format('public.%I',o.object_name),'REFERENCES') can_reference,
  has_table_privilege(r.role_name,format('public.%I',o.object_name),'TRIGGER') can_trigger
 FROM present_roles r
 CROSS JOIN unnest(ARRAY[__COUNTERPARTY_TABLES_SQL__]::text[]) o(object_name)
), function_privileges AS (
 SELECT r.role_name role,f.schema_name,f.function_name,f.identity_arguments,
  has_function_privilege(r.role_name,f.oid,'EXECUTE') can_execute
 FROM present_roles r CROSS JOIN observed_functions f
)
SELECT json_build_object(
 'counterparty_row_counts',json_build_object(
  'counterparty_identity',(SELECT count(*) FROM public.counterparty_identity),
  'counterparty_classification',(SELECT count(*) FROM public.counterparty_classification),
  'candidate_counterparty',(SELECT count(*) FROM public.candidate_counterparty),
  'candidate_evidence_link',(SELECT count(*) FROM public.candidate_evidence_link)),
 'counterparty_tables',COALESCE((SELECT json_agg(json_build_object(
  'table',table_name,'owner',owner,'kind',kind) ORDER BY table_name)
  FROM observed_tables),'[]'::json),
 'counterparty_functions',COALESCE((SELECT json_agg(json_build_object(
  'schema',schema_name,'name',function_name,'identity_arguments',identity_arguments,
  'result',result,'owner',owner,'security_definer',security_definer,'proconfig',proconfig)
  ORDER BY schema_name,function_name,identity_arguments) FROM observed_functions),'[]'::json),
 'counterparty_triggers',COALESCE((SELECT json_agg(json_build_object(
  'table',table_name,'name',trigger_name,'enabled',enabled,'constraint',is_constraint,
  'trigger_type',trigger_type,'function_schema',function_schema,'function_name',function_name)
  ORDER BY table_name,trigger_name) FROM observed_triggers),'[]'::json),
 'counterparty_constraints',COALESCE((SELECT json_agg(json_build_object(
  'table',table_name,'name',constraint_name,'type',constraint_type,
  'validated',is_validated,'deferrable',is_deferrable,
  'initially_deferred',is_initially_deferred,'definition',definition)
  ORDER BY table_name,constraint_name)
  FROM observed_constraints),'[]'::json),
 'counterparty_table_acls',COALESCE((SELECT json_agg(json_build_object(
  'table',table_name,'grantee',grantee,'privilege',privilege,'grantable',grantable)
  ORDER BY table_name,grantee,privilege) FROM table_acls),'[]'::json),
 'counterparty_function_acls',COALESCE((SELECT json_agg(json_build_object(
  'schema',schema_name,'name',function_name,'identity_arguments',identity_arguments,
  'grantee',grantee,'privilege',privilege,'grantable',grantable)
  ORDER BY schema_name,function_name,identity_arguments,grantee,privilege)
  FROM function_acls),'[]'::json),
 'counterparty_effective_table_privileges',COALESCE((SELECT json_agg(json_build_object(
  'role',role,'table',object_name,'select',can_select,'insert',can_insert,
  'update',can_update,'delete',can_delete,'truncate',can_truncate,
  'references',can_reference,'trigger',can_trigger)
  ORDER BY role,object_name)
  FROM table_privileges),'[]'::json),
 'counterparty_effective_function_privileges',COALESCE((SELECT json_agg(json_build_object(
  'role',role,'schema',schema_name,'name',function_name,
  'identity_arguments',identity_arguments,'execute',can_execute)
  ORDER BY role,schema_name,function_name,identity_arguments) FROM function_privileges),'[]'::json)
)::text;
    """.replace("__R1_ROLE_SQL__", _COUNTERPARTY_ROLES_SQL)
    .replace("__COUNTERPARTY_NEW_TABLES_SQL__", _COUNTERPARTY_NEW_TABLES_SQL)
    .replace("__COUNTERPARTY_TABLES_SQL__", _COUNTERPARTY_TABLES_SQL)
    .replace("__COUNTERPARTY_FUNCTIONS_SQL__", _COUNTERPARTY_FUNCTIONS_SQL)
    .replace("__COUNTERPARTY_CONSTRAINTS_SQL__", _COUNTERPARTY_CONSTRAINTS_SQL)
    .strip()
)

BANK_STATEMENT_SECURITY_REVISION = "20260830_0021"
ACCOUNT_REGISTRY_SECURITY_REVISION = "20260830_0023"
BANK_STATEMENT_TABLES = (
    "managed_account",
    "managed_account_lifecycle",
    "bank_statement",
    "bank_statement_transaction",
    "bank_statement_observation",
    "bank_statement_review",
)
ACCOUNT_REGISTRY_TABLES = (
    "account_registry_operation",
    "managed_account_alias",
    "account_business_unit_assignment",
    "fact_business_unit_allocation_set",
    "fact_business_unit_allocation_item",
)
R1_CUTOVER_INVENTORY_TABLES = tuple(
    sorted(
        set(R1_PUBLIC_TABLES)
        | set(COUNTERPARTY_PROTECTED_TABLES)
        | set(BANK_STATEMENT_TABLES)
        | set(ACCOUNT_REGISTRY_TABLES)
    )
)
_R1_CUTOVER_ROW_COUNTS_SQL = ", ".join(
    f"'{table}', (SELECT count(*) FROM public.{table})" for table in R1_CUTOVER_INVENTORY_TABLES
)
R1_CUTOVER_INVENTORY_SQL = (
    ""  # nosec B608 - table names come only from fixed repository allowlists.
    "SELECT json_build_object("
    "'schema_revision', (SELECT version_num FROM public.alembic_version), "
    "'candidate_total', (SELECT count(*) FROM public.candidate), "
    "'latest_pending_candidates', (SELECT count(*) FROM public.candidate c "
    "JOIN LATERAL (SELECT status FROM public.candidate_revision cr "
    "WHERE cr.candidate_id=c.id ORDER BY cr.revision DESC LIMIT 1) latest ON true "
    "WHERE latest.status='PENDING'), "
    "'audit_events', (SELECT count(*) FROM public.audit_event), "
    f"'row_counts', json_build_object({_R1_CUTOVER_ROW_COUNTS_SQL})"
    ")::text;"
)
BANK_STATEMENT_FUNCTION_SIGNATURES = {
    ("public", "r1_bank_statement_append_only"): "",
    ("public", "r1_bank_statement_transaction_digest"): (
        "p_managed_account_ref uuid, p_occurred_at timestamp with time zone, "
        "p_amount_minor bigint, p_balance_minor bigint, p_currency text, "
        "p_counterparty_ref text, p_counterparty_name text, p_counterparty_account text, "
        "p_counterparty_institution text, p_transaction_serial text, p_transaction_name text"
    ),
    ("public", "r1_validate_bank_statement"): "",
    ("public", "r1_require_statement_backed_account"): "",
    ("public", "r1_validate_statement_facts"): "",
    ("public", "r1_require_transaction_observation"): "",
    ("internal_import", "import_bank_statement"): "p_request jsonb",
    ("internal_command", "review_bank_statement"): (
        "p_statement_ref uuid, p_operation_id uuid, p_assertion_jti uuid, "
        "p_actor_ref text, p_workload_principal_ref text, p_expected_revision integer, "
        "p_decision text, p_reason text"
    ),
    ("internal_read", "get_bank_statement_summary"): (
        "p_statement_ref uuid, p_entity_ref uuid, p_audit_horizon_sequence bigint, "
        "p_audit_horizon_hash bytea"
    ),
    ("internal_read", "list_bank_statement_transactions"): (
        "p_statement_ref uuid, p_entity_ref uuid, p_audit_horizon_sequence bigint, "
        "p_audit_horizon_hash bytea, p_after_row integer, p_limit integer"
    ),
}
BANK_STATEMENT_FUNCTION_RESULTS = {
    ("public", "r1_bank_statement_append_only"): "trigger",
    ("public", "r1_bank_statement_transaction_digest"): "bytea",
    ("public", "r1_validate_bank_statement"): "trigger",
    ("public", "r1_require_statement_backed_account"): "trigger",
    ("public", "r1_validate_statement_facts"): "trigger",
    ("public", "r1_require_transaction_observation"): "trigger",
    ("internal_import", "import_bank_statement"): "jsonb",
    ("internal_command", "review_bank_statement"): "jsonb",
    ("internal_read", "get_bank_statement_summary"): (
        "TABLE(statement_ref uuid, managed_account_ref uuid, evidence_ref uuid, "
        "period_start date, period_end date, transaction_count integer, "
        "review_status character varying, review_revision integer)"
    ),
    ("internal_read", "list_bank_statement_transactions"): (
        "TABLE(source_row_number integer, occurred_at timestamp with time zone, "
        "amount_minor bigint, balance_minor bigint, currency character varying, "
        "counterparty_ref character varying, counterparty_name character varying, "
        "counterparty_account_masked character varying, "
        "counterparty_institution character varying, transaction_serial character varying, "
        "transaction_name character varying)"
    ),
}
BANK_STATEMENT_SECURITY_DEFINER_FUNCTIONS = frozenset(
    {
        ("public", "r1_require_statement_backed_account"),
        ("public", "r1_validate_statement_facts"),
        ("public", "r1_require_transaction_observation"),
        ("internal_import", "import_bank_statement"),
        ("internal_command", "review_bank_statement"),
        ("internal_read", "get_bank_statement_summary"),
        ("internal_read", "list_bank_statement_transactions"),
    }
)
BANK_STATEMENT_TRIGGER_CONTRACT = {
    "managed_account_append_only": (
        "managed_account",
        False,
        27,
        False,
        False,
        "r1_bank_statement_append_only",
    ),
    "managed_account_lifecycle_append_only": (
        "managed_account_lifecycle",
        False,
        27,
        False,
        False,
        "r1_bank_statement_append_only",
    ),
    "bank_statement_append_only": (
        "bank_statement",
        False,
        27,
        False,
        False,
        "r1_bank_statement_append_only",
    ),
    "bank_statement_transaction_append_only": (
        "bank_statement_transaction",
        False,
        27,
        False,
        False,
        "r1_bank_statement_append_only",
    ),
    "bank_statement_observation_append_only": (
        "bank_statement_observation",
        False,
        27,
        False,
        False,
        "r1_bank_statement_append_only",
    ),
    "bank_statement_review_append_only": (
        "bank_statement_review",
        False,
        27,
        False,
        False,
        "r1_bank_statement_append_only",
    ),
    "validate_managed_account_audit": (
        "managed_account",
        False,
        7,
        False,
        False,
        "r1_validate_bank_statement",
    ),
    "validate_managed_account_lifecycle_audit": (
        "managed_account_lifecycle",
        False,
        7,
        False,
        False,
        "r1_validate_bank_statement",
    ),
    "validate_bank_statement_audit": (
        "bank_statement",
        False,
        7,
        False,
        False,
        "r1_validate_bank_statement",
    ),
    "validate_bank_statement_transaction_audit": (
        "bank_statement_transaction",
        False,
        7,
        False,
        False,
        "r1_validate_bank_statement",
    ),
    "validate_bank_statement_observation_audit": (
        "bank_statement_observation",
        False,
        7,
        False,
        False,
        "r1_validate_bank_statement",
    ),
    "validate_bank_statement_review_audit": (
        "bank_statement_review",
        False,
        7,
        False,
        False,
        "r1_validate_bank_statement",
    ),
    "require_statement_backed_account": (
        "managed_account",
        True,
        5,
        True,
        True,
        "r1_require_statement_backed_account",
    ),
    "validate_statement_facts": (
        "bank_statement",
        True,
        5,
        True,
        True,
        "r1_validate_statement_facts",
    ),
    "validate_statement_observation_set": (
        "bank_statement_observation",
        True,
        5,
        True,
        True,
        "r1_validate_statement_facts",
    ),
    "require_transaction_observation": (
        "bank_statement_transaction",
        True,
        5,
        True,
        True,
        "r1_require_transaction_observation",
    ),
}
BANK_STATEMENT_REQUIRED_TRIGGERS = frozenset(BANK_STATEMENT_TRIGGER_CONTRACT)
BANK_STATEMENT_CONSTRAINT_CONTRACT = {
    "bank_statement_audit_event_id_fkey": (
        "bank_statement",
        "f",
        "FOREIGN KEY (audit_event_id) REFERENCES audit_event(id) ON DELETE RESTRICT",
    ),
    "bank_statement_audit_event_id_key": (
        "bank_statement",
        "u",
        "UNIQUE (audit_event_id)",
    ),
    "bank_statement_check": (
        "bank_statement",
        "c",
        "CHECK ((period_start <= period_end))",
    ),
    "bank_statement_currency_check": (
        "bank_statement",
        "c",
        "CHECK (((currency)::text = 'CNY'::text))",
    ),
    "bank_statement_evidence_ref_fkey": (
        "bank_statement",
        "f",
        "FOREIGN KEY (evidence_ref) REFERENCES evidence_object(evidence_ref) ON DELETE RESTRICT",
    ),
    "bank_statement_evidence_ref_key": (
        "bank_statement",
        "u",
        "UNIQUE (evidence_ref)",
    ),
    "bank_statement_managed_account_ref_fkey": (
        "bank_statement",
        "f",
        "FOREIGN KEY (managed_account_ref) REFERENCES managed_account(managed_account_ref) "
        "ON DELETE RESTRICT",
    ),
    "bank_statement_pkey": ("bank_statement", "p", "PRIMARY KEY (statement_ref)"),
    "bank_statement_source_sha256_check": (
        "bank_statement",
        "c",
        "CHECK ((octet_length(source_sha256) = 32))",
    ),
    "bank_statement_source_sha256_key": (
        "bank_statement",
        "u",
        "UNIQUE (source_sha256)",
    ),
    "bank_statement_source_size_check": (
        "bank_statement",
        "c",
        "CHECK ((source_size > 0))",
    ),
    "bank_statement_source_system_check": (
        "bank_statement",
        "c",
        "CHECK (((source_system)::text ~ '^[a-z0-9][a-z0-9_]{0,63}$'::text))",
    ),
    "bank_statement_statement_ref_managed_account_ref_key": (
        "bank_statement",
        "u",
        "UNIQUE (statement_ref, managed_account_ref)",
    ),
    "bank_statement_transaction_count_check": (
        "bank_statement",
        "c",
        "CHECK ((transaction_count > 0))",
    ),
    "bank_statement_transaction_set_sha256_check": (
        "bank_statement",
        "c",
        "CHECK ((octet_length(transaction_set_sha256) = 32))",
    ),
    "bank_statement_observation_audit_event_id_fkey": (
        "bank_statement_observation",
        "f",
        "FOREIGN KEY (audit_event_id) REFERENCES audit_event(id) ON DELETE RESTRICT",
    ),
    "bank_statement_observation_audit_event_id_key": (
        "bank_statement_observation",
        "u",
        "UNIQUE (audit_event_id)",
    ),
    "bank_statement_observation_pkey": (
        "bank_statement_observation",
        "p",
        "PRIMARY KEY (source_event_ref)",
    ),
    "bank_statement_observation_source_row_number_check": (
        "bank_statement_observation",
        "c",
        "CHECK ((source_row_number > 0))",
    ),
    "bank_statement_observation_source_row_sha256_check": (
        "bank_statement_observation",
        "c",
        "CHECK ((octet_length(source_row_sha256) = 32))",
    ),
    "bank_statement_observation_statement_ref_managed_account_r_fkey": (
        "bank_statement_observation",
        "f",
        "FOREIGN KEY (statement_ref, managed_account_ref) REFERENCES "
        "bank_statement(statement_ref, managed_account_ref) ON DELETE RESTRICT",
    ),
    "bank_statement_observation_statement_ref_source_row_number_key": (
        "bank_statement_observation",
        "u",
        "UNIQUE (statement_ref, source_row_number)",
    ),
    "bank_statement_observation_statement_ref_transaction_ref_key": (
        "bank_statement_observation",
        "u",
        "UNIQUE (statement_ref, transaction_ref)",
    ),
    "bank_statement_observation_transaction_ref_managed_account_fkey": (
        "bank_statement_observation",
        "f",
        "FOREIGN KEY (transaction_ref, managed_account_ref) REFERENCES "
        "bank_statement_transaction(transaction_ref, managed_account_ref) ON DELETE RESTRICT",
    ),
    "bank_statement_review_assertion_jti_key": (
        "bank_statement_review",
        "u",
        "UNIQUE (assertion_jti)",
    ),
    "bank_statement_review_audit_event_id_fkey": (
        "bank_statement_review",
        "f",
        "FOREIGN KEY (audit_event_id) REFERENCES audit_event(id) ON DELETE RESTRICT",
    ),
    "bank_statement_review_audit_event_id_key": (
        "bank_statement_review",
        "u",
        "UNIQUE (audit_event_id)",
    ),
    "bank_statement_review_check": (
        "bank_statement_review",
        "c",
        "CHECK ((((revision = 1) AND ((status)::text = 'PENDING'::text) AND "
        "(operation_id IS NULL) AND (assertion_jti IS NULL) AND (actor_ref IS NULL) "
        "AND (workload_principal_ref IS NULL) AND (expected_revision IS NULL) AND "
        "(command_sha256 IS NULL)) OR ((revision > 1) AND ((status)::text = ANY "
        "((ARRAY['CONFIRMED'::character varying, 'REJECTED'::character varying])::text[])) "
        "AND (operation_id IS NOT NULL) AND (assertion_jti IS NOT NULL) AND "
        "(btrim((actor_ref)::text) <> ''::text) AND "
        "(btrim((workload_principal_ref)::text) <> ''::text) AND "
        "(expected_revision = (revision - 1)) AND (command_sha256 IS NOT NULL))))",
    ),
    "bank_statement_review_command_sha256_check": (
        "bank_statement_review",
        "c",
        "CHECK (((command_sha256 IS NULL) OR (octet_length(command_sha256) = 32)))",
    ),
    "bank_statement_review_operation_id_key": (
        "bank_statement_review",
        "u",
        "UNIQUE (operation_id)",
    ),
    "bank_statement_review_pkey": (
        "bank_statement_review",
        "p",
        "PRIMARY KEY (statement_ref, revision)",
    ),
    "bank_statement_review_revision_check": (
        "bank_statement_review",
        "c",
        "CHECK ((revision > 0))",
    ),
    "bank_statement_review_statement_ref_fkey": (
        "bank_statement_review",
        "f",
        "FOREIGN KEY (statement_ref) REFERENCES bank_statement(statement_ref) ON DELETE RESTRICT",
    ),
    "bank_statement_review_status_check": (
        "bank_statement_review",
        "c",
        "CHECK (((status)::text = ANY ((ARRAY['PENDING'::character varying, "
        "'CONFIRMED'::character varying, 'REJECTED'::character varying])::text[])))",
    ),
    "bank_statement_transaction_audit_event_id_fkey": (
        "bank_statement_transaction",
        "f",
        "FOREIGN KEY (audit_event_id) REFERENCES audit_event(id) ON DELETE RESTRICT",
    ),
    "bank_statement_transaction_audit_event_id_key": (
        "bank_statement_transaction",
        "u",
        "UNIQUE (audit_event_id)",
    ),
    "bank_statement_transaction_counterparty_ref_check": (
        "bank_statement_transaction",
        "c",
        "CHECK (((counterparty_ref)::text ~ '^cp_[a-z0-9_]{1,96}$'::text))",
    ),
    "bank_statement_transaction_currency_check": (
        "bank_statement_transaction",
        "c",
        "CHECK (((currency)::text = 'CNY'::text))",
    ),
    "bank_statement_transaction_fact_sha256_check": (
        "bank_statement_transaction",
        "c",
        "CHECK ((octet_length(fact_sha256) = 32))",
    ),
    "bank_statement_transaction_managed_account_ref_fkey": (
        "bank_statement_transaction",
        "f",
        "FOREIGN KEY (managed_account_ref) REFERENCES managed_account(managed_account_ref) "
        "ON DELETE RESTRICT",
    ),
    "bank_statement_transaction_managed_account_ref_transaction__key": (
        "bank_statement_transaction",
        "u",
        "UNIQUE (managed_account_ref, transaction_serial)",
    ),
    "bank_statement_transaction_pkey": (
        "bank_statement_transaction",
        "p",
        "PRIMARY KEY (transaction_ref)",
    ),
    "bank_statement_transaction_transaction_name_check": (
        "bank_statement_transaction",
        "c",
        "CHECK ((btrim((transaction_name)::text) <> ''::text))",
    ),
    "bank_statement_transaction_transaction_ref_managed_account__key": (
        "bank_statement_transaction",
        "u",
        "UNIQUE (transaction_ref, managed_account_ref)",
    ),
    "bank_statement_transaction_transaction_serial_check": (
        "bank_statement_transaction",
        "c",
        "CHECK ((btrim((transaction_serial)::text) <> ''::text))",
    ),
    "managed_account_account_suffix_check": (
        "managed_account",
        "c",
        "CHECK (((account_suffix)::text ~ '^[0-9]{4,8}$'::text))",
    ),
    "managed_account_audit_event_id_fkey": (
        "managed_account",
        "f",
        "FOREIGN KEY (audit_event_id) REFERENCES audit_event(id) ON DELETE RESTRICT",
    ),
    "managed_account_audit_event_id_key": (
        "managed_account",
        "u",
        "UNIQUE (audit_event_id)",
    ),
    "managed_account_entity_id_fkey": (
        "managed_account",
        "f",
        "FOREIGN KEY (entity_id) REFERENCES entity(id) ON DELETE RESTRICT",
    ),
    "managed_account_institution_code_check": (
        "managed_account",
        "c",
        "CHECK (((institution_code)::text = 'mybank'::text))",
    ),
    "managed_account_key_format": (
        "managed_account",
        "c",
        "CHECK (((account_key)::text ~ '^[a-z0-9][a-z0-9._:-]{0,199}$'::text))",
    ),
    "managed_account_kind_format": (
        "managed_account",
        "c",
        "CHECK (((account_kind)::text ~ '^[A-Z][A-Z0-9_]{0,31}$'::text))",
    ),
    "managed_account_owner_kind_check": (
        "managed_account",
        "c",
        "CHECK (((owner_kind)::text = ANY ((ARRAY['PERSONAL'::character varying, "
        "'COMPANY'::character varying])::text[])))",
    ),
    "managed_account_owner_ref_format": (
        "managed_account",
        "c",
        "CHECK (((owner_ref)::text ~ '^[a-z0-9][a-z0-9._:-]{0,199}$'::text))",
    ),
    "managed_account_pkey": ("managed_account", "p", "PRIMARY KEY (managed_account_ref)"),
    "uq_managed_account_entity_key": (
        "managed_account",
        "u",
        "UNIQUE (entity_id, account_key)",
    ),
    "managed_account_lifecycle_audit_event_id_fkey": (
        "managed_account_lifecycle",
        "f",
        "FOREIGN KEY (audit_event_id) REFERENCES audit_event(id) ON DELETE RESTRICT",
    ),
    "managed_account_lifecycle_audit_event_id_key": (
        "managed_account_lifecycle",
        "u",
        "UNIQUE (audit_event_id)",
    ),
    "managed_account_lifecycle_managed_account_ref_fkey": (
        "managed_account_lifecycle",
        "f",
        "FOREIGN KEY (managed_account_ref) REFERENCES managed_account(managed_account_ref) "
        "ON DELETE RESTRICT",
    ),
    "managed_account_lifecycle_pkey": (
        "managed_account_lifecycle",
        "p",
        "PRIMARY KEY (managed_account_ref, revision)",
    ),
    "managed_account_lifecycle_revision_check": (
        "managed_account_lifecycle",
        "c",
        "CHECK ((revision > 0))",
    ),
    "managed_account_lifecycle_status_check": (
        "managed_account_lifecycle",
        "c",
        "CHECK (((status)::text = ANY ((ARRAY['ACTIVE'::character varying, "
        "'INACTIVE'::character varying, 'CLOSED'::character varying])::text[])))",
    ),
}

ACCOUNT_REGISTRY_MANAGED_ACCOUNT_TRIGGER_CONTRACT = {
    "validate_managed_account_registry": (
        "managed_account",
        False,
        7,
        False,
        False,
        "account_registry_validate_managed_account",
    ),
}
ACCOUNT_REGISTRY_MANAGED_ACCOUNT_CONSTRAINT_CONTRACT = {
    "fk_managed_account_admission_evidence": (
        "managed_account",
        "f",
        "FOREIGN KEY (entity_id, admission_evidence_ref) REFERENCES "
        "evidence_object(entity_id, evidence_ref) ON DELETE RESTRICT",
    ),
    "managed_account_owner_ref_is_entity": (
        "managed_account",
        "c",
        "CHECK (((owner_ref)::text = (entity_id)::text))",
    ),
    "managed_account_institution_code_format": (
        "managed_account",
        "c",
        "CHECK (((institution_code)::text ~ '^[a-z0-9][a-z0-9_]{0,31}$'::text))",
    ),
    "uq_managed_account_ref_entity": (
        "managed_account",
        "u",
        "UNIQUE (managed_account_ref, entity_id)",
    ),
}
ACCOUNT_REGISTRY_FUNCTION_SIGNATURES = {
    ("public", "account_registry_normalize_alias"): "p_value text",
    ("public", "account_registry_append_only"): "",
    ("public", "account_registry_validate_managed_account"): "",
    ("public", "account_registry_validate_fact"): "",
    ("public", "account_registry_validate_business_unit_snapshot"): "",
    ("public", "account_registry_reject_assignment_overlap"): "",
    ("public", "account_registry_validate_allocation_revision"): "",
    ("public", "account_registry_require_allocation_total"): "",
    ("internal_import", "import_bank_statement_0021"): "p_request jsonb",
    ("internal_command", "apply_account_registry_plan"): "p_request jsonb",
    ("internal_read", "get_account_registry_projection"): (
        "p_owner_entity_ref uuid, p_audit_horizon_sequence bigint, p_audit_horizon_hash bytea"
    ),
}
ACCOUNT_REGISTRY_FUNCTION_RESULTS = {
    ("public", "account_registry_normalize_alias"): "text",
    ("public", "account_registry_append_only"): "trigger",
    ("public", "account_registry_validate_managed_account"): "trigger",
    ("public", "account_registry_validate_fact"): "trigger",
    ("public", "account_registry_validate_business_unit_snapshot"): "trigger",
    ("public", "account_registry_reject_assignment_overlap"): "trigger",
    ("public", "account_registry_validate_allocation_revision"): "trigger",
    ("public", "account_registry_require_allocation_total"): "trigger",
    ("internal_import", "import_bank_statement_0021"): "jsonb",
    ("internal_command", "apply_account_registry_plan"): "jsonb",
    ("internal_read", "get_account_registry_projection"): "jsonb",
}
ACCOUNT_REGISTRY_SECURITY_DEFINER_FUNCTIONS = frozenset(
    {
        ("public", "account_registry_validate_managed_account"),
        ("public", "account_registry_validate_fact"),
        ("public", "account_registry_validate_business_unit_snapshot"),
        ("public", "account_registry_reject_assignment_overlap"),
        ("public", "account_registry_validate_allocation_revision"),
        ("public", "account_registry_require_allocation_total"),
        ("internal_import", "import_bank_statement_0021"),
        ("internal_command", "apply_account_registry_plan"),
        ("internal_read", "get_account_registry_projection"),
    }
)
ACCOUNT_REGISTRY_FUNCTION_EXECUTORS = {
    ("internal_command", "apply_account_registry_plan"): "ledgerbridge_api",
    ("internal_read", "get_account_registry_projection"): "ledgerbridge_reader",
}
ACCOUNT_REGISTRY_TRIGGER_CONTRACT = {
    "account_registry_operation_append_only": (
        "account_registry_operation",
        False,
        False,
        False,
        "account_registry_append_only",
    ),
    "validate_account_registry_operation": (
        "account_registry_operation",
        False,
        False,
        False,
        "account_registry_validate_fact",
    ),
    "managed_account_alias_append_only": (
        "managed_account_alias",
        False,
        False,
        False,
        "account_registry_append_only",
    ),
    "validate_managed_account_alias_registry": (
        "managed_account_alias",
        False,
        False,
        False,
        "account_registry_validate_fact",
    ),
    "account_business_unit_assignment_append_only": (
        "account_business_unit_assignment",
        False,
        False,
        False,
        "account_registry_append_only",
    ),
    "reject_account_business_unit_overlap": (
        "account_business_unit_assignment",
        False,
        False,
        False,
        "account_registry_reject_assignment_overlap",
    ),
    "validate_account_business_unit_registry": (
        "account_business_unit_assignment",
        False,
        False,
        False,
        "account_registry_validate_fact",
    ),
    "validate_account_business_unit_snapshot": (
        "account_business_unit_assignment",
        False,
        False,
        False,
        "account_registry_validate_business_unit_snapshot",
    ),
    "fact_business_unit_allocation_set_append_only": (
        "fact_business_unit_allocation_set",
        False,
        False,
        False,
        "account_registry_append_only",
    ),
    "validate_fact_business_unit_allocation_registry": (
        "fact_business_unit_allocation_set",
        False,
        False,
        False,
        "account_registry_validate_fact",
    ),
    "validate_fact_allocation_revision": (
        "fact_business_unit_allocation_set",
        False,
        False,
        False,
        "account_registry_validate_allocation_revision",
    ),
    "require_fact_allocation_set_total": (
        "fact_business_unit_allocation_set",
        True,
        True,
        True,
        "account_registry_require_allocation_total",
    ),
    "fact_business_unit_allocation_item_append_only": (
        "fact_business_unit_allocation_item",
        False,
        False,
        False,
        "account_registry_append_only",
    ),
    "validate_fact_business_unit_snapshot": (
        "fact_business_unit_allocation_item",
        False,
        False,
        False,
        "account_registry_validate_business_unit_snapshot",
    ),
    "require_fact_allocation_item_total": (
        "fact_business_unit_allocation_item",
        True,
        True,
        True,
        "account_registry_require_allocation_total",
    ),
}

_ACCOUNT_REGISTRY_TABLES_SQL = ", ".join(f"'{name}'" for name in ACCOUNT_REGISTRY_TABLES)
_ACCOUNT_REGISTRY_ROLES_SQL = ", ".join(f"('{name}'::name)" for name in R1_CONTROLLED_ROLES)
_ACCOUNT_REGISTRY_FUNCTIONS_SQL = ", ".join(
    f"('{schema}', '{name}', '{args}')"
    for (schema, name), args in ACCOUNT_REGISTRY_FUNCTION_SIGNATURES.items()
)
_ACCOUNT_REGISTRY_ROW_COUNTS_SQL = ", ".join(
    f"'{table}', (SELECT count(*) FROM public.{table})"  # nosec B608 - fixed table allowlist.
    for table in ACCOUNT_REGISTRY_TABLES
)
ACCOUNT_REGISTRY_SECURITY_SQL = (
    ""  # nosec B608 - replacements use only fixed allowlists.
    """
WITH expected_roles(role_name) AS (VALUES __ACCOUNT_REGISTRY_ROLES_SQL__),
present_roles(role_name) AS (
 SELECT e.role_name FROM expected_roles e JOIN pg_roles r ON r.rolname=e.role_name
), expected_functions(schema_name,function_name,identity_arguments) AS (
 VALUES __ACCOUNT_REGISTRY_FUNCTIONS_SQL__
), observed_tables AS (
 SELECT c.relname table_name,pg_get_userbyid(c.relowner) owner,c.relkind kind
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' AND c.relname IN (__ACCOUNT_REGISTRY_TABLES_SQL__)
), observed_functions AS (
 SELECT p.oid function_oid,n.nspname schema_name,p.proname function_name,
  pg_get_function_identity_arguments(p.oid) identity_arguments,
  pg_get_function_result(p.oid) result,pg_get_userbyid(p.proowner) owner,
  p.prosecdef security_definer,COALESCE(to_jsonb(p.proconfig),'[]'::jsonb) proconfig
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 JOIN expected_functions e ON e.schema_name=n.nspname AND e.function_name=p.proname
  AND e.identity_arguments=pg_get_function_identity_arguments(p.oid)
), observed_triggers AS (
 SELECT c.relname table_name,t.tgname trigger_name,t.tgenabled enabled,
  t.tgconstraint<>0 is_constraint,con.condeferrable is_deferrable,
  con.condeferred is_initially_deferred,p.proname function_name
 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
 JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid
 LEFT JOIN pg_constraint con ON con.oid=t.tgconstraint
 WHERE n.nspname='public' AND c.relname IN (__ACCOUNT_REGISTRY_TABLES_SQL__)
  AND NOT t.tgisinternal
), observed_constraints AS (
 SELECT c.relname table_name,con.conname constraint_name,con.contype constraint_type,
  con.convalidated is_validated,con.condeferrable is_deferrable,
  con.condeferred is_initially_deferred
 FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
 JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' AND c.relname IN (__ACCOUNT_REGISTRY_TABLES_SQL__)
), table_acls AS (
 SELECT c.relname table_name,
  CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END grantee,
  a.privilege_type privilege,a.is_grantable grantable
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
 WHERE n.nspname='public' AND c.relname IN (__ACCOUNT_REGISTRY_TABLES_SQL__)
), function_acls AS (
 SELECT n.nspname schema_name,p.proname function_name,
  pg_get_function_identity_arguments(p.oid) identity_arguments,
  CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END grantee,
  a.privilege_type privilege,a.is_grantable grantable
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 JOIN expected_functions e ON e.schema_name=n.nspname AND e.function_name=p.proname
  AND e.identity_arguments=pg_get_function_identity_arguments(p.oid)
 CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
), table_privileges AS (
 SELECT r.role_name role,t.table_name,
  has_table_privilege(r.role_name::text,format('%I.%I','public',t.table_name),'SELECT') can_select,
  has_table_privilege(r.role_name::text,format('%I.%I','public',t.table_name),'INSERT') can_insert,
  has_table_privilege(r.role_name::text,format('%I.%I','public',t.table_name),'UPDATE') can_update,
  has_table_privilege(r.role_name::text,format('%I.%I','public',t.table_name),'DELETE') can_delete
 FROM present_roles r CROSS JOIN observed_tables t
), function_privileges AS (
 SELECT r.role_name role,f.schema_name,f.function_name,f.identity_arguments,
  has_function_privilege(r.role_name::text,f.function_oid,'EXECUTE') can_execute
 FROM present_roles r CROSS JOIN observed_functions f
)
SELECT jsonb_build_object(
 'account_registry_row_counts',jsonb_build_object(__ACCOUNT_REGISTRY_ROW_COUNTS_SQL__),
 'account_registry_tables',COALESCE((SELECT jsonb_agg(jsonb_build_object(
  'table',table_name,'owner',owner,'kind',kind) ORDER BY table_name)
  FROM observed_tables),'[]'::jsonb),
 'account_registry_functions',COALESCE((SELECT jsonb_agg(jsonb_build_object(
  'schema',schema_name,'name',function_name,'identity_arguments',identity_arguments,
  'result',result,'owner',owner,'security_definer',security_definer,'proconfig',proconfig)
  ORDER BY schema_name,function_name,identity_arguments) FROM observed_functions),'[]'::jsonb),
 'account_registry_triggers',COALESCE((SELECT jsonb_agg(jsonb_build_object(
  'table',table_name,'name',trigger_name,'enabled',enabled,'constraint',is_constraint,
  'deferrable',COALESCE(is_deferrable,false),
  'initially_deferred',COALESCE(is_initially_deferred,false),'function_name',function_name)
  ORDER BY table_name,trigger_name) FROM observed_triggers),'[]'::jsonb),
 'account_registry_constraints',COALESCE((SELECT jsonb_agg(jsonb_build_object(
  'table',table_name,'name',constraint_name,'type',constraint_type,
  'validated',is_validated,'deferrable',is_deferrable,
  'initially_deferred',is_initially_deferred) ORDER BY table_name,constraint_name)
  FROM observed_constraints),'[]'::jsonb),
 'account_registry_table_acls',COALESCE((SELECT jsonb_agg(jsonb_build_object(
  'table',table_name,'grantee',grantee,'privilege',privilege,'grantable',grantable)
  ORDER BY table_name,grantee,privilege) FROM table_acls),'[]'::jsonb),
 'account_registry_function_acls',COALESCE((SELECT jsonb_agg(jsonb_build_object(
  'schema',schema_name,'name',function_name,'identity_arguments',identity_arguments,
  'grantee',grantee,'privilege',privilege,'grantable',grantable)
  ORDER BY schema_name,function_name,identity_arguments,grantee,privilege)
  FROM function_acls),'[]'::jsonb),
 'account_registry_effective_table_privileges',COALESCE((SELECT jsonb_agg(jsonb_build_object(
  'role',role,'table',table_name,'select',can_select,'insert',can_insert,
  'update',can_update,'delete',can_delete) ORDER BY role,table_name)
  FROM table_privileges),'[]'::jsonb),
 'account_registry_effective_function_privileges',COALESCE((SELECT jsonb_agg(jsonb_build_object(
  'role',role,'schema',schema_name,'name',function_name,
  'identity_arguments',identity_arguments,'execute',can_execute)
  ORDER BY role,schema_name,function_name,identity_arguments)
  FROM function_privileges),'[]'::jsonb)
)::text;
    """.replace("__ACCOUNT_REGISTRY_ROLES_SQL__", _ACCOUNT_REGISTRY_ROLES_SQL)
    .replace("__ACCOUNT_REGISTRY_FUNCTIONS_SQL__", _ACCOUNT_REGISTRY_FUNCTIONS_SQL)
    .replace("__ACCOUNT_REGISTRY_TABLES_SQL__", _ACCOUNT_REGISTRY_TABLES_SQL)
    .replace("__ACCOUNT_REGISTRY_ROW_COUNTS_SQL__", _ACCOUNT_REGISTRY_ROW_COUNTS_SQL)
    .strip()
)

_BANK_TABLES_SQL = ", ".join(f"'{name}'" for name in BANK_STATEMENT_TABLES)
_BANK_ROLES_SQL = ", ".join(f"('{name}'::name)" for name in R1_CONTROLLED_ROLES)
_BANK_FUNCTIONS_SQL = ", ".join(
    f"('{schema}', '{name}', '{args}')"
    for (schema, name), args in BANK_STATEMENT_FUNCTION_SIGNATURES.items()
)
BANK_STATEMENT_SECURITY_SQL = (
    ""  # nosec B608 - replacements use only fixed allowlists.
    """
WITH expected_roles(role_name) AS (VALUES __R1_ROLE_SQL__),
present_roles(role_name) AS (
 SELECT e.role_name FROM expected_roles e JOIN pg_roles r ON r.rolname=e.role_name
), expected_functions(schema_name,function_name,identity_arguments) AS (
 VALUES __BANK_FUNCTIONS_SQL__
), observed_tables AS (
 SELECT c.relname table_name,pg_get_userbyid(c.relowner) owner,c.relkind kind
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' AND c.relname IN (__BANK_TABLES_SQL__)
), observed_schemas AS (
 SELECT n.nspname schema_name,pg_get_userbyid(n.nspowner) owner
 FROM pg_namespace n
 WHERE n.nspname IN ('internal_import','internal_command','internal_read')
), observed_functions AS (
 SELECT n.nspname schema_name,p.proname function_name,
  pg_get_function_identity_arguments(p.oid) identity_arguments,
  pg_get_function_result(p.oid) result,
  pg_get_userbyid(p.proowner) owner,p.prosecdef security_definer,
  COALESCE(to_json(p.proconfig),'[]'::json) proconfig,p.oid,p.proacl acl
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE EXISTS (SELECT 1 FROM expected_functions e
  WHERE e.schema_name=n.nspname AND e.function_name=p.proname)
), observed_triggers AS (
 SELECT c.relname table_name,t.tgname trigger_name,t.tgenabled enabled,
  t.tgconstraint<>0 is_constraint,t.tgtype trigger_type,
  t.tgdeferrable is_deferrable,t.tginitdeferred is_initially_deferred,
  fnn.nspname function_schema,fn.proname function_name
 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
 JOIN pg_namespace n ON n.oid=c.relnamespace
 JOIN pg_proc fn ON fn.oid=t.tgfoid
 JOIN pg_namespace fnn ON fnn.oid=fn.pronamespace
 WHERE n.nspname='public' AND NOT t.tgisinternal AND c.relname IN (__BANK_TABLES_SQL__)
), observed_constraints AS (
 SELECT c.relname table_name,con.conname constraint_name,con.contype constraint_type,
  con.convalidated is_validated,con.condeferrable is_deferrable,
  con.condeferred is_initially_deferred,pg_get_constraintdef(con.oid,false) definition
 FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
 JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' AND c.relname IN (__BANK_TABLES_SQL__)
  AND con.contype IN ('p','u','f','c','x')
), table_acls AS (
 SELECT c.relname table_name,
  CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END grantee,
  a.privilege_type privilege,a.is_grantable grantable
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 CROSS JOIN LATERAL aclexplode(c.relacl) a
 WHERE n.nspname='public' AND c.relname IN (__BANK_TABLES_SQL__)
), function_acls AS (
 SELECT f.schema_name,f.function_name,f.identity_arguments,
  CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END grantee,
  a.privilege_type privilege,a.is_grantable grantable
 FROM observed_functions f
 CROSS JOIN LATERAL aclexplode(f.acl) a
), schema_acls AS (
 SELECT n.nspname schema_name,
  CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END grantee,
  a.privilege_type privilege,a.is_grantable grantable
 FROM pg_namespace n
 CROSS JOIN LATERAL aclexplode(n.nspacl) a
 WHERE n.nspname IN ('internal_import','internal_command','internal_read')
), table_privileges AS (
 SELECT r.role_name role,o.object_name,
  has_table_privilege(r.role_name,format('public.%I',o.object_name),'SELECT') can_select,
  has_table_privilege(r.role_name,format('public.%I',o.object_name),'INSERT') can_insert,
  has_table_privilege(r.role_name,format('public.%I',o.object_name),'UPDATE') can_update,
  has_table_privilege(r.role_name,format('public.%I',o.object_name),'DELETE') can_delete,
  has_table_privilege(r.role_name,format('public.%I',o.object_name),'TRUNCATE') can_truncate,
  has_table_privilege(r.role_name,format('public.%I',o.object_name),'REFERENCES') can_reference,
  has_table_privilege(r.role_name,format('public.%I',o.object_name),'TRIGGER') can_trigger
 FROM present_roles r CROSS JOIN unnest(ARRAY[__BANK_TABLES_SQL__]::text[]) o(object_name)
), function_privileges AS (
 SELECT r.role_name role,f.schema_name,f.function_name,f.identity_arguments,
  has_function_privilege(r.role_name,f.oid,'EXECUTE') can_execute
 FROM present_roles r CROSS JOIN observed_functions f
), schema_privileges AS (
 SELECT r.role_name role,s.schema_name,
  has_schema_privilege(r.role_name,s.schema_name,'USAGE') can_use,
  has_schema_privilege(r.role_name,s.schema_name,'CREATE') can_create
 FROM present_roles r
 CROSS JOIN (VALUES ('internal_import'::text),('internal_command'::text),
  ('internal_read'::text)) s(schema_name)
)
SELECT json_build_object(
 'bank_statement_row_counts',json_build_object(
  'managed_account',(SELECT count(*) FROM public.managed_account),
  'managed_account_lifecycle',(SELECT count(*) FROM public.managed_account_lifecycle),
  'bank_statement',(SELECT count(*) FROM public.bank_statement),
  'bank_statement_transaction',(SELECT count(*) FROM public.bank_statement_transaction),
  'bank_statement_observation',(SELECT count(*) FROM public.bank_statement_observation),
 'bank_statement_review',(SELECT count(*) FROM public.bank_statement_review)),
 'bank_statement_tables',COALESCE((SELECT json_agg(json_build_object(
  'table',table_name,'owner',owner,'kind',kind) ORDER BY table_name)
  FROM observed_tables),'[]'::json),
 'bank_statement_schemas',COALESCE((SELECT json_agg(json_build_object(
  'schema',schema_name,'owner',owner) ORDER BY schema_name)
  FROM observed_schemas),'[]'::json),
 'bank_statement_functions',COALESCE((SELECT json_agg(json_build_object(
  'schema',schema_name,'name',function_name,'identity_arguments',identity_arguments,
  'result',result,'owner',owner,'security_definer',security_definer,'proconfig',proconfig)
  ORDER BY schema_name,function_name,identity_arguments) FROM observed_functions),'[]'::json),
 'bank_statement_triggers',COALESCE((SELECT json_agg(json_build_object(
  'table',table_name,'name',trigger_name,'enabled',enabled,'constraint',is_constraint,
  'trigger_type',trigger_type,'deferrable',is_deferrable,
  'initially_deferred',is_initially_deferred,
  'function_schema',function_schema,'function_name',function_name)
  ORDER BY table_name,trigger_name) FROM observed_triggers),'[]'::json),
 'bank_statement_constraints',COALESCE((SELECT json_agg(json_build_object(
  'table',table_name,'name',constraint_name,'type',constraint_type,
  'validated',is_validated,'deferrable',is_deferrable,
  'initially_deferred',is_initially_deferred,'definition',definition)
  ORDER BY table_name,constraint_name) FROM observed_constraints),'[]'::json),
 'bank_statement_table_acls',COALESCE((SELECT json_agg(json_build_object(
  'table',table_name,'grantee',grantee,'privilege',privilege,'grantable',grantable)
  ORDER BY table_name,grantee,privilege) FROM table_acls),'[]'::json),
 'bank_statement_function_acls',COALESCE((SELECT json_agg(json_build_object(
  'schema',schema_name,'name',function_name,'identity_arguments',identity_arguments,
  'grantee',grantee,'privilege',privilege,'grantable',grantable)
  ORDER BY schema_name,function_name,identity_arguments,grantee,privilege)
  FROM function_acls),'[]'::json),
 'bank_statement_schema_acls',COALESCE((SELECT json_agg(json_build_object(
  'schema',schema_name,'grantee',grantee,'privilege',privilege,'grantable',grantable)
  ORDER BY schema_name,grantee,privilege) FROM schema_acls),'[]'::json),
 'bank_statement_effective_table_privileges',COALESCE((SELECT json_agg(json_build_object(
  'role',role,'table',object_name,'select',can_select,'insert',can_insert,
  'update',can_update,'delete',can_delete,'truncate',can_truncate,
  'references',can_reference,'trigger',can_trigger) ORDER BY role,object_name)
  FROM table_privileges),'[]'::json),
 'bank_statement_effective_function_privileges',COALESCE((SELECT json_agg(json_build_object(
  'role',role,'schema',schema_name,'name',function_name,
  'identity_arguments',identity_arguments,'execute',can_execute)
  ORDER BY role,schema_name,function_name,identity_arguments) FROM function_privileges),'[]'::json),
 'bank_statement_effective_schema_privileges',COALESCE((SELECT json_agg(json_build_object(
  'role',role,'schema',schema_name,'usage',can_use,'create',can_create)
  ORDER BY role,schema_name) FROM schema_privileges),'[]'::json)
)::text;
    """.replace("__R1_ROLE_SQL__", _BANK_ROLES_SQL)
    .replace("__BANK_TABLES_SQL__", _BANK_TABLES_SQL)
    .replace("__BANK_FUNCTIONS_SQL__", _BANK_FUNCTIONS_SQL)
    .strip()
)

EVIDENCE_UNLOCK_SECURITY_REVISION = "20260830_0025"
CLASSIFICATION_BATCH_SECURITY_REVISION = "20260831_0026"
EVIDENCE_UNLOCK_TABLES = (
    "evidence_unlock_source",
    "evidence_unlock_operation",
    "evidence_unlock_receipt",
    "evidence_unlock_output",
)
EVIDENCE_UNLOCK_TABLE_SCHEMAS = {
    "evidence_unlock_source": "internal_import",
    "evidence_unlock_operation": "internal_command",
    "evidence_unlock_receipt": "internal_command",
    "evidence_unlock_output": "internal_command",
}
EVIDENCE_UNLOCK_FUNCTION_SIGNATURES = {
    "register_evidence_unlock_source": "p_request jsonb",
    "evidence_unlock_reject_mutation": "",
    "normalize_evidence_unlock_scope_bindings": "p_bindings jsonb",
    "require_evidence_unlock_operation": "p_request jsonb, p_contract_version text",
    "prepare_evidence_unlock": "p_request jsonb",
    "complete_evidence_unlock": "p_request jsonb",
    "reject_evidence_unlock": "p_request jsonb",
    "project_evidence_unlocks": "p_evidence jsonb, p_audit_horizon_sequence bigint",
    "list_candidates_base_as_of": R1_INTERNAL_READ_FUNCTION_SIGNATURES["list_candidates_as_of"],
    "render_candidate_revision_base": "p_candidate_id uuid, p_revision integer",
    "list_candidates_as_of": R1_INTERNAL_READ_FUNCTION_SIGNATURES["list_candidates_as_of"],
    "render_candidate_revision": "p_candidate_id uuid, p_revision integer",
}
EVIDENCE_UNLOCK_FUNCTION_SCHEMAS = {
    "register_evidence_unlock_source": "internal_import",
    "evidence_unlock_reject_mutation": "internal_command",
    "normalize_evidence_unlock_scope_bindings": "internal_command",
    "require_evidence_unlock_operation": "internal_command",
    "prepare_evidence_unlock": "internal_command",
    "complete_evidence_unlock": "internal_command",
    "reject_evidence_unlock": "internal_command",
    "project_evidence_unlocks": "internal_read",
    "list_candidates_base_as_of": "internal_read",
    "render_candidate_revision_base": "internal_read",
    "list_candidates_as_of": "internal_read",
    "render_candidate_revision": "internal_read",
}
EVIDENCE_UNLOCK_FUNCTION_EXECUTORS = {
    "register_evidence_unlock_source": "ledgerbridge_worker",
    "prepare_evidence_unlock": "ledgerbridge_api",
    "complete_evidence_unlock": "ledgerbridge_api",
    "reject_evidence_unlock": "ledgerbridge_api",
    "list_candidates_as_of": "ledgerbridge_reader",
}
_EVIDENCE_UNLOCK_LIST_CANDIDATES_RESULT = (
    "TABLE(contract_version character varying, candidate_ref uuid, "
    "short_id character varying, revision integer, status character varying, "
    "entity_ref uuid, business_unit_ref character varying, "
    "business_unit_label character varying, category_code character varying, "
    "category_label character varying, amount_minor bigint, currency character varying, "
    "accounting_month character varying, summary character varying, "
    "confidence_basis_points smallint, source jsonb, evidence jsonb, blockers jsonb, "
    "review_summary jsonb, created_at timestamp with time zone, "
    "updated_at timestamp with time zone, supersedes_candidate_ref uuid, "
    "superseded_by_candidate_ref uuid)"
)
EVIDENCE_UNLOCK_FUNCTION_RESULTS = {
    "register_evidence_unlock_source": "TABLE(source_ref uuid, source_evidence_ref uuid)",
    "evidence_unlock_reject_mutation": "trigger",
    "normalize_evidence_unlock_scope_bindings": "jsonb",
    "require_evidence_unlock_operation": "internal_command.evidence_unlock_operation",
    "prepare_evidence_unlock": (
        "TABLE(outcome text, source_ref uuid, source_evidence_ref uuid, entity_ref uuid, "
        "business_unit_ref character varying, object_ref character varying, "
        "plaintext_sha256 bytea, plaintext_size bigint, ciphertext_sha256 bytea, "
        "ciphertext_size bigint, storage_key character varying, chunk_size integer, "
        "stream_header bytea, wrapped_key_generation character varying, "
        "wrapped_key_nonce bytea, wrapped_key_ciphertext bytea)"
    ),
    "complete_evidence_unlock": "TABLE(source_ref uuid, unlock_status text)",
    "reject_evidence_unlock": "TABLE(source_ref uuid)",
    "project_evidence_unlocks": "jsonb",
    "list_candidates_base_as_of": _EVIDENCE_UNLOCK_LIST_CANDIDATES_RESULT,
    "render_candidate_revision_base": "jsonb",
    "list_candidates_as_of": _EVIDENCE_UNLOCK_LIST_CANDIDATES_RESULT,
    "render_candidate_revision": "jsonb",
}
EVIDENCE_UNLOCK_SECURITY_DEFINER_FUNCTIONS = frozenset(
    set(EVIDENCE_UNLOCK_FUNCTION_SIGNATURES) - {"normalize_evidence_unlock_scope_bindings"}
)
EVIDENCE_UNLOCK_TRIGGER_CONTRACT = {
    "evidence_unlock_source_append_only": (
        "internal_import",
        "evidence_unlock_source",
        "evidence_unlock_reject_mutation",
    ),
    "evidence_unlock_operation_append_only": (
        "internal_command",
        "evidence_unlock_operation",
        "evidence_unlock_reject_mutation",
    ),
    "evidence_unlock_receipt_append_only": (
        "internal_command",
        "evidence_unlock_receipt",
        "evidence_unlock_reject_mutation",
    ),
    "evidence_unlock_output_append_only": (
        "internal_command",
        "evidence_unlock_output",
        "evidence_unlock_reject_mutation",
    ),
}

_EVIDENCE_UNLOCK_ROLES_SQL = ", ".join(f"('{name}'::name)" for name in R1_CONTROLLED_ROLES)
_EVIDENCE_UNLOCK_TABLES_SQL = ", ".join(
    f"('{schema}', '{table}')" for table, schema in EVIDENCE_UNLOCK_TABLE_SCHEMAS.items()
)
_EVIDENCE_UNLOCK_FUNCTIONS_SQL = ", ".join(
    f"('{EVIDENCE_UNLOCK_FUNCTION_SCHEMAS[name]}', '{name}', '{arguments}')"
    for name, arguments in EVIDENCE_UNLOCK_FUNCTION_SIGNATURES.items()
)
_EVIDENCE_UNLOCK_ROW_COUNTS_SQL = ", ".join(
    f"'{table}', (SELECT count(*) FROM {schema}.{table})"  # nosec B608 - fixed allowlist.
    for table, schema in EVIDENCE_UNLOCK_TABLE_SCHEMAS.items()
)
EVIDENCE_UNLOCK_SECURITY_SQL = (
    ""  # nosec B608 - replacements use only fixed allowlists.
    """
WITH expected_roles(role_name) AS (VALUES __EVIDENCE_UNLOCK_ROLES_SQL__),
present_roles(role_name) AS (
 SELECT e.role_name FROM expected_roles e JOIN pg_roles r ON r.rolname=e.role_name
), expected_tables(schema_name,table_name) AS (
 VALUES __EVIDENCE_UNLOCK_TABLES_SQL__
), expected_functions(schema_name,function_name,identity_arguments) AS (
 VALUES __EVIDENCE_UNLOCK_FUNCTIONS_SQL__
), observed_tables AS (
 SELECT n.nspname schema_name,c.relname table_name,pg_get_userbyid(c.relowner) owner,c.relkind kind
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 JOIN expected_tables e ON e.schema_name=n.nspname AND e.table_name=c.relname
), observed_functions AS (
 SELECT p.oid function_oid,n.nspname schema_name,p.proname function_name,
  pg_get_function_identity_arguments(p.oid) identity_arguments,
  pg_get_function_result(p.oid) result,pg_get_userbyid(p.proowner) owner,
  p.prosecdef security_definer,COALESCE(to_jsonb(p.proconfig),'[]'::jsonb) proconfig
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 JOIN expected_functions e ON e.schema_name=n.nspname AND e.function_name=p.proname
  AND e.identity_arguments=pg_get_function_identity_arguments(p.oid)
), observed_triggers AS (
 SELECT n.nspname schema_name,c.relname table_name,t.tgname trigger_name,t.tgenabled enabled,
  t.tgconstraint<>0 is_constraint,p.proname function_name
 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
 JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid
 JOIN expected_tables e ON e.schema_name=n.nspname AND e.table_name=c.relname
 WHERE NOT t.tgisinternal
), table_acls AS (
 SELECT n.nspname schema_name,c.relname table_name,
  CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END grantee,
  a.privilege_type privilege,a.is_grantable grantable
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 JOIN expected_tables e ON e.schema_name=n.nspname AND e.table_name=c.relname
 CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
), function_acls AS (
 SELECT n.nspname schema_name,p.proname function_name,
  pg_get_function_identity_arguments(p.oid) identity_arguments,
  CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END grantee,
  a.privilege_type privilege,a.is_grantable grantable
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 JOIN expected_functions e ON e.schema_name=n.nspname AND e.function_name=p.proname
  AND e.identity_arguments=pg_get_function_identity_arguments(p.oid)
 CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
), table_privileges AS (
 SELECT r.role_name role,t.schema_name,t.table_name,
  has_table_privilege(r.role_name::text,format('%I.%I',t.schema_name,t.table_name),'SELECT') can_select,
  has_table_privilege(r.role_name::text,format('%I.%I',t.schema_name,t.table_name),'INSERT') can_insert,
  has_table_privilege(r.role_name::text,format('%I.%I',t.schema_name,t.table_name),'UPDATE') can_update,
 has_table_privilege(r.role_name::text,format('%I.%I',t.schema_name,t.table_name),'DELETE') can_delete
 FROM present_roles r CROSS JOIN observed_tables t
), function_privileges AS (
 SELECT r.role_name role,f.schema_name,f.function_name,f.identity_arguments,
  has_function_privilege(r.role_name::text,f.function_oid,'EXECUTE') can_execute
 FROM present_roles r CROSS JOIN observed_functions f
)
SELECT jsonb_build_object(
 'evidence_unlock_row_counts',jsonb_build_object(__EVIDENCE_UNLOCK_ROW_COUNTS_SQL__),
 'evidence_unlock_tables',COALESCE((SELECT jsonb_agg(jsonb_build_object(
  'schema',schema_name,'table',table_name,'owner',owner,'kind',kind)
  ORDER BY schema_name,table_name) FROM observed_tables),'[]'::jsonb),
 'evidence_unlock_functions',COALESCE((SELECT jsonb_agg(jsonb_build_object(
  'schema',schema_name,'name',function_name,'identity_arguments',identity_arguments,
  'result',result,'owner',owner,'security_definer',security_definer,'proconfig',proconfig)
  ORDER BY schema_name,function_name,identity_arguments) FROM observed_functions),'[]'::jsonb),
 'evidence_unlock_triggers',COALESCE((SELECT jsonb_agg(jsonb_build_object(
  'schema',schema_name,'table',table_name,'name',trigger_name,'enabled',enabled,
  'constraint',is_constraint,'function_name',function_name)
  ORDER BY schema_name,table_name,trigger_name) FROM observed_triggers),'[]'::jsonb),
 'evidence_unlock_table_acls',COALESCE((SELECT jsonb_agg(jsonb_build_object(
  'schema',schema_name,'table',table_name,'grantee',grantee,'privilege',privilege,
  'grantable',grantable) ORDER BY schema_name,table_name,grantee,privilege)
  FROM table_acls),'[]'::jsonb),
 'evidence_unlock_function_acls',COALESCE((SELECT jsonb_agg(jsonb_build_object(
  'schema',schema_name,'name',function_name,'identity_arguments',identity_arguments,
  'grantee',grantee,'privilege',privilege,'grantable',grantable)
  ORDER BY schema_name,function_name,identity_arguments,grantee,privilege)
  FROM function_acls),'[]'::jsonb),
 'evidence_unlock_effective_table_privileges',COALESCE((SELECT jsonb_agg(jsonb_build_object(
  'role',role,'schema',schema_name,'table',table_name,'select',can_select,'insert',can_insert,
  'update',can_update,'delete',can_delete) ORDER BY role,schema_name,table_name)
  FROM table_privileges),'[]'::jsonb),
 'evidence_unlock_effective_function_privileges',COALESCE((SELECT jsonb_agg(jsonb_build_object(
  'role',role,'schema',schema_name,'name',function_name,'identity_arguments',identity_arguments,
  'execute',can_execute) ORDER BY role,schema_name,function_name,identity_arguments)
  FROM function_privileges),'[]'::jsonb)
)::text;
    """.replace("__EVIDENCE_UNLOCK_ROLES_SQL__", _EVIDENCE_UNLOCK_ROLES_SQL)
    .replace("__EVIDENCE_UNLOCK_TABLES_SQL__", _EVIDENCE_UNLOCK_TABLES_SQL)
    .replace("__EVIDENCE_UNLOCK_FUNCTIONS_SQL__", _EVIDENCE_UNLOCK_FUNCTIONS_SQL)
    .replace("__EVIDENCE_UNLOCK_ROW_COUNTS_SQL__", _EVIDENCE_UNLOCK_ROW_COUNTS_SQL)
    .strip()
)

COMPANY_REPORTING_SECURITY_REVISION = "20260830_0024"
MYBANK_CUTOVER_SCHEMA_REVISIONS = frozenset(
    {
        ACCOUNT_REGISTRY_SECURITY_REVISION,
        COMPANY_REPORTING_SECURITY_REVISION,
        EVIDENCE_UNLOCK_SECURITY_REVISION,
        CLASSIFICATION_BATCH_SECURITY_REVISION,
    }
)
COMPANY_REPORTING_SCHEMA = "company_reporting_read"
COMPANY_REPORTING_FUNCTION_SIGNATURES = {
    "unavailable_balance_v1": "",
    "candidate_report_v1_as_of": (
        "p_entity_ref uuid, p_business_unit_ids uuid[], p_include_unassigned boolean, "
        "p_from_month date, p_to_month date, p_audit_sequence bigint"
    ),
    "statement_report_v1_as_of": (
        "p_entity_ref uuid, p_business_unit_ids uuid[], p_include_unassigned boolean, "
        "p_from_month date, p_to_month date, p_audit_sequence bigint"
    ),
    "posted_report_v1_as_of": (
        "p_entity_ref uuid, p_business_unit_ids uuid[], p_from_month date, "
        "p_to_month date, p_audit_sequence bigint"
    ),
    "get_company_report_v1_as_of": (
        "p_entity_ref uuid, p_business_unit_ids uuid[], p_include_unassigned boolean, "
        "p_basis character varying, p_from_month date, p_to_month date, "
        "p_audit_sequence bigint, p_audit_hash bytea"
    ),
}
COMPANY_REPORTING_FUNCTION_RESULTS = {
    "unavailable_balance_v1": "jsonb",
    "candidate_report_v1_as_of": "jsonb",
    "statement_report_v1_as_of": "jsonb",
    "posted_report_v1_as_of": "jsonb",
    "get_company_report_v1_as_of": "TABLE(report jsonb)",
}
COMPANY_REPORTING_SECURITY_DEFINER_FUNCTIONS = frozenset(
    {
        "candidate_report_v1_as_of",
        "statement_report_v1_as_of",
        "posted_report_v1_as_of",
        "get_company_report_v1_as_of",
    }
)
COMPANY_REPORTING_REQUIRED_TABLES = (
    "account",
    "account_business_unit_assignment",
    "audit_event",
    "bank_statement",
    "bank_statement_observation",
    "bank_statement_review",
    "bank_statement_transaction",
    "business_unit",
    "candidate",
    "candidate_event",
    "candidate_revision",
    "candidate_source",
    "entity",
    "fact_business_unit_allocation_item",
    "fact_business_unit_allocation_set",
    "journal_entry",
    "journal_entry_attribution",
    "managed_account",
    "posting",
)
COMPANY_REPORTING_REQUIRED_COLUMNS = {
    ("journal_entry_attribution", "business_unit_ref_snapshot"): "character varying(100)",
    ("journal_entry_attribution", "business_unit_label_snapshot"): "character varying(200)",
}
COMPANY_REPORTING_REQUIRED_FUNCTIONS = {
    ("public", "r1_assert_posted_total_integrity", ""): "boolean",
}
COMPANY_REPORTING_TRIGGER_CONTRACT = {
    "r1_capture_journal_attribution_snapshot": (
        "journal_entry_attribution",
        False,
        7,
        False,
        False,
        "r1_capture_journal_attribution_snapshot",
    ),
    "r1_posted_attribution_business_unit_snapshot": (
        "journal_entry_attribution",
        True,
        5,
        True,
        True,
        "r1_require_posted_business_unit_snapshot",
    ),
    "r1_posted_entry_business_unit_snapshot": (
        "journal_entry",
        True,
        21,
        True,
        True,
        "r1_require_posted_business_unit_snapshot",
    ),
}

_COMPANY_REPORTING_FUNCTIONS_SQL = ", ".join(
    f"('{name}', '{arguments}')"
    for name, arguments in COMPANY_REPORTING_FUNCTION_SIGNATURES.items()
)
_COMPANY_REPORTING_TABLES_SQL = ", ".join(f"'{name}'" for name in COMPANY_REPORTING_REQUIRED_TABLES)
_COMPANY_REPORTING_COLUMNS_SQL = ", ".join(
    f"('{table}', '{column}')" for table, column in COMPANY_REPORTING_REQUIRED_COLUMNS
)
_COMPANY_REPORTING_TRIGGERS_SQL = ", ".join(
    f"'{name}'" for name in COMPANY_REPORTING_TRIGGER_CONTRACT
)
_COMPANY_REPORTING_ROLES_SQL = ", ".join(f"('{name}'::name)" for name in R1_CONTROLLED_ROLES)
COMPANY_REPORTING_SECURITY_SQL = (
    ""  # nosec B608 - replacements use only fixed allowlists.
    """
WITH expected_roles(role_name) AS (VALUES __R1_ROLE_SQL__),
present_roles(role_name) AS (
 SELECT e.role_name FROM expected_roles e JOIN pg_roles r ON r.rolname=e.role_name
), expected_functions(function_name,identity_arguments) AS (
 VALUES __COMPANY_REPORTING_FUNCTIONS_SQL__
), expected_columns(table_name,column_name) AS (
 VALUES __COMPANY_REPORTING_COLUMNS_SQL__
), observed_schema AS (
 SELECT n.nspname schema_name,pg_get_userbyid(n.nspowner) owner,n.oid,n.nspacl acl
 FROM pg_namespace n WHERE n.nspname='company_reporting_read'
), observed_functions AS (
 SELECT n.nspname schema_name,p.proname function_name,
  pg_get_function_identity_arguments(p.oid) identity_arguments,
  pg_get_function_result(p.oid) result,pg_get_userbyid(p.proowner) owner,
  p.prosecdef security_definer,COALESCE(to_json(p.proconfig),'[]'::json) proconfig,
  p.oid,p.proacl acl
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='company_reporting_read'
  AND EXISTS (SELECT 1 FROM expected_functions e WHERE e.function_name=p.proname)
), observed_required_functions AS (
 SELECT n.nspname schema_name,p.proname function_name,
  pg_get_function_identity_arguments(p.oid) identity_arguments,
  pg_get_function_result(p.oid) result,pg_get_userbyid(p.proowner) owner,
  p.prosecdef security_definer,COALESCE(to_json(p.proconfig),'[]'::json) proconfig
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='public' AND p.proname='r1_assert_posted_total_integrity'
), observed_tables AS (
 SELECT n.nspname schema_name,c.relname table_name,
  pg_get_userbyid(c.relowner) owner,c.relkind kind
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' AND c.relname IN (__COMPANY_REPORTING_TABLES_SQL__)
), observed_columns AS (
 SELECT n.nspname schema_name,c.relname table_name,a.attname column_name,
  format_type(a.atttypid,a.atttypmod) data_type
 FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
 JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' AND a.attnum>0 AND NOT a.attisdropped
  AND EXISTS (SELECT 1 FROM expected_columns e
   WHERE e.table_name=c.relname AND e.column_name=a.attname)
), observed_triggers AS (
 SELECT c.relname table_name,t.tgname trigger_name,t.tgenabled enabled,
  t.tgconstraint<>0 is_constraint,t.tgtype trigger_type,
  t.tgdeferrable is_deferrable,t.tginitdeferred is_initially_deferred,
  fnn.nspname function_schema,fn.proname function_name
 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
 JOIN pg_namespace n ON n.oid=c.relnamespace
 JOIN pg_proc fn ON fn.oid=t.tgfoid JOIN pg_namespace fnn ON fnn.oid=fn.pronamespace
 WHERE n.nspname='public' AND NOT t.tgisinternal
  AND t.tgname IN (__COMPANY_REPORTING_TRIGGERS_SQL__)
), schema_acls AS (
 SELECT s.schema_name,
  CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END grantee,
  a.privilege_type privilege,a.is_grantable grantable
 FROM observed_schema s CROSS JOIN LATERAL aclexplode(COALESCE(s.acl,'{}'::aclitem[])) a
), function_acls AS (
 SELECT f.schema_name,f.function_name,f.identity_arguments,
  CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END grantee,
  a.privilege_type privilege,a.is_grantable grantable
 FROM observed_functions f
 CROSS JOIN LATERAL aclexplode(COALESCE(f.acl,'{}'::aclitem[])) a
), schema_privileges AS (
 SELECT r.role_name role,s.schema_name,
  has_schema_privilege(r.role_name,s.oid,'USAGE') can_use,
  has_schema_privilege(r.role_name,s.oid,'CREATE') can_create
 FROM present_roles r CROSS JOIN observed_schema s
), function_privileges AS (
 SELECT r.role_name role,f.schema_name,f.function_name,f.identity_arguments,
  has_function_privilege(r.role_name,f.oid,'EXECUTE') can_execute
 FROM present_roles r CROSS JOIN observed_functions f
)
SELECT json_build_object(
 'company_reporting_schema',(SELECT json_build_object(
  'schema',schema_name,'owner',owner) FROM observed_schema),
 'company_reporting_functions',COALESCE((SELECT json_agg(json_build_object(
  'schema',schema_name,'name',function_name,'identity_arguments',identity_arguments,
  'result',result,'owner',owner,'security_definer',security_definer,'proconfig',proconfig)
  ORDER BY function_name,identity_arguments) FROM observed_functions),'[]'::json),
 'company_reporting_required_tables',COALESCE((SELECT json_agg(json_build_object(
  'schema',schema_name,'table',table_name,'owner',owner,'kind',kind) ORDER BY table_name)
  FROM observed_tables),'[]'::json),
 'company_reporting_required_columns',COALESCE((SELECT json_agg(json_build_object(
  'schema',schema_name,'table',table_name,'column',column_name,'type',data_type)
  ORDER BY table_name,column_name) FROM observed_columns),'[]'::json),
 'company_reporting_required_functions',COALESCE((SELECT json_agg(json_build_object(
  'schema',schema_name,'name',function_name,'identity_arguments',identity_arguments,
  'result',result,'owner',owner,'security_definer',security_definer,'proconfig',proconfig)
  ORDER BY schema_name,function_name,identity_arguments)
  FROM observed_required_functions),'[]'::json),
 'company_reporting_triggers',COALESCE((SELECT json_agg(json_build_object(
  'table',table_name,'name',trigger_name,'enabled',enabled,'constraint',is_constraint,
  'trigger_type',trigger_type,'deferrable',is_deferrable,
  'initially_deferred',is_initially_deferred,'function_schema',function_schema,
  'function_name',function_name) ORDER BY trigger_name) FROM observed_triggers),'[]'::json),
 'company_reporting_schema_acls',COALESCE((SELECT json_agg(json_build_object(
  'schema',schema_name,'grantee',grantee,'privilege',privilege,'grantable',grantable)
  ORDER BY grantee,privilege) FROM schema_acls),'[]'::json),
 'company_reporting_function_acls',COALESCE((SELECT json_agg(json_build_object(
  'schema',schema_name,'name',function_name,'identity_arguments',identity_arguments,
  'grantee',grantee,'privilege',privilege,'grantable',grantable)
  ORDER BY function_name,identity_arguments,grantee,privilege) FROM function_acls),'[]'::json),
 'company_reporting_effective_schema_privileges',COALESCE((SELECT json_agg(
  json_build_object('role',role,'schema',schema_name,'usage',can_use,'create',can_create)
  ORDER BY role,schema_name) FROM schema_privileges),'[]'::json),
 'company_reporting_effective_function_privileges',COALESCE((SELECT json_agg(
  json_build_object('role',role,'schema',schema_name,'name',function_name,
  'identity_arguments',identity_arguments,'execute',can_execute)
  ORDER BY role,function_name,identity_arguments) FROM function_privileges),'[]'::json)
)::text;
    """.replace("__R1_ROLE_SQL__", _COMPANY_REPORTING_ROLES_SQL)
    .replace("__COMPANY_REPORTING_FUNCTIONS_SQL__", _COMPANY_REPORTING_FUNCTIONS_SQL)
    .replace("__COMPANY_REPORTING_TABLES_SQL__", _COMPANY_REPORTING_TABLES_SQL)
    .replace("__COMPANY_REPORTING_COLUMNS_SQL__", _COMPANY_REPORTING_COLUMNS_SQL)
    .replace("__COMPANY_REPORTING_TRIGGERS_SQL__", _COMPANY_REPORTING_TRIGGERS_SQL)
    .strip()
)

_R1_PUBLIC_TABLE_SQL = ", ".join(f"'{name}'" for name in R1_PUBLIC_TABLES)
_R1_VIEW_SQL = ", ".join(f"'{name}'" for name in R1_INTERNAL_READ_VIEWS)
_R1_FUNCTION_SQL = ", ".join(
    f"('{name}', '{identity_arguments}')"
    for name, identity_arguments in R1_INTERNAL_READ_FUNCTION_SIGNATURES.items()
)
_R1_ROLE_SQL = ", ".join(f"('{name}'::name)" for name in R1_CONTROLLED_ROLES)

# This query intentionally records both catalog ACLs and effective privileges.
# ACL arrays alone do not show inherited PUBLIC grants, and information_schema
# omits denied/empty rows.  Restore verification therefore compares the matrix
# generated by has_*_privilege() as well as the raw default ACL observations.
R1_SECURITY_SQL = (
    ""  # nosec B608 - placeholders are replaced only from fixed allowlists.
    """
WITH expected_roles(role_name) AS (
    VALUES __R1_ROLE_SQL__
), expected_r1_functions(function_name, identity_arguments) AS (
    VALUES __R1_FUNCTION_SQL__
), database_owner AS (
    SELECT pg_get_userbyid(datdba) AS role_name
      FROM pg_database
     WHERE datname = current_database()
), observed_roles(role_name) AS (
    SELECT role_name FROM expected_roles
    UNION
    SELECT role_name FROM database_owner
), present_roles(role_name) AS (
    SELECT e.role_name
      FROM expected_roles AS e
      JOIN pg_roles AS r ON r.rolname = e.role_name
), r1_objects(schema_name, object_name, object_kind) AS (
    SELECT 'public'::text, object_name, 'table'::text
      FROM unnest(ARRAY[__R1_PUBLIC_TABLE_SQL__]::text[]) AS object(object_name)
    UNION ALL
    SELECT 'internal_read'::text, object_name, 'view'::text
      FROM unnest(ARRAY[__R1_VIEW_SQL__]::text[]) AS object(object_name)
    UNION ALL
    SELECT 'internal_read'::text, 'evidence_read_receipt', 'table'::text
), role_matrix AS (
    SELECT COALESCE(json_agg(json_build_object(
        'role', r.rolname,
        'login', r.rolcanlogin,
        'superuser', r.rolsuper,
        'create_database', r.rolcreatedb,
        'create_role', r.rolcreaterole,
        'inherit', r.rolinherit,
        'replication', r.rolreplication,
        'bypass_rls', r.rolbypassrls,
        'memberships', COALESCE((
            SELECT json_agg(json_build_object(
                'direction', CASE WHEN m.member = r.oid THEN 'member' ELSE 'granted' END,
                'role', CASE WHEN m.member = r.oid
                            THEN pg_get_userbyid(m.roleid)
                            ELSE pg_get_userbyid(m.member) END,
                'admin_option', m.admin_option
            ) ORDER BY m.roleid, m.member)
              FROM pg_auth_members AS m
             WHERE m.member = r.oid OR m.roleid = r.oid
        ), '[]'::json)
    ) ORDER BY r.rolname), '[]'::json) AS value
      FROM observed_roles AS e
      JOIN pg_roles AS r ON r.rolname = e.role_name
), database_acl AS (
    SELECT COALESCE(json_agg(json_build_object(
        'grantee', CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
        'privilege', a.privilege_type,
        'grantable', a.is_grantable
    ) ORDER BY a.grantee, a.privilege_type), '[]'::json) AS value
      FROM pg_database AS d
      CROSS JOIN LATERAL aclexplode(COALESCE(d.datacl, '{}'::aclitem[])) AS a
     WHERE d.datname = current_database()
), schema_acl AS (
    SELECT COALESCE(json_agg(json_build_object(
        'schema', n.nspname,
        'grantee', CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
        'privilege', a.privilege_type,
        'grantable', a.is_grantable
    ) ORDER BY n.nspname, a.grantee, a.privilege_type), '[]'::json) AS value
      FROM pg_namespace AS n
      CROSS JOIN LATERAL aclexplode(COALESCE(n.nspacl, '{}'::aclitem[])) AS a
     WHERE n.nspname IN ('public', 'internal_read')
), default_acl AS (
    SELECT COALESCE(json_agg(json_build_object(
        'owner', pg_get_userbyid(d.defaclrole),
        'schema', COALESCE(n.nspname, ''),
        'object_type', d.defaclobjtype,
        'grantee', CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
        'privilege', a.privilege_type,
        'grantable', a.is_grantable
    ) ORDER BY d.defaclrole, d.defaclobjtype, n.nspname, a.grantee, a.privilege_type), '[]'::json) AS value
      FROM pg_default_acl AS d
      LEFT JOIN pg_namespace AS n ON n.oid = d.defaclnamespace
      CROSS JOIN LATERAL aclexplode(COALESCE(d.defaclacl, '{}'::aclitem[])) AS a
     WHERE d.defaclnamespace = 0
        OR n.nspname IN ('public', 'internal_read', 'internal_import', 'internal_command')
), r1_constraints AS (
    SELECT COALESCE(json_agg(json_build_object(
        'schema', n.nspname,
        'table', c.relname,
        'name', con.conname,
        'type', con.contype,
        'deferrable', con.condeferrable,
        'initially_deferred', con.condeferred,
        'validated', con.convalidated
    ) ORDER BY n.nspname, c.relname, con.conname), '[]'::json) AS value
      FROM pg_constraint AS con
      JOIN pg_class AS c ON c.oid = con.conrelid
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
     WHERE (n.nspname = 'public' AND c.relname IN (__R1_PUBLIC_TABLE_SQL__))
        OR (n.nspname = 'internal_read' AND c.relname = 'evidence_read_receipt')
), r1_triggers AS (
    SELECT COALESCE(json_agg(json_build_object(
        'schema', n.nspname,
        'table', c.relname,
        'name', t.tgname,
        'enabled', t.tgenabled,
        'constraint', t.tgconstraint <> 0
    ) ORDER BY n.nspname, c.relname, t.tgname), '[]'::json) AS value
      FROM pg_trigger AS t
      JOIN pg_class AS c ON c.oid = t.tgrelid
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
     WHERE NOT t.tgisinternal
       AND ((n.nspname = 'public' AND c.relname IN (__R1_PUBLIC_TABLE_SQL__))
         OR (n.nspname = 'internal_read' AND c.relname = 'evidence_read_receipt'))
), r1_views AS (
    SELECT COALESCE(json_agg(json_build_object(
        'schema', n.nspname,
        'name', c.relname,
        'security_barrier', 'security_barrier=true' = ANY(COALESCE(c.reloptions, '{}')),
        'security_invoker', 'security_invoker=true' = ANY(COALESCE(c.reloptions, '{}')),
        'owner', pg_get_userbyid(c.relowner)
    ) ORDER BY c.relname), '[]'::json) AS value
      FROM pg_class AS c
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
     WHERE n.nspname = 'internal_read' AND c.relkind = 'v'
       AND c.relname IN (__R1_VIEW_SQL__)
), r1_functions AS (
    SELECT COALESCE(json_agg(json_build_object(
        'schema', n.nspname,
        'name', p.proname,
        'identity_arguments', pg_get_function_identity_arguments(p.oid),
        'owner', pg_get_userbyid(p.proowner),
        'security_definer', p.prosecdef,
        'proconfig', COALESCE(to_json(p.proconfig), '[]'::json)
    ) ORDER BY n.nspname, p.proname, pg_get_function_identity_arguments(p.oid)), '[]'::json) AS value
     FROM pg_proc AS p
      JOIN pg_namespace AS n ON n.oid = p.pronamespace
     WHERE (n.nspname = 'internal_read' AND (
                -- Exact allowlist matches are the normal observation path.
                EXISTS (
                    SELECT 1
                      FROM expected_r1_functions AS expected
                     WHERE expected.function_name = p.proname
                       AND expected.identity_arguments = pg_get_function_identity_arguments(p.oid)
                )
                -- Keep same-name signature drift visible so the verifier can
                -- reject a wrong signature or an extra overload fail-closed.
                OR EXISTS (
                    SELECT 1
                      FROM expected_r1_functions AS expected
                     WHERE expected.function_name = p.proname
                )
            ))
        OR (n.nspname = 'public' AND p.proname LIKE 'r1_%')
), effective_table_privileges AS (
    SELECT COALESCE(json_agg(json_build_object(
        'role', e.role_name,
        'schema', o.schema_name,
        'object', o.object_name,
        'kind', o.object_kind,
        'select', has_table_privilege(e.role_name::text, format('%I.%I', o.schema_name, o.object_name), 'SELECT'),
        'insert', has_table_privilege(e.role_name::text, format('%I.%I', o.schema_name, o.object_name), 'INSERT'),
        'update', has_table_privilege(e.role_name::text, format('%I.%I', o.schema_name, o.object_name), 'UPDATE'),
        'delete', has_table_privilege(e.role_name::text, format('%I.%I', o.schema_name, o.object_name), 'DELETE'),
        'truncate', has_table_privilege(e.role_name::text, format('%I.%I', o.schema_name, o.object_name), 'TRUNCATE'),
        'references', has_table_privilege(e.role_name::text, format('%I.%I', o.schema_name, o.object_name), 'REFERENCES'),
        'trigger', has_table_privilege(e.role_name::text, format('%I.%I', o.schema_name, o.object_name), 'TRIGGER')
    ) ORDER BY e.role_name, o.schema_name, o.object_name), '[]'::json) AS value
      FROM present_roles AS e CROSS JOIN r1_objects AS o
), effective_function_privileges AS (
    SELECT COALESCE(json_agg(json_build_object(
        'role', e.role_name,
        'schema', n.nspname,
        'name', p.proname,
        'identity_arguments', pg_get_function_identity_arguments(p.oid),
        'execute', has_function_privilege(e.role_name::text, p.oid, 'EXECUTE')
    ) ORDER BY e.role_name, n.nspname, p.proname, pg_get_function_identity_arguments(p.oid)), '[]'::json) AS value
      FROM present_roles AS e
      CROSS JOIN pg_proc AS p
      JOIN pg_namespace AS n ON n.oid = p.pronamespace
     WHERE (n.nspname = 'internal_read' AND (
                EXISTS (
                    SELECT 1
                      FROM expected_r1_functions AS expected
                     WHERE expected.function_name = p.proname
                       AND expected.identity_arguments = pg_get_function_identity_arguments(p.oid)
                )
                OR EXISTS (
                    SELECT 1
                      FROM expected_r1_functions AS expected
                     WHERE expected.function_name = p.proname
                )
            ))
        OR (n.nspname = 'public' AND p.proname LIKE 'r1_%')
), effective_schema_privileges AS (
    SELECT COALESCE(json_agg(json_build_object(
        'role', e.role_name,
        'schema', s.schema_name,
        'usage', has_schema_privilege(e.role_name::text, s.schema_name, 'USAGE'),
        'create', has_schema_privilege(e.role_name::text, s.schema_name, 'CREATE')
    ) ORDER BY e.role_name, s.schema_name), '[]'::json) AS value
      FROM present_roles AS e
      CROSS JOIN (VALUES ('public'::text), ('internal_read'::text)) AS s(schema_name)
)
SELECT json_build_object(
    'r1_role_matrix', (SELECT value FROM role_matrix),
    'r1_database_acl', (SELECT value FROM database_acl),
    'r1_schema_acl', (SELECT value FROM schema_acl),
    'r1_default_acls', (SELECT value FROM default_acl),
    'r1_constraints', (SELECT value FROM r1_constraints),
    'r1_triggers', (SELECT value FROM r1_triggers),
    'r1_views', (SELECT value FROM r1_views),
    'r1_functions', (SELECT value FROM r1_functions),
    'r1_effective_table_privileges', (SELECT value FROM effective_table_privileges),
    'r1_effective_function_privileges', (SELECT value FROM effective_function_privileges),
    'r1_effective_schema_privileges', (SELECT value FROM effective_schema_privileges)
)::text;
    """.replace("__R1_PUBLIC_TABLE_SQL__", _R1_PUBLIC_TABLE_SQL)
    .replace("__R1_VIEW_SQL__", _R1_VIEW_SQL)
    .replace("__R1_FUNCTION_SQL__", _R1_FUNCTION_SQL)
    .replace("__R1_ROLE_SQL__", _R1_ROLE_SQL)
    .strip()
)

PHASE_1_TABLES = ("entity", "account", "journal_entry", "posting", "audit_event")
PHASE_2_TABLES = ("raw_artifact", "import_job", "source_record")
PHASE_3_TABLES = ("ingest_channel", "source_system")
ROW_COUNT_SQL = {
    PHASE_1_TABLES: """
        SELECT json_build_object(
            'entity', (SELECT count(*) FROM public.entity),
            'account', (SELECT count(*) FROM public.account),
            'journal_entry', (SELECT count(*) FROM public.journal_entry),
            'posting', (SELECT count(*) FROM public.posting),
            'audit_event', (SELECT count(*) FROM public.audit_event)
        )::text;
    """.strip(),
    PHASE_1_TABLES + PHASE_2_TABLES: """
        SELECT json_build_object(
            'entity', (SELECT count(*) FROM public.entity),
            'account', (SELECT count(*) FROM public.account),
            'journal_entry', (SELECT count(*) FROM public.journal_entry),
            'posting', (SELECT count(*) FROM public.posting),
            'audit_event', (SELECT count(*) FROM public.audit_event),
            'raw_artifact', (SELECT count(*) FROM public.raw_artifact),
            'import_job', (SELECT count(*) FROM public.import_job),
            'source_record', (SELECT count(*) FROM public.source_record)
        )::text;
    """.strip(),
    PHASE_1_TABLES + PHASE_2_TABLES + PHASE_3_TABLES: """
        SELECT json_build_object(
            'entity', (SELECT count(*) FROM public.entity),
            'account', (SELECT count(*) FROM public.account),
            'journal_entry', (SELECT count(*) FROM public.journal_entry),
            'posting', (SELECT count(*) FROM public.posting),
            'audit_event', (SELECT count(*) FROM public.audit_event),
            'raw_artifact', (SELECT count(*) FROM public.raw_artifact),
            'import_job', (SELECT count(*) FROM public.import_job),
            'source_record', (SELECT count(*) FROM public.source_record),
            'ingest_channel', (SELECT count(*) FROM public.ingest_channel),
            'source_system', (SELECT count(*) FROM public.source_system)
        )::text;
    """.strip(),
}
PHASE_1_FUNCTIONS = frozenset(
    {
        "account_block_protected_dimension_change",
        "append_audit_event",
        "audit_event_block_mutation",
        "journal_entry_assert_posted_complete",
        "journal_entry_block_posted_mutation",
        "journal_entry_validate_relationships",
        "posting_assert_balanced",
        "posting_block_posted_mutation",
        "posting_enforce_entity",
    }
)
PHASE_2_FUNCTIONS = frozenset(
    {
        "import_job_enforce_transition",
        "journal_entry_validate_post_audit",
        "raw_artifact_block_mutation",
        "raw_artifact_validate_audit",
        "source_record_block_mutation",
    }
)
PHASE_3_FUNCTIONS = frozenset({"registry_block_mutation"})
PHASE_1_TRIGGERS = frozenset(
    {
        "account_protected_dimensions_immutable",
        "audit_event_no_update_delete",
        "journal_entry_validate_correction",
        "journal_entry_posted_immutable",
        "posting_entity_boundary",
        "posting_posted_immutable",
        "posting_balanced_per_currency",
        "journal_entry_posted_complete",
    }
)
PHASE_2_TRIGGERS = frozenset(
    {
        "raw_artifact_no_update_delete",
        "raw_artifact_audit_binding",
        "source_record_no_update_delete",
        "import_job_state_machine",
        "journal_entry_post_audit_binding",
    }
)
PHASE_3_TRIGGERS = frozenset(
    {
        "ingest_channel_no_update_delete",
        "source_system_no_update_delete",
        "import_job_terminal_audit_binding",
    }
)
PHASE_1_TABLE_PRIVILEGES = frozenset(
    (table, privilege)
    for table in ("entity", "account", "journal_entry", "posting")
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
) | {("audit_event", "SELECT")}
PHASE_2_TABLE_PRIVILEGES = frozenset(
    {
        ("raw_artifact", "SELECT"),
        ("raw_artifact", "INSERT"),
        ("source_record", "SELECT"),
        ("source_record", "INSERT"),
        ("import_job", "SELECT"),
        ("import_job", "INSERT"),
    }
)
PHASE_2_COLUMN_PRIVILEGES = frozenset(
    ("import_job", column, "UPDATE")
    for column in (
        "status",
        "started_at",
        "completed_at",
        "parsed_count",
        "created_count",
        "duplicate_count",
        "error_code",
        "diagnostic_summary",
    )
)
PHASE_3_COLUMN_PRIVILEGES = frozenset({("import_job", "terminal_audit_event_id", "UPDATE")})
PHASE_3_TABLE_PRIVILEGES = frozenset({("ingest_channel", "SELECT"), ("source_system", "SELECT")})
RUNTIME_IDENTITY_PROGRAM = (
    "import os; "
    "from sqlalchemy import create_engine, text; "
    "engine=create_engine(os.environ['LEDGERBRIDGE_DATABASE_URL']); "
    "connection=engine.connect(); "
    "row=connection.execute(text('SELECT session_user, current_user')).one(); "
    "print('|'.join(row)); "
    "connection.close(); engine.dispose()"
)


class BackupError(RuntimeError):
    """Raised when backup or restore safety validation fails."""


@dataclass(frozen=True, slots=True)
class CutoverInventory:
    """Count-only inventory used inside an isolated restore session."""

    schema_revision: str
    candidate_total: int
    latest_pending_candidates: int
    audit_events: int
    row_counts: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if self.schema_revision not in MYBANK_CUTOVER_SCHEMA_REVISIONS:
            raise BackupError("cutover inventory schema revision is invalid")
        scalar_counts = (
            self.candidate_total,
            self.latest_pending_candidates,
            self.audit_events,
        )
        if any(type(value) is not int or value < 0 for value in scalar_counts):
            raise BackupError("cutover inventory scalar count is invalid")
        if (
            tuple(sorted(self.row_counts)) != self.row_counts
            or len({name for name, _ in self.row_counts}) != len(self.row_counts)
            or {name for name, _ in self.row_counts} != set(R1_CUTOVER_INVENTORY_TABLES)
            or any(type(value) is not int or value < 0 for _, value in self.row_counts)
        ):
            raise BackupError("cutover inventory row counts are invalid")

    @classmethod
    def from_payload(cls, payload: object) -> CutoverInventory:
        if not isinstance(payload, dict) or set(payload) != {
            "schema_revision",
            "candidate_total",
            "latest_pending_candidates",
            "audit_events",
            "row_counts",
        }:
            raise BackupError("cutover inventory payload is invalid")
        row_counts = payload.get("row_counts")
        if not isinstance(row_counts, dict):
            raise BackupError("cutover inventory payload is invalid")
        values = tuple(sorted((str(name), value) for name, value in row_counts.items()))
        return cls(
            schema_revision=cast(str, payload.get("schema_revision")),
            candidate_total=cast(int, payload.get("candidate_total")),
            latest_pending_candidates=cast(int, payload.get("latest_pending_candidates")),
            audit_events=cast(int, payload.get("audit_events")),
            row_counts=cast(tuple[tuple[str, int], ...], values),
        )

    def count(self, table: str) -> int:
        return dict(self.row_counts)[table]

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_revision": self.schema_revision,
            "candidate_total": self.candidate_total,
            "latest_pending_candidates": self.latest_pending_candidates,
            "audit_events": self.audit_events,
            "row_counts": dict(self.row_counts),
        }


def validate_mybank_cutover_inventory_sequence(
    *,
    before: CutoverInventory,
    after: CutoverInventory,
    replay: CutoverInventory,
    conflict: CutoverInventory,
    transaction_count: int,
    alias_count: int,
    assignment_count: int,
) -> dict[str, int]:
    """Prove the fixed whole-statement delta and two zero-delta follow-ups."""

    if type(transaction_count) is not int or transaction_count <= 0:
        raise BackupError("cutover transaction count is invalid")
    if type(alias_count) is not int or alias_count <= 0:
        raise BackupError("cutover account alias count is invalid")
    if type(assignment_count) is not int or assignment_count < 0:
        raise BackupError("cutover account assignment count is invalid")
    if any(
        item.candidate_total != before.candidate_total
        or item.latest_pending_candidates != before.latest_pending_candidates
        for item in (after, replay, conflict)
    ):
        raise BackupError("cutover candidate inventory changed")
    expected_deltas = {
        "evidence_object": 1,
        "encrypted_object_identity": 1,
        "encrypted_blob_version": 1,
        "managed_account": 1,
        "managed_account_lifecycle": 1,
        "account_registry_operation": 1,
        "managed_account_alias": alias_count,
        "account_business_unit_assignment": assignment_count,
        "bank_statement": 1,
        "bank_statement_transaction": transaction_count,
        "bank_statement_observation": transaction_count,
        "bank_statement_review": 1,
    }
    before_counts = dict(before.row_counts)
    after_counts = dict(after.row_counts)
    for table in R1_CUTOVER_INVENTORY_TABLES:
        observed = after_counts[table] - before_counts[table]
        expected = expected_deltas.get(table, 0)
        if observed != expected:
            label = "required" if table in expected_deltas else "unrelated"
            raise BackupError(f"cutover {label} table inventory changed: {table}")
    expected_audit_delta = 7 + alias_count + assignment_count + 2 * transaction_count
    if after.audit_events - before.audit_events != expected_audit_delta:
        raise BackupError("cutover audit inventory changed unexpectedly")
    if replay != after:
        raise BackupError("cutover replay changed inventory")
    if conflict != after:
        raise BackupError("cutover conflict probe changed inventory")
    return {
        "candidate_total": after.candidate_total,
        "latest_pending_candidates": after.latest_pending_candidates,
        "audit_event_delta": expected_audit_delta,
        "transaction_count": transaction_count,
        "alias_count": alias_count,
        "assignment_count": assignment_count,
        "replay_delta": 0,
        "conflict_delta": 0,
    }


class Runner:
    """Subprocess adapter that never puts secret values in command arguments."""

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        stdin_path: Path | None = None,
        stdout_path: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        with contextlib.ExitStack() as stack:
            stdin = stack.enter_context(stdin_path.open("rb")) if stdin_path else None
            stdout: int | Any
            if stdout_path is None:
                stdout = subprocess.PIPE
            else:
                stdout = stack.enter_context(stdout_path.open("wb"))
            result = subprocess.run(  # nosec B603
                args,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.PIPE,
                check=False,
            )
        if check and result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[-2000:]
            command = " ".join(args[:2])
            raise BackupError(f"command failed ({command}): {stderr.strip()}")
        return result

    def capture(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> str:
        result = self._run(args, cwd=cwd, env=env, check=check)
        return (result.stdout or b"").decode("utf-8", errors="strict").strip()

    def succeeds(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> bool:
        return self._run(args, cwd=cwd, env=env, check=False).returncode == 0

    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        stdin_path: Path | None = None,
        stdout_path: Path | None = None,
        check: bool = True,
    ) -> None:
        self._run(
            args,
            cwd=cwd,
            env=env,
            stdin_path=stdin_path,
            stdout_path=stdout_path,
            check=check,
        )


@dataclass(frozen=True)
class CommonConfig:
    project_dir: Path
    backup_root: Path
    work_root: Path
    gpg_home: Path
    fingerprint: str
    postgres_image: str = POSTGRES_IMAGE


@dataclass(frozen=True)
class SourceState:
    revision: str
    postgres_container: str
    api_container: str
    worker_container: str
    api_image: str
    api_image_id: str
    artifact_volume: str
    database: dict[str, Any]


@dataclass(frozen=True)
class RestoreResources:
    suffix: str
    container: str
    network: str
    database_volume: str
    artifact_volume: str

    @classmethod
    def create(cls, suffix: str) -> RestoreResources:
        if SUFFIX_PATTERN.fullmatch(suffix) is None:
            raise BackupError("restore suffix must be exactly eight lowercase hex characters")
        return cls(
            suffix=suffix,
            container=f"ledgerbridge-restore-postgres-{suffix}",
            network=f"ledgerbridge-restore-network-{suffix}",
            database_volume=f"ledgerbridge_restore_db_{suffix}",
            artifact_volume=f"ledgerbridge_restore_artifacts_{suffix}",
        )


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(moment: datetime | None = None) -> str:
    return (moment or _now()).strftime("%Y%m%dT%H%M%SZ")


def _validate_absolute_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise BackupError(f"{label} must be an absolute path")
    if path.is_symlink():
        raise BackupError(f"{label} may not be a symlink")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or resolved == Path(resolved.anchor):
        raise BackupError(f"{label} must be a non-root directory")
    return resolved


def _validate_secure_directory(path: Path, label: str) -> Path:
    resolved = _validate_absolute_directory(path, label)
    mode = stat.S_IMODE(resolved.stat().st_mode)
    if mode & 0o077:
        raise BackupError(f"{label} must not be accessible by group or other users")
    return resolved


def _validate_work_root(path: Path) -> Path:
    resolved = _validate_absolute_directory(path, "plaintext work root")
    mode = resolved.stat().st_mode
    if mode & 0o022 and not mode & stat.S_ISVTX:
        raise BackupError("writable plaintext work root must have the sticky bit")
    return resolved


def _validate_private_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise BackupError(f"{label} must be a regular non-symlink file")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise BackupError(f"{label} must not be accessible by group or other users")
    return path


def _normalize_fingerprint(value: str) -> str:
    normalized = value.replace(" ", "").upper()
    if FINGERPRINT_PATTERN.fullmatch(normalized) is None:
        raise BackupError("GPG fingerprint must be 40-64 hexadecimal characters")
    return normalized


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    path.chmod(0o600)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BackupError(f"{label} must contain a JSON object")
    return cast(dict[str, Any], value)


def _parse_env(path: Path) -> dict[str, str]:
    _validate_private_file(path, "deployment .env")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator != "=" or not key or key in values:
            raise BackupError("deployment .env contains an invalid or duplicate entry")
        values[key] = value
    return values


def _replace_database_host(url: str, host: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or parsed.username is None or parsed.password is None or not parsed.path:
        raise BackupError("runtime database URL is incomplete")
    netloc = (
        f"{quote(unquote(parsed.username), safe='')}:"
        f"{quote(unquote(parsed.password), safe='')}@{host}"
    )
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _compose(runner: Runner, project_dir: Path, *args: str, check: bool = True) -> str:
    return runner.capture(["docker", "compose", *args], cwd=project_dir, check=check)


def _container_for(runner: Runner, project_dir: Path, service: str) -> str:
    container = _compose(runner, project_dir, "ps", "-q", service)
    if not container:
        raise BackupError(f"Compose service is missing: {service}")
    return container


def _container_health(runner: Runner, container: str) -> str:
    return runner.capture(["docker", "inspect", "--format", "{{.State.Health.Status}}", container])


def _database_metadata(
    runner: Runner, container: str, database: str | None = None
) -> dict[str, Any]:
    if database is None:
        command = (
            'exec psql --no-psqlrc --username "$POSTGRES_USER" '
            '--dbname "$POSTGRES_DB" -At -v ON_ERROR_STOP=1 -c "$1"'
        )
    else:
        command = (
            "exec psql --no-psqlrc --username postgres "
            f'--dbname "{database}" -At -v ON_ERROR_STOP=1 -c "$1"'
        )

    def query(sql: str) -> object:
        output = runner.capture(["docker", "exec", container, "sh", "-c", command, "sh", sql])
        return json.loads(output)

    value = query(DATABASE_METADATA_SQL)
    if not isinstance(value, dict):
        raise BackupError("database metadata query did not return a JSON object")
    metadata = cast(dict[str, Any], value)
    revision = metadata.get("alembic_version")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9]{8}_[0-9]{4}", revision) is None:
        raise BackupError("database metadata has an invalid Alembic revision")
    tables = list(PHASE_1_TABLES)
    if revision >= "20260821_0003":
        tables.extend(PHASE_2_TABLES)
    if revision >= "20260822_0004":
        tables.extend(PHASE_3_TABLES)
    table_key = tuple(tables)
    row_counts = query(ROW_COUNT_SQL[table_key])
    if not isinstance(row_counts, dict) or set(row_counts) != set(tables):
        raise BackupError("database row-count query returned an invalid object")
    security = query(DATABASE_SECURITY_SQL)
    if not isinstance(security, dict):
        raise BackupError("database security query returned an invalid object")
    metadata["metadata_version"] = 2
    metadata["row_counts"] = row_counts
    metadata.update(cast(dict[str, Any], security))
    if revision >= R1_SECURITY_REVISION:
        r1_security = query(R1_SECURITY_SQL)
        if not isinstance(r1_security, dict):
            raise BackupError("R1 security query returned an invalid object")
        required_r1_keys = {
            "r1_role_matrix",
            "r1_database_acl",
            "r1_schema_acl",
            "r1_default_acls",
            "r1_constraints",
            "r1_triggers",
            "r1_views",
            "r1_functions",
            "r1_effective_table_privileges",
            "r1_effective_function_privileges",
            "r1_effective_schema_privileges",
        }
        if set(r1_security) != required_r1_keys:
            raise BackupError("R1 security query returned an incomplete object")
        metadata.update(cast(dict[str, Any], r1_security))
    if revision >= COUNTERPARTY_SECURITY_REVISION:
        counterparty_security = query(COUNTERPARTY_SECURITY_SQL)
        required_counterparty_keys = {
            "counterparty_row_counts",
            "counterparty_tables",
            "counterparty_functions",
            "counterparty_triggers",
            "counterparty_constraints",
            "counterparty_table_acls",
            "counterparty_function_acls",
            "counterparty_effective_table_privileges",
            "counterparty_effective_function_privileges",
        }
        if (
            not isinstance(counterparty_security, dict)
            or set(counterparty_security) != required_counterparty_keys
        ):
            raise BackupError("counterparty security query returned an incomplete object")
        metadata.update(cast(dict[str, Any], counterparty_security))
    if revision >= BANK_STATEMENT_SECURITY_REVISION:
        bank_security = query(BANK_STATEMENT_SECURITY_SQL)
        required_bank_keys = {
            "bank_statement_row_counts",
            "bank_statement_tables",
            "bank_statement_schemas",
            "bank_statement_functions",
            "bank_statement_triggers",
            "bank_statement_constraints",
            "bank_statement_table_acls",
            "bank_statement_function_acls",
            "bank_statement_schema_acls",
            "bank_statement_effective_table_privileges",
            "bank_statement_effective_function_privileges",
            "bank_statement_effective_schema_privileges",
        }
        if not isinstance(bank_security, dict) or set(bank_security) != required_bank_keys:
            raise BackupError("bank statement security query returned an incomplete object")
        metadata.update(cast(dict[str, Any], bank_security))
    if revision >= ACCOUNT_REGISTRY_SECURITY_REVISION:
        registry_security = query(ACCOUNT_REGISTRY_SECURITY_SQL)
        required_registry_keys = {
            "account_registry_row_counts",
            "account_registry_tables",
            "account_registry_functions",
            "account_registry_triggers",
            "account_registry_constraints",
            "account_registry_table_acls",
            "account_registry_function_acls",
            "account_registry_effective_table_privileges",
            "account_registry_effective_function_privileges",
        }
        if (
            not isinstance(registry_security, dict)
            or set(registry_security) != required_registry_keys
        ):
            raise BackupError("account registry security query returned an incomplete object")
        metadata.update(cast(dict[str, Any], registry_security))
        cutover_inventory = CutoverInventory.from_payload(query(R1_CUTOVER_INVENTORY_SQL))
        metadata["cutover_inventory"] = cutover_inventory.as_payload()
    if revision >= COMPANY_REPORTING_SECURITY_REVISION:
        company_reporting_security = query(COMPANY_REPORTING_SECURITY_SQL)
        required_company_reporting_keys = {
            "company_reporting_schema",
            "company_reporting_functions",
            "company_reporting_required_tables",
            "company_reporting_required_columns",
            "company_reporting_required_functions",
            "company_reporting_triggers",
            "company_reporting_schema_acls",
            "company_reporting_function_acls",
            "company_reporting_effective_schema_privileges",
            "company_reporting_effective_function_privileges",
        }
        if (
            not isinstance(company_reporting_security, dict)
            or set(company_reporting_security) != required_company_reporting_keys
        ):
            raise BackupError("company reporting security query returned an incomplete object")
        metadata.update(cast(dict[str, Any], company_reporting_security))
    if revision >= EVIDENCE_UNLOCK_SECURITY_REVISION:
        evidence_unlock_security = query(EVIDENCE_UNLOCK_SECURITY_SQL)
        required_evidence_unlock_keys = {
            "evidence_unlock_row_counts",
            "evidence_unlock_tables",
            "evidence_unlock_functions",
            "evidence_unlock_triggers",
            "evidence_unlock_table_acls",
            "evidence_unlock_function_acls",
            "evidence_unlock_effective_table_privileges",
            "evidence_unlock_effective_function_privileges",
        }
        if (
            not isinstance(evidence_unlock_security, dict)
            or set(evidence_unlock_security) != required_evidence_unlock_keys
        ):
            raise BackupError("evidence unlock security query returned an incomplete object")
        metadata.update(cast(dict[str, Any], evidence_unlock_security))
    if revision >= "20260821_0003":
        artifact_sql = (
            R1_ARTIFACT_MANIFEST_SQL if revision >= "20260824_0012" else ARTIFACT_MANIFEST_SQL
        )
        artifact_manifest = query(artifact_sql)
        if not isinstance(artifact_manifest, dict):
            raise BackupError("artifact manifest query returned an invalid object")
        metadata.update(cast(dict[str, Any], artifact_manifest))
    return metadata


def _verify_gpg_key(runner: Runner, home: Path, fingerprint: str) -> None:
    output = runner.capture(
        [
            "gpg",
            "--homedir",
            str(home),
            "--batch",
            "--with-colons",
            "--list-secret-keys",
            fingerprint,
        ]
    )
    fingerprints = {
        fields[9].upper()
        for line in output.splitlines()
        if (fields := line.split(":"))[0] == "fpr" and len(fields) > 9
    }
    if fingerprint not in fingerprints:
        raise BackupError("the requested GPG secret key is not present")


def _verify_deployment_manifest(
    runner: Runner,
    verifier_project_dir: Path,
    target_root: Path,
    revision: str,
) -> None:
    runner.run(
        [
            sys.executable,
            str(verifier_project_dir / "scripts" / "deployment_manifest.py"),
            "verify",
            "--root",
            str(target_root),
            "--manifest",
            "MANIFEST.sha256",
            "--expected-revision",
            revision,
        ]
    )


def _collect_source_state(config: CommonConfig, runner: Runner) -> SourceState:
    project_dir = config.project_dir
    revision = (project_dir / "DEPLOYED_REVISION").read_text(encoding="utf-8").strip()
    if REVISION_PATTERN.fullmatch(revision) is None:
        raise BackupError("DEPLOYED_REVISION is not a full lowercase Git SHA")
    _validate_private_file(project_dir / ".env", "deployment .env")
    _verify_deployment_manifest(runner, project_dir, project_dir, revision)
    postgres = _container_for(runner, project_dir, "postgres")
    api = _container_for(runner, project_dir, "api")
    worker = _container_for(runner, project_dir, "worker")
    for service, container in (("postgres", postgres), ("api", api), ("worker", worker)):
        if _container_health(runner, container) != "healthy":
            raise BackupError(f"production service is not healthy: {service}")
    image = runner.capture(["docker", "inspect", "--format", "{{.Config.Image}}", api])
    worker_image = runner.capture(["docker", "inspect", "--format", "{{.Config.Image}}", worker])
    image_id = runner.capture(["docker", "inspect", "--format", "{{.Image}}", api])
    worker_image_id = runner.capture(["docker", "inspect", "--format", "{{.Image}}", worker])
    image_revision = runner.capture(
        [
            "docker",
            "inspect",
            "--format",
            '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            api,
        ]
    )
    if image != worker_image or not image.startswith("ledgerbridge-app:"):
        raise BackupError("API and worker do not share one revision-tagged image")
    if image_id != worker_image_id:
        raise BackupError("API and worker do not share one immutable image ID")
    if image_revision != revision:
        raise BackupError("production image revision label does not match DEPLOYED_REVISION")
    _validate_backup_image(runner, image, image_id, revision)
    artifact_volume = runner.capture(
        [
            "docker",
            "inspect",
            "--format",
            (
                "{{range .Mounts}}{{if eq .Destination "
                '"/var/lib/ledgerbridge/artifacts"}}{{.Name}}{{end}}{{end}}'
            ),
            api,
        ]
    )
    if not artifact_volume:
        raise BackupError("artifact named volume was not found on the API container")
    return SourceState(
        revision=revision,
        postgres_container=postgres,
        api_container=api,
        worker_container=worker,
        api_image=image,
        api_image_id=image_id,
        artifact_volume=artifact_volume,
        database=_database_metadata(runner, postgres),
    )


def _write_payload_hashes(directory: Path) -> None:
    lines = [f"{_sha256(directory / name)}  {name}" for name in PAYLOAD_COMPONENTS]
    (directory / "PAYLOAD.sha256").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def _verify_payload_hashes(directory: Path) -> None:
    manifest = directory / "PAYLOAD.sha256"
    expected: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        pure = PurePosixPath(name)
        if (
            separator != "  "
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or pure.is_absolute()
            or ".." in pure.parts
            or name in expected
        ):
            raise BackupError("payload hash manifest is invalid")
        expected[name] = digest
    if set(expected) != set(PAYLOAD_COMPONENTS):
        raise BackupError("payload hash manifest has an unexpected file set")
    for name, digest in expected.items():
        if _sha256(directory / name) != digest:
            raise BackupError(f"payload component hash mismatch: {name}")


def _deterministic_artifact_tar(
    runner: Runner, *, image: str, volume: str, destination_dir: Path, output: str
) -> None:
    runner.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "DAC_READ_SEARCH",
            "--security-opt",
            "no-new-privileges",
            "-v",
            f"{volume}:/source:ro",
            "-v",
            f"{destination_dir}:/backup:rw",
            image,
            "tar",
            *TAR_NORMALIZATION,
            "-C",
            "/source",
            "-cf",
            f"/backup/{output}",
            ".",
        ]
    )


def _artifact_quota_config(project_dir: Path) -> dict[str, int]:
    environment = _parse_env(project_dir / ".env")
    defaults = {
        "per_artifact_max_bytes": 50 * 1024 * 1024,
        "published_max_bytes": 10 * 1024 * 1024 * 1024,
        "staging_max_bytes": 512 * 1024 * 1024,
        "staging_ttl_seconds": 60 * 60,
    }
    variables = {
        "per_artifact_max_bytes": "LEDGERBRIDGE_ARTIFACT_MAX_BYTES",
        "published_max_bytes": "LEDGERBRIDGE_ARTIFACT_TOTAL_MAX_BYTES",
        "staging_max_bytes": "LEDGERBRIDGE_ARTIFACT_STAGING_MAX_BYTES",
        "staging_ttl_seconds": "LEDGERBRIDGE_ARTIFACT_STAGING_TTL_SECONDS",
    }
    result: dict[str, int] = {}
    for field, variable in variables.items():
        raw = environment.get(variable, str(defaults[field]))
        try:
            value = int(raw)
        except ValueError as exc:
            raise BackupError(f"deployment quota setting is not an integer: {variable}") from exc
        if value <= 0 or value > 2**63 - 1:
            raise BackupError(f"deployment quota setting is outside its safe range: {variable}")
        result[field] = value
    return result


def _artifact_archive_metadata(archive: Path, quota: dict[str, int]) -> dict[str, object]:
    published_bytes = 0
    staging_bytes = 0
    artifact_entries: list[str] = []
    with tarfile.open(archive, mode="r:") as bundle:
        names: set[str] = set()
        for member in bundle.getmembers():
            pure = PurePosixPath(member.name)
            parts = tuple(part for part in pure.parts if part != ".")
            normalized = pure.as_posix()
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
                or normalized in names
            ):
                raise BackupError("artifact archive contains an unsafe entry")
            names.add(normalized)
            if member.isdir():
                if not _valid_artifact_archive_directory(parts):
                    raise BackupError("artifact archive contains an unexpected directory")
                continue
            if not member.isfile():
                raise BackupError("artifact archive contains an unexpected entry type")
            if parts == (".quota.lock",):
                continue
            if len(parts) == 2 and parts[0] == ".staging" and parts[1].startswith("artifact-"):
                if member.size > quota["per_artifact_max_bytes"]:
                    raise BackupError("artifact archive member exceeds per-artifact quota")
                staging_bytes += member.size
                continue
            if _valid_artifact_blob_parts(parts):
                if member.size > quota["per_artifact_max_bytes"]:
                    raise BackupError("artifact archive member exceeds per-artifact quota")
                stream = bundle.extractfile(member)
                if stream is None:
                    raise BackupError("artifact archive member could not be read")
                digest = hashlib.sha256()
                observed_size = 0
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    observed_size += len(chunk)
                if observed_size != member.size:
                    raise BackupError("artifact archive member size differs from tar metadata")
                first, second, expected_digest = parts[1:]
                actual_digest = digest.hexdigest()
                if not hmac.compare_digest(actual_digest, expected_digest):
                    raise BackupError("artifact archive member digest differs from its storage key")
                storage_key = f"sha256/{first}/{second}/{expected_digest}"
                artifact_entries.append(f"{expected_digest}:{member.size}:{storage_key}")
                published_bytes += observed_size
                continue
            raise BackupError("artifact archive contains an unexpected file")
    if published_bytes > quota["published_max_bytes"]:
        raise BackupError("artifact archive exceeds the configured published quota")
    if staging_bytes > quota["staging_max_bytes"]:
        raise BackupError("artifact archive exceeds the configured staging quota")
    return {
        "published_bytes": published_bytes,
        "staging_bytes": staging_bytes,
        "unsafe_entries": 0,
        "quota": quota,
        "artifact_count": len(artifact_entries),
        "artifact_manifest_sha256": hashlib.sha256(
            "\n".join(sorted(artifact_entries)).encode("utf-8")
        ).hexdigest(),
    }


def _valid_artifact_archive_directory(parts: tuple[str, ...]) -> bool:
    if not parts or parts in {(".staging",), ("sha256",)}:
        return True
    if parts[0] != "sha256" or len(parts) not in {2, 3}:
        return False
    return all(re.fullmatch(r"[0-9a-f]{2}", part) is not None for part in parts[1:])


def _valid_artifact_blob_parts(parts: tuple[str, ...]) -> bool:
    if len(parts) != 4 or parts[0] != "sha256":
        return False
    first, second, digest = parts[1:]
    return (
        re.fullmatch(r"[0-9a-f]{2}", first) is not None
        and re.fullmatch(r"[0-9a-f]{2}", second) is not None
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        and digest.startswith(first + second)
    )


def _create_plain_payload(
    config: CommonConfig, state: SourceState, work_dir: Path, runner: Runner
) -> Path:
    database_dump = work_dir / "database.dump"
    roles_dump = work_dir / "roles.sql"
    runner.run(
        [
            "docker",
            "exec",
            state.postgres_container,
            "sh",
            "-c",
            (
                'exec pg_dump --no-password --username "$POSTGRES_USER" '
                '--dbname "$POSTGRES_DB" --format=custom --create'
            ),
        ],
        stdout_path=database_dump,
    )
    runner.run(
        [
            "docker",
            "exec",
            state.postgres_container,
            "sh",
            "-c",
            ('exec pg_dumpall --no-password --username "$POSTGRES_USER" --roles-only'),
        ],
        stdout_path=roles_dump,
    )
    runner.run(
        ["docker", "exec", "-i", state.postgres_container, "pg_restore", "--list"],
        stdin_path=database_dump,
    )
    _deterministic_artifact_tar(
        runner,
        image=state.api_image_id,
        volume=state.artifact_volume,
        destination_dir=work_dir,
        output="artifacts.tar",
    )
    runner.run(
        [
            "tar",
            *TAR_NORMALIZATION,
            "-C",
            str(config.project_dir.parent),
            "-cf",
            str(work_dir / "deployment-tree.tar"),
            config.project_dir.name,
        ]
    )
    artifact_control = _artifact_archive_metadata(
        work_dir / "artifacts.tar",
        _artifact_quota_config(config.project_dir),
    )
    if (
        state.database.get("artifact_count") != artifact_control["artifact_count"]
        or state.database.get("artifact_manifest_sha256")
        != artifact_control["artifact_manifest_sha256"]
    ):
        raise BackupError("database artifact manifest differs from the artifact archive")
    metadata = {
        "format": BACKUP_FORMAT,
        "created_at": _now().isoformat(),
        "revision": state.revision,
        "api_image": state.api_image,
        "api_image_id": state.api_image_id,
        "artifact_volume": state.artifact_volume,
        "database": state.database,
        "artifact_control": artifact_control,
        "artifact_archive_sha256": _sha256(work_dir / "artifacts.tar"),
        "deployment_tree_sha256": _sha256(work_dir / "deployment-tree.tar"),
    }
    _write_json(work_dir / "metadata.json", metadata)
    _write_payload_hashes(work_dir)
    payload = work_dir / "payload.tar"
    runner.run(
        [
            "tar",
            *TAR_NORMALIZATION,
            "-C",
            str(work_dir),
            "-cf",
            str(payload),
            *PAYLOAD_COMPONENTS,
            "PAYLOAD.sha256",
        ]
    )
    return payload


def _assert_tree_has_no_symlinks(root: Path) -> None:
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            relative = candidate.relative_to(root).as_posix()
            raise BackupError(f"deployment tree contains a symlink: {relative}")


def _container_status(runner: Runner, container: str) -> str:
    return runner.capture(["docker", "inspect", "--format", "{{.State.Status}}", container])


def _wait_for_health(
    runner: Runner, container: str, *, expected: str = "healthy", timeout: int = 90
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _container_health(runner, container) == expected:
            return
        time.sleep(2)
    raise BackupError(f"container did not become {expected}: {container}")


def _restart_application(runner: Runner, state: SourceState) -> None:
    runner.run(["docker", "start", state.api_container, state.worker_container])
    _wait_for_health(runner, state.api_container)
    _wait_for_health(runner, state.worker_container)


def _assert_source_unchanged(
    before: SourceState, after: SourceState, *, require_container_identity: bool = True
) -> None:
    checks: tuple[tuple[str, object, object], ...] = (
        ("revision", before.revision, after.revision),
        ("API image", before.api_image, after.api_image),
        ("API image ID", before.api_image_id, after.api_image_id),
        ("artifact volume", before.artifact_volume, after.artifact_volume),
        ("database metadata", before.database, after.database),
    )
    if require_container_identity:
        checks += (
            ("Postgres container", before.postgres_container, after.postgres_container),
            ("API container", before.api_container, after.api_container),
            ("worker container", before.worker_container, after.worker_container),
        )
    changed = [label for label, old, new in checks if old != new]
    if changed:
        raise BackupError(f"production state changed unexpectedly: {', '.join(changed)}")


def _validated_config(config: CommonConfig, runner: Runner) -> CommonConfig:
    if config.postgres_image != POSTGRES_IMAGE:
        raise BackupError("restore rehearsal must use the repository-pinned PostgreSQL image")
    validated = CommonConfig(
        project_dir=_validate_absolute_directory(config.project_dir, "project directory"),
        backup_root=_validate_secure_directory(config.backup_root, "backup root"),
        work_root=_validate_work_root(config.work_root),
        gpg_home=_validate_secure_directory(config.gpg_home, "GPG home"),
        fingerprint=_normalize_fingerprint(config.fingerprint),
        postgres_image=config.postgres_image,
    )
    _verify_gpg_key(runner, validated.gpg_home, validated.fingerprint)
    return validated


def _safe_remove_partial(path: Path, backup_root: Path) -> None:
    if not path.exists():
        return
    resolved_root = backup_root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    if (
        resolved_path.parent != resolved_root
        or not resolved_path.name.startswith(".partial-")
        or resolved_path.is_symlink()
    ):
        raise BackupError("refusing to remove an unguarded partial backup path")
    shutil.rmtree(resolved_path)


def create_backup(config: CommonConfig, runner: Runner | None = None) -> Path:
    """Create an encrypted, self-verified backup with a bounded write quiesce."""
    runner = runner or Runner()
    config = _validated_config(config, runner)
    before = _collect_source_state(config, runner)
    stamp = _timestamp()
    partial = config.backup_root / f".partial-{stamp}-{secrets.token_hex(4)}"
    destination = config.backup_root / f"{stamp}-{before.revision[:12]}"
    if destination.exists():
        raise BackupError(f"backup destination already exists: {destination.name}")
    work_dir: Path | None = None
    stopped = False
    published = False
    try:
        partial.mkdir(mode=0o700)
        partial.chmod(0o700)
        work_dir = Path(tempfile.mkdtemp(prefix="ledgerbridge-backup-", dir=config.work_root))
        work_dir.chmod(0o700)
        _assert_tree_has_no_symlinks(config.project_dir)
        stopped = True
        runner.run(
            [
                "docker",
                "stop",
                "--time",
                "30",
                before.api_container,
                before.worker_container,
            ]
        )
        for service, container in (
            ("api", before.api_container),
            ("worker", before.worker_container),
        ):
            if _container_status(runner, container) != "exited":
                raise BackupError(f"production service did not stop cleanly: {service}")

        quiesced = replace(
            before,
            database=_database_metadata(runner, before.postgres_container),
        )
        payload = _create_plain_payload(config, quiesced, work_dir, runner)
        cipher = partial / "ledgerbridge-backup.tar.gpg"
        runner.run(
            [
                "gpg",
                "--homedir",
                str(config.gpg_home),
                "--batch",
                "--yes",
                "--trust-model",
                "always",
                "--recipient",
                config.fingerprint,
                "--output",
                str(cipher),
                "--encrypt",
                str(payload),
            ]
        )
        cipher.chmod(0o600)
        roundtrip = work_dir / "roundtrip.tar"
        runner.run(
            [
                "gpg",
                "--homedir",
                str(config.gpg_home),
                "--batch",
                "--yes",
                "--output",
                str(roundtrip),
                "--decrypt",
                str(cipher),
            ]
        )
        if not hmac.compare_digest(_sha256(payload), _sha256(roundtrip)):
            raise BackupError("encrypted backup failed its decrypt round-trip check")

        sidecar = {
            "format": BACKUP_FORMAT,
            "created_at": _now().isoformat(),
            "revision": before.revision,
            "gpg_fingerprint": config.fingerprint,
            "ciphertext": cipher.name,
            "ciphertext_sha256": _sha256(cipher),
            "postgres_image": config.postgres_image,
        }
        _write_json(partial / "backup.json", sidecar)
        checksum = partial / "SHA256SUMS"
        checksum.write_text(
            f"{sidecar['ciphertext_sha256']}  {cipher.name}\n",
            encoding="utf-8",
            newline="\n",
        )
        checksum.chmod(0o600)

        _restart_application(runner, before)
        stopped = False
        after = _collect_source_state(config, runner)
        _assert_source_unchanged(quiesced, after)
        partial.rename(destination)
        published = True
        return destination
    except BaseException as error:
        if stopped:
            try:
                _restart_application(runner, before)
            except BaseException as restart_error:
                raise BackupError(
                    f"backup failed and production restart also failed: {error}"
                ) from restart_error
        raise
    finally:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
        if not published:
            _safe_remove_partial(partial, config.backup_root)


def _safe_extract_tar(
    archive: Path, destination: Path, *, expected_files: set[str] | None = None
) -> None:
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    destination.chmod(0o700)
    with tarfile.open(archive, mode="r:") as bundle:
        members = bundle.getmembers()
        names: set[str] = set()
        for member in members:
            pure = PurePosixPath(member.name)
            normalized = pure.as_posix()
            if (
                not normalized
                or pure.is_absolute()
                or ".." in pure.parts
                or member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
                or normalized in names
            ):
                raise BackupError(f"unsafe or duplicate tar member: {member.name!r}")
            names.add(normalized)
        if expected_files is not None and names != expected_files:
            raise BackupError("backup payload contains an unexpected file set")
        bundle.extractall(destination, members=members, filter="data")  # nosec B202


def _validate_backup_directory(config: CommonConfig, backup: Path) -> Path:
    backup = _validate_secure_directory(backup, "backup directory")
    if backup.parent != config.backup_root:
        raise BackupError("backup directory must be a direct child of the configured root")
    return backup


def _validate_backup_sidecar(config: CommonConfig, backup: Path) -> tuple[dict[str, Any], Path]:
    sidecar = _load_json(backup / "backup.json", "backup sidecar")
    expected_keys = {
        "format",
        "created_at",
        "revision",
        "gpg_fingerprint",
        "ciphertext",
        "ciphertext_sha256",
        "postgres_image",
    }
    if set(sidecar) != expected_keys or sidecar.get("format") not in SUPPORTED_BACKUP_FORMATS:
        raise BackupError("backup sidecar format or field set is invalid")
    revision = sidecar.get("revision")
    if not isinstance(revision, str) or REVISION_PATTERN.fullmatch(revision) is None:
        raise BackupError("backup sidecar revision is invalid")
    if sidecar.get("gpg_fingerprint") != config.fingerprint:
        raise BackupError("backup was not encrypted for the configured key")
    if sidecar.get("postgres_image") != config.postgres_image:
        raise BackupError("backup PostgreSQL image pin does not match this automation")
    if sidecar.get("ciphertext") != "ledgerbridge-backup.tar.gpg":
        raise BackupError("backup ciphertext filename is invalid")
    digest = sidecar.get("ciphertext_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise BackupError("backup ciphertext digest is invalid")
    cipher = _validate_private_file(backup / "ledgerbridge-backup.tar.gpg", "ciphertext")
    checksum = _validate_private_file(backup / "SHA256SUMS", "ciphertext checksum")
    expected_line = f"{digest}  {cipher.name}\n"
    if not hmac.compare_digest(checksum.read_text(encoding="utf-8"), expected_line):
        raise BackupError("SHA256SUMS does not match the backup sidecar")
    if not hmac.compare_digest(_sha256(cipher), digest):
        raise BackupError("encrypted backup checksum mismatch")
    return sidecar, cipher


def _validate_tar_archive(archive: Path) -> None:
    with tarfile.open(archive, mode="r:") as bundle:
        names: set[str] = set()
        for member in bundle.getmembers():
            pure = PurePosixPath(member.name)
            normalized = pure.as_posix()
            if (
                not normalized
                or pure.is_absolute()
                or ".." in pure.parts
                or member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
                or normalized in names
                or (normalized == "." and not member.isdir())
            ):
                raise BackupError(f"unsafe or duplicate tar member: {member.name!r}")
            names.add(normalized)


def _validate_backup_image(
    runner: Runner, image: object, image_id: object | None, revision: str
) -> str:
    if (
        not isinstance(image, str)
        or re.fullmatch(r"ledgerbridge-app:[0-9a-f]{7,40}", image) is None
    ):
        raise BackupError("backup application image tag is invalid")
    if image_id is not None and (
        not isinstance(image_id, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
    ):
        raise BackupError("backup application immutable image ID is invalid")
    resolved_id = runner.capture(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    if image_id is not None and not hmac.compare_digest(resolved_id, image_id):
        raise BackupError("backup application tag no longer resolves to its immutable image ID")
    inspected_image = image_id if image_id is not None else image
    label = runner.capture(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            inspected_image,
        ]
    )
    if not hmac.compare_digest(label, revision):
        raise BackupError("backup application image revision label is invalid")
    return inspected_image


def _wait_for_postgres(runner: Runner, container: str, timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        output = runner.capture(
            ["docker", "exec", container, "pg_isready", "-U", "postgres"],
            check=False,
        )
        if "accepting connections" in output:
            return
        time.sleep(2)
    raise BackupError("isolated PostgreSQL did not become ready")


def _cleanup_restore_resources(runner: Runner, resources: RestoreResources) -> None:
    runner.run(["docker", "rm", "--force", resources.container], check=False)
    runner.run(["docker", "volume", "rm", "--force", resources.database_volume], check=False)
    runner.run(["docker", "volume", "rm", "--force", resources.artifact_volume], check=False)
    runner.run(["docker", "network", "rm", resources.network], check=False)
    probes = (
        ("container", ["docker", "inspect", resources.container]),
        ("database volume", ["docker", "volume", "inspect", resources.database_volume]),
        ("artifact volume", ["docker", "volume", "inspect", resources.artifact_volume]),
        ("network", ["docker", "network", "inspect", resources.network]),
    )
    remaining = [label for label, command in probes if runner.succeeds(command)]
    if remaining:
        raise BackupError(f"restore resources were not removed: {', '.join(remaining)}")


def _database_name(metadata: dict[str, Any]) -> str:
    value = metadata.get("database_name")
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value) is None:
        raise BackupError("database name in backup metadata is invalid")
    return value


def _legacy_runtime_role_is_retired(metadata: dict[str, Any]) -> bool:
    roles = metadata.get("r1_role_matrix")
    if isinstance(roles, list):
        app = next(
            (
                item
                for item in roles
                if isinstance(item, dict) and item.get("role") == "ledgerbridge_app"
            ),
            None,
        )
        if isinstance(app, dict) and isinstance(app.get("login"), bool):
            return app["login"] is False
    revision = metadata.get("alembic_version")
    return (
        isinstance(revision, str)
        and revision >= "20260823_0007"
        and metadata.get("runtime_role_valid") is False
        and metadata.get("role_grant_count") == 0
    )


_SQL_STRING_LITERAL_PATTERN = r"'(?:''|[^'])*'"
_SOURCE_TEXT_ARRAY_ITEM_PATTERN = rf"{_SQL_STRING_LITERAL_PATTERN}::character varying"
_RESTORED_TEXT_ARRAY_ITEM_PATTERN = rf"\({_SQL_STRING_LITERAL_PATTERN}::character varying\)::text"
_SOURCE_TEXT_ARRAY_PATTERN = re.compile(
    rf"\(ARRAY\[(?P<items>{_SOURCE_TEXT_ARRAY_ITEM_PATTERN}"
    rf"(?:,[ \t]*{_SOURCE_TEXT_ARRAY_ITEM_PATTERN})*)\]\)::text\[\]"
)
_RESTORED_TEXT_ARRAY_PATTERN = re.compile(
    rf"ARRAY\[(?P<items>{_RESTORED_TEXT_ARRAY_ITEM_PATTERN}"
    rf"(?:,[ \t]*{_RESTORED_TEXT_ARRAY_ITEM_PATTERN})*)\]"
)
_SQL_STRING_LITERAL = re.compile(_SQL_STRING_LITERAL_PATTERN)


def _canonical_check_constraint_definition(definition: str) -> str:
    """Normalize only PostgreSQL's equivalent varchar[] to text[] rendering.

    ``pg_dump`` emits a varchar array followed by one array-level cast while a
    restored catalog renders the same parse tree with an element-level text
    cast.  The replacement retains every quoted literal and leaves every other
    expression byte-for-byte unchanged.
    """

    def replace(match: re.Match[str]) -> str:
        literals = _SQL_STRING_LITERAL.findall(match.group("items"))
        return "LEDGERBRIDGE_TEXT_ARRAY[" + ",".join(literals) + "]"

    canonical = _SOURCE_TEXT_ARRAY_PATTERN.sub(replace, definition)
    return _RESTORED_TEXT_ARRAY_PATTERN.sub(replace, canonical)


def _canonical_constraint_rows(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    canonical: list[Any] = []
    for item in value:
        if not isinstance(item, dict):
            canonical.append(item)
            continue
        row = dict(item)
        definition = row.get("definition")
        if row.get("type") == "c" and isinstance(definition, str):
            row["definition"] = _canonical_check_constraint_definition(definition)
        canonical.append(row)
    return canonical


def _canonical_constraint_contract(
    contract: dict[Any, tuple[Any, Any, Any]],
) -> dict[Any, tuple[Any, Any, Any]]:
    return {
        name: (
            table,
            constraint_type,
            _canonical_check_constraint_definition(definition)
            if constraint_type == "c" and isinstance(definition, str)
            else definition,
        )
        for name, (table, constraint_type, definition) in contract.items()
    }


_DEFAULT_TABLE_OWNER_PRIVILEGES = frozenset(
    {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
)
_TABLE_ACL_OWNER_FIELDS = {
    "counterparty_table_acls": (
        "counterparty_tables",
        "counterparty_effective_table_privileges",
    ),
    "bank_statement_table_acls": (
        "bank_statement_tables",
        "bank_statement_effective_table_privileges",
    ),
    "account_registry_table_acls": (
        "account_registry_tables",
        "account_registry_effective_table_privileges",
    ),
    "evidence_unlock_table_acls": (
        "evidence_unlock_tables",
        "evidence_unlock_effective_table_privileges",
    ),
}


def _without_redundant_table_owner_acls(acls: Any, tables: Any) -> Any:
    if (
        not isinstance(acls, list)
        or not isinstance(tables, list)
        or any(not isinstance(item, dict) for item in tables)
    ):
        return acls
    owners: dict[tuple[Any, Any], str] = {}
    for item in tables:
        table = item.get("table")
        owner = item.get("owner")
        identity = (item.get("schema"), table)
        if not isinstance(table, str) or not isinstance(owner, str) or identity in owners:
            return acls
        owners[identity] = owner
    owner_rows: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for item in acls:
        if not isinstance(item, dict):
            continue
        identity = (item.get("schema"), item.get("table"))
        if item.get("grantee") == owners.get(identity):
            owner_rows.setdefault(identity, []).append(item)
    complete_default_groups: set[tuple[Any, Any]] = set()
    for identity, rows in owner_rows.items():
        expected_keys = {"table", "grantee", "privilege", "grantable"}
        if identity[0] is not None:
            expected_keys.add("schema")
        if (
            len(rows) == len(_DEFAULT_TABLE_OWNER_PRIVILEGES)
            and all(set(item) == expected_keys for item in rows)
            and all(
                item.get("grantable") is False or item.get("grantable") == "NO" for item in rows
            )
            and {item.get("privilege") for item in rows} == _DEFAULT_TABLE_OWNER_PRIVILEGES
        ):
            complete_default_groups.add(identity)
    return [
        item
        for item in acls
        if not (
            isinstance(item, dict)
            and (item.get("schema"), item.get("table")) in complete_default_groups
            and item.get("grantee") == owners.get((item.get("schema"), item.get("table")))
        )
    ]


def _validate_restored_database(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    is_v2 = expected.get("metadata_version") == 2
    compared_fields = sorted(expected)
    if is_v2:
        comparison_expected = dict(expected)
        comparison_actual = dict(actual)
        if "cutover_inventory" not in comparison_expected:
            # Historical 0021 backups predate the count-only cutover inventory.
            # Keep them restorable without treating the unpaired new observation
            # as source evidence.
            comparison_actual.pop("cutover_inventory", None)
        for field in set(comparison_expected) & set(comparison_actual):
            if field.endswith("_constraints"):
                comparison_expected[field] = _canonical_constraint_rows(comparison_expected[field])
                comparison_actual[field] = _canonical_constraint_rows(comparison_actual[field])
        for acl_field, (table_field, effective_field) in _TABLE_ACL_OWNER_FIELDS.items():
            expected_tables = expected.get(table_field)
            actual_tables = actual.get(table_field)
            expected_effective = expected.get(effective_field)
            actual_effective = actual.get(effective_field)
            if (
                acl_field in comparison_expected
                and acl_field in comparison_actual
                and isinstance(expected_tables, list)
                and expected_tables == actual_tables
                and isinstance(expected_effective, list)
                and expected_effective == actual_effective
            ):
                comparison_expected[acl_field] = _without_redundant_table_owner_acls(
                    comparison_expected[acl_field], expected_tables
                )
                comparison_actual[acl_field] = _without_redundant_table_owner_acls(
                    comparison_actual[acl_field], actual_tables
                )
        database_owner = expected.get("database_owner")
        if isinstance(database_owner, str):
            owner_grantees = {database_owner, "pg_database_owner"}
            for field in ("r1_database_acl", "r1_schema_acl"):
                expected_acl = expected.get(field)
                actual_acl = actual.get(field)
                if isinstance(expected_acl, list) and isinstance(actual_acl, list):
                    comparison_expected[field] = sorted(
                        (
                            item
                            for item in expected_acl
                            if not isinstance(item, dict)
                            or item.get("grantee") not in owner_grantees
                        ),
                        key=lambda item: json.dumps(item, sort_keys=True),
                    )
                    comparison_actual[field] = sorted(
                        (
                            item
                            for item in actual_acl
                            if not isinstance(item, dict)
                            or item.get("grantee") not in owner_grantees
                        ),
                        key=lambda item: json.dumps(item, sort_keys=True),
                    )
        expected_roles = expected.get("r1_role_matrix")
        actual_roles = actual.get("r1_role_matrix")
        if (
            isinstance(database_owner, str)
            and actual.get("database_owner") == database_owner
            and isinstance(expected_roles, list)
            and isinstance(actual_roles, list)
            and all(isinstance(item, dict) for item in expected_roles)
            and all(isinstance(item, dict) for item in actual_roles)
            and all(item.get("role") != database_owner for item in expected_roles)
            and sum(item.get("role") == database_owner for item in actual_roles) == 1
        ):
            # The first R1 v2 metadata shape did not observe the database
            # owner in the role matrix.  Compare that one historical shape
            # against current observations without weakening any other field.
            comparison_actual["r1_role_matrix"] = [
                item for item in actual_roles if item.get("role") != database_owner
            ]
    else:
        comparison_expected = expected
        comparison_actual = {key: actual.get(key) for key in expected}
    if comparison_actual != comparison_expected:
        differing = sorted(
            key
            for key in set(comparison_expected) | set(comparison_actual)
            if comparison_expected.get(key) != comparison_actual.get(key)
        )
        raise BackupError(f"restored database metadata differs: {', '.join(differing)}")
    legacy_role_retired = _legacy_runtime_role_is_retired(actual)
    role_grant_count = actual.get("role_grant_count")
    if not isinstance(role_grant_count, int):
        raise BackupError("ledgerbridge_app restored table grants are invalid")
    if legacy_role_retired and role_grant_count != 0:
        raise BackupError("retired ledgerbridge_app retains restored table grants")
    if not legacy_role_retired and role_grant_count <= 0:
        raise BackupError("ledgerbridge_app has no restored table grants")
    required_true = (
        ("schema_create_denied",)
        if legacy_role_retired
        else ("runtime_role_valid", "audit_select_only", "schema_create_denied")
    )
    failed = [name for name in required_true if actual.get(name) is not True]
    if failed:
        raise BackupError(f"restored privilege invariants failed: {', '.join(failed)}")
    if actual.get("data_checksums") != "on":
        raise BackupError("restored PostgreSQL cluster does not have data checksums enabled")
    missing_objects = [
        name
        for name in ("function_count", "trigger_count")
        if not isinstance(actual.get(name), int) or actual[name] <= 0
    ]
    if missing_objects:
        raise BackupError(f"restored database lacks required objects: {', '.join(missing_objects)}")
    if is_v2:
        _validate_rich_database_security(actual)
    return compared_fields


def _validate_r1_database_security(metadata: dict[str, Any]) -> None:
    """Validate the closed R1 catalog/ACL surface from a restore observation.

    R1 deliberately has no direct table grant for ``ledgerbridge_app``.  The
    compatibility role keeps its legacy Phase 1/2/3 grants, while all R1 facts
    are exposed only through the reader's internal views/functions.  Raw ACLs
    and effective ``has_*_privilege`` observations are both checked because an
    inherited PUBLIC grant is invisible in a simple role-table-grant listing.
    """

    def _list(name: str) -> list[dict[str, Any]]:
        value = metadata.get(name)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise BackupError(f"restored R1 metadata is invalid: {name}")
        return cast(list[dict[str, Any]], value)

    def _is_grantable_flag(value: Any) -> bool:
        # R1_SECURITY_SQL emits the catalog boolean.  Accept the historical
        # information_schema spelling as well for already-created v2 reports.
        return (
            value is True or value is False or (isinstance(value, str) and value in {"YES", "NO"})
        )

    def _is_not_grantable(value: Any) -> bool:
        return value is False or value == "NO"

    database_owner = metadata.get("database_owner")
    if not isinstance(database_owner, str) or database_owner in R1_CONTROLLED_ROLES:
        raise BackupError("restored R1 database owner is invalid")

    roles = _list("r1_role_matrix")
    role_names = [item.get("role") for item in roles]
    if any(not isinstance(role, str) for role in role_names):
        raise BackupError("restored R1 role matrix is invalid")
    role_name_set = set(cast(list[str], role_names))
    if (
        len(role_name_set) != len(roles)
        or not set(R1_ROLES).issubset(role_name_set)
        or not role_name_set.issubset({*R1_CONTROLLED_ROLES, database_owner})
    ):
        raise BackupError("restored R1 role matrix is incomplete")
    active_roles = tuple(role for role in R1_CONTROLLED_ROLES if role in role_name_set)
    revision = metadata.get("alembic_version")
    if not isinstance(revision, str):
        raise BackupError("restored database revision is invalid")
    legacy_role_retired = _legacy_runtime_role_is_retired(metadata)
    for item in roles:
        role = item.get("role")
        is_database_owner = role == database_owner
        expected_login = role != "ledgerbridge_app" or not legacy_role_retired
        if (
            not isinstance(role, str)
            or not isinstance(item.get("login"), bool)
            or (
                not is_database_owner
                and (
                    (role != "ledgerbridge_backup" and item.get("login") is not expected_login)
                    or item.get("superuser") is not False
                    or item.get("create_database") is not False
                    or item.get("create_role") is not False
                    or item.get("inherit") is not False
                    or item.get("replication") is not False
                    or item.get("bypass_rls") is not False
                )
            )
            or item.get("memberships") != []
        ):
            raise BackupError(f"restored R1 role matrix is privileged or non-isolated: {role}")

    database_acl = _list("r1_database_acl")
    database_principals = {
        "PUBLIC",
        "pg_database_owner",
        database_owner,
        *R1_CONTROLLED_ROLES,
    }
    database_acl_keys: set[tuple[str, str]] = set()
    for item in database_acl:
        grantee = item.get("grantee")
        privilege = item.get("privilege")
        if not isinstance(grantee, str) or not isinstance(privilege, str):
            raise BackupError("restored R1 database ACL metadata is invalid")
        if not _is_grantable_flag(item.get("grantable")):
            raise BackupError("restored R1 database ACL metadata is invalid")
        key = (grantee, privilege)
        if key in database_acl_keys:
            raise BackupError("restored R1 database ACL contains a duplicate entry")
        database_acl_keys.add(key)
        if grantee not in database_principals:
            raise BackupError("restored R1 database ACL contains an unknown or stale grantee")
        if privilege not in {"CONNECT", "TEMPORARY", "CREATE"}:
            raise BackupError("restored R1 database ACL contains an excess privilege")
        if grantee == "PUBLIC":
            raise BackupError("restored R1 database ACL grants PUBLIC access")
        if grantee in R1_CONTROLLED_ROLES:
            if grantee not in active_roles:
                raise BackupError("restored R1 database ACL references an absent controlled role")
            if privilege != "CONNECT" or not _is_not_grantable(item.get("grantable")):
                raise BackupError("restored R1 runtime database ACL is over-privileged")

    expected_connect = set(R1_ROLES)
    if "ledgerbridge_backup" in active_roles:
        expected_connect.add("ledgerbridge_backup")
    connect_grantees = {
        item.get("grantee") for item in database_acl if item.get("privilege") == "CONNECT"
    }
    if not expected_connect.issubset(connect_grantees):
        raise BackupError("restored R1 database CONNECT allowlist is incomplete")
    schema_acl = _list("r1_schema_acl")
    schema_grants = {
        "public": {
            "PUBLIC": {"USAGE"},
            "pg_database_owner": {"USAGE", "CREATE"},
            database_owner: {"USAGE", "CREATE"},
            "ledgerbridge_api": {"USAGE"},
            "ledgerbridge_worker": {"USAGE"},
            "ledgerbridge_app": {"USAGE"},
        },
        "internal_read": {
            database_owner: {"USAGE", "CREATE"},
            "ledgerbridge_api": {"USAGE"},
            "ledgerbridge_reader": {"USAGE"},
        },
    }
    schema_acl_keys: set[tuple[str, str, str]] = set()
    for item in schema_acl:
        schema = item.get("schema")
        grantee = item.get("grantee")
        privilege = item.get("privilege")
        if (
            not isinstance(schema, str)
            or not isinstance(grantee, str)
            or not isinstance(privilege, str)
            or not _is_grantable_flag(item.get("grantable"))
        ):
            raise BackupError("restored R1 schema ACL metadata is invalid")
        schema_key = (schema, grantee, privilege)
        if schema_key in schema_acl_keys:
            raise BackupError("restored R1 schema ACL contains a duplicate entry")
        schema_acl_keys.add(schema_key)
        allowed_grants = schema_grants.get(schema)
        if allowed_grants is None:
            raise BackupError("restored R1 schema ACL contains an unexpected schema")
        allowed_privileges = allowed_grants.get(grantee)
        if allowed_privileges is None:
            if grantee not in database_principals:
                raise BackupError("restored R1 schema ACL contains an unknown or stale grantee")
            if privilege == "CREATE":
                raise BackupError("restored R1 schema CREATE privilege is over-broad")
            raise BackupError("restored R1 schema ACL contains an excess grant")
        if privilege not in allowed_privileges:
            if privilege == "CREATE":
                raise BackupError("restored R1 schema CREATE privilege is over-broad")
            raise BackupError("restored R1 schema ACL contains an excess grant")
        if grantee not in {database_owner, "pg_database_owner"} and not _is_not_grantable(
            item.get("grantable")
        ):
            raise BackupError("restored R1 schema ACL is grantable")

    default_acls = _list("r1_default_acls")
    default_acl_privileges = {
        "r": {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"},
        "S": {"SELECT", "UPDATE", "USAGE"},
        "f": {"EXECUTE"},
        "T": {"USAGE"},
        "n": {"USAGE", "CREATE"},
    }
    for item in default_acls:
        owner = item.get("owner")
        schema = item.get("schema")
        object_type = item.get("object_type")
        grantee = item.get("grantee")
        privilege = item.get("privilege")
        if (
            not isinstance(owner, str)
            or not isinstance(schema, str)
            or not isinstance(object_type, str)
            or not isinstance(grantee, str)
            or not isinstance(privilege, str)
        ):
            raise BackupError("restored R1 default ACL metadata is invalid")
        if not _is_grantable_flag(item.get("grantable")):
            raise BackupError("restored R1 default ACL metadata is invalid")
        if grantee not in database_principals:
            raise BackupError("restored R1 default ACL contains an unknown or stale grantee")
        if owner != database_owner or grantee != database_owner:
            raise BackupError("restored R1 default ACL contains an excess entry")
        if schema not in {"", "public", "internal_read", "internal_import", "internal_command"}:
            raise BackupError("restored R1 default ACL contains an unexpected schema")
        if privilege not in default_acl_privileges.get(object_type, set()):
            raise BackupError("restored R1 default ACL contains an excess privilege")

    constraints = _list("r1_constraints")
    constraint_names = {item.get("name") for item in constraints}
    missing_constraints = sorted(R1_REQUIRED_CONSTRAINTS - constraint_names)
    if missing_constraints:
        raise BackupError(
            "restored R1 constraints are incomplete: " + ", ".join(missing_constraints)
        )
    for item in constraints:
        if (
            not isinstance(item.get("schema"), str)
            or not isinstance(item.get("table"), str)
            or not isinstance(item.get("name"), str)
            or item.get("validated") is not True
        ):
            raise BackupError("restored R1 constraint metadata is invalid")

    triggers = _list("r1_triggers")
    trigger_names = {item.get("name") for item in triggers}
    missing_triggers = sorted(R1_REQUIRED_TRIGGERS - trigger_names)
    if missing_triggers:
        raise BackupError("restored R1 triggers are incomplete: " + ", ".join(missing_triggers))
    if any(item.get("enabled") != "O" for item in triggers):
        raise BackupError("restored R1 trigger is disabled")

    views = _list("r1_views")
    view_names = {item.get("name") for item in views}
    if view_names != set(R1_INTERNAL_READ_VIEWS):
        raise BackupError("restored R1 internal_read views differ from the required baseline")
    for item in views:
        if (
            item.get("schema") != "internal_read"
            or item.get("security_barrier") is not True
            or item.get("security_invoker") is not False
            or item.get("owner") != database_owner
        ):
            raise BackupError("restored R1 view security boundary is invalid")

    functions = _list("r1_functions")
    if any(item.get("owner") != database_owner for item in functions):
        raise BackupError("restored R1 function security boundary is invalid")
    if any(
        not isinstance(item.get("schema"), str)
        or not isinstance(item.get("name"), str)
        or not isinstance(item.get("identity_arguments"), str)
        for item in functions
    ):
        raise BackupError("restored R1 function metadata is invalid")
    internal_functions = [item for item in functions if item.get("schema") == "internal_read"]
    allowlisted_internal_function_keys = {
        ("internal_read", name, identity_arguments)
        for name, identity_arguments in R1_INTERNAL_READ_FUNCTION_SIGNATURES.items()
    }
    required_internal_function_keys = set(allowlisted_internal_function_keys)
    if revision < R1_ACCOUNTING_DIMENSIONS_REVISION:
        required_internal_function_keys.remove(
            (
                "internal_read",
                "get_accounting_dimensions",
                R1_INTERNAL_READ_FUNCTION_SIGNATURES["get_accounting_dimensions"],
            )
        )
    actual_internal_function_keys = {
        (item["schema"], item["name"], item["identity_arguments"]) for item in internal_functions
    }
    if (
        len(actual_internal_function_keys) != len(internal_functions)
        or not required_internal_function_keys.issubset(actual_internal_function_keys)
        or not actual_internal_function_keys.issubset(allowlisted_internal_function_keys)
    ):
        raise BackupError(
            "restored R1 internal_read functions differ from the required signature baseline"
        )
    for item in internal_functions:
        if (
            item.get("security_definer") is not True
            or not isinstance(item.get("proconfig"), list)
            or "search_path=pg_catalog" not in item["proconfig"]
        ):
            raise BackupError("restored R1 function security boundary is invalid")

    table_privileges = _list("r1_effective_table_privileges")
    expected_objects = (
        {("public", name) for name in R1_PUBLIC_TABLES}
        | {("internal_read", name) for name in R1_INTERNAL_READ_VIEWS}
        | {("internal_read", "evidence_read_receipt")}
    )
    expected_keys = {
        (role, schema, object_name)
        for role in active_roles
        for schema, object_name in expected_objects
    }
    actual_keys = {
        (item.get("role"), item.get("schema"), item.get("object")) for item in table_privileges
    }
    if len(actual_keys) != len(table_privileges) or actual_keys != expected_keys:
        raise BackupError("restored R1 effective table privilege matrix is incomplete")
    privilege_names = (
        "select",
        "insert",
        "update",
        "delete",
        "truncate",
        "references",
        "trigger",
    )
    for item in table_privileges:
        role = item.get("role")
        schema = item.get("schema")
        object_name = item.get("object")
        if any(not isinstance(item.get(name), bool) for name in privilege_names):
            raise BackupError("restored R1 effective table privilege metadata is invalid")
        expected_kind = (
            "table" if schema == "public" or object_name == "evidence_read_receipt" else "view"
        )
        if item.get("kind") != expected_kind:
            raise BackupError("restored R1 effective table privilege object kind is invalid")
        if schema == "public" or object_name == "evidence_read_receipt":
            if role == "ledgerbridge_app" and any(item[name] for name in privilege_names):
                raise BackupError("ledgerbridge_app has an unexpected R1 table grant")
            if any(item[name] for name in privilege_names):
                raise BackupError("restored R1 fact table has an unexpected effective privilege")
        elif schema == "internal_read":
            # The reader consumes only SECURITY DEFINER functions.  Direct
            # SELECT on the projection views and the receipt table remains
            # revoked for every runtime role, including ledgerbridge_reader.
            if any(item[name] for name in privilege_names):
                raise BackupError("restored R1 internal_read view privilege matrix is invalid")

    function_privileges = _list("r1_effective_function_privileges")
    expected_function_objects = set(actual_internal_function_keys)
    expected_function_objects.update(
        (item["schema"], item["name"], item["identity_arguments"])
        for item in functions
        if item["schema"] == "public"
    )
    expected_function_keys = {
        (role, schema, name, identity_arguments)
        for role in active_roles
        for schema, name, identity_arguments in expected_function_objects
    }
    if any(
        not isinstance(item.get("role"), str)
        or not isinstance(item.get("schema"), str)
        or not isinstance(item.get("name"), str)
        or not isinstance(item.get("identity_arguments"), str)
        for item in function_privileges
    ):
        raise BackupError("restored R1 effective function privilege metadata is invalid")
    actual_function_keys = {
        (item.get("role"), item.get("schema"), item.get("name"), item.get("identity_arguments"))
        for item in function_privileges
    }
    if (
        len(actual_function_keys) != len(function_privileges)
        or actual_function_keys != expected_function_keys
    ):
        raise BackupError("restored R1 effective function privilege matrix is incomplete")
    for item in function_privileges:
        role = item.get("role")
        schema = item.get("schema")
        if not isinstance(item.get("execute"), bool):
            raise BackupError("restored R1 effective function privilege metadata is invalid")
        if schema == "internal_read":
            expected_executor = (
                "ledgerbridge_api"
                if item.get("name") == "append_internal_evidence_read_audit"
                else "ledgerbridge_reader"
            )
            if item["execute"] != (role == expected_executor):
                raise BackupError("restored R1 internal_read function privilege matrix is invalid")
        if schema == "public" and item["execute"]:
            raise BackupError("restored R1 public validator is executable by a runtime role")

    schema_privileges = _list("r1_effective_schema_privileges")
    expected_schema_keys = {
        (role, schema) for role in active_roles for schema in ("public", "internal_read")
    }
    actual_schema_keys = {(item.get("role"), item.get("schema")) for item in schema_privileges}
    if (
        len(actual_schema_keys) != len(schema_privileges)
        or actual_schema_keys != expected_schema_keys
    ):
        raise BackupError("restored R1 effective schema privilege matrix is incomplete")
    for item in schema_privileges:
        role = item.get("role")
        schema = item.get("schema")
        if not isinstance(item.get("usage"), bool) or not isinstance(item.get("create"), bool):
            raise BackupError("restored R1 effective schema privilege metadata is invalid")
        if item["create"] or (
            schema == "internal_read"
            and item["usage"]
            != (
                role == "ledgerbridge_reader"
                or (role == "ledgerbridge_api" and revision >= "20260828_0016")
            )
        ):
            raise BackupError("restored R1 schema privilege matrix is invalid")


def _validate_counterparty_security(metadata: dict[str, Any]) -> None:
    def _list(name: str) -> list[dict[str, Any]]:
        value = metadata.get(name)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise BackupError(f"restored counterparty metadata is invalid: {name}")
        return cast(list[dict[str, Any]], value)

    def _grantable(value: Any) -> bool:
        return (
            value is True or value is False or (isinstance(value, str) and value in {"YES", "NO"})
        )

    row_counts = metadata.get("counterparty_row_counts")
    if (
        not isinstance(row_counts, dict)
        or set(row_counts) != set(COUNTERPARTY_PROTECTED_TABLES)
        or any(not isinstance(value, int) or value < 0 for value in row_counts.values())
    ):
        raise BackupError("restored counterparty row-count metadata is invalid")

    tables = _list("counterparty_tables")
    actual_tables = {item.get("table") for item in tables}
    owner = metadata.get("database_owner")
    if not isinstance(owner, str):
        raise BackupError("restored counterparty database owner is invalid")
    if len(actual_tables) != len(tables) or actual_tables != set(COUNTERPARTY_PROTECTED_TABLES):
        raise BackupError("restored counterparty tables differ from the required baseline")
    if any(item.get("owner") != owner or item.get("kind") != "r" for item in tables):
        raise BackupError("restored counterparty table security boundary is invalid")

    functions = _list("counterparty_functions")
    expected_functions = {
        (schema, name, args) for (schema, name), args in COUNTERPARTY_FUNCTION_SIGNATURES.items()
    }
    actual_functions = {
        (item.get("schema"), item.get("name"), item.get("identity_arguments")) for item in functions
    }
    if len(actual_functions) != len(functions) or actual_functions != expected_functions:
        raise BackupError("restored counterparty functions differ from the required baseline")
    for item in functions:
        schema = item.get("schema")
        name = item.get("name")
        if (
            item.get("owner") != owner
            or item.get("security_definer") is not (schema == "internal_read")
            or item.get("proconfig") != ["search_path=pg_catalog"]
            or item.get("result")
            != COUNTERPARTY_FUNCTION_RESULTS.get(cast(tuple[str, str], (schema, name)))
        ):
            raise BackupError("restored counterparty function security boundary is invalid")

    triggers = _list("counterparty_triggers")
    actual_trigger_contract = {
        item.get("name"): (
            item.get("table"),
            item.get("constraint"),
            item.get("trigger_type"),
            item.get("function_name"),
        )
        for item in triggers
    }
    if (
        len(actual_trigger_contract) != len(triggers)
        or actual_trigger_contract != COUNTERPARTY_TRIGGER_CONTRACT
    ):
        raise BackupError(
            "restored counterparty trigger contract differs from the required baseline"
        )
    if any(
        item.get("enabled") != "O" or item.get("function_schema") != "public" for item in triggers
    ):
        raise BackupError("restored counterparty trigger security boundary is invalid")

    constraints = _list("counterparty_constraints")
    actual_constraint_contract = _canonical_constraint_contract(
        {
            item.get("name"): (item.get("table"), item.get("type"), item.get("definition"))
            for item in constraints
        }
    )
    if len(actual_constraint_contract) != len(
        constraints
    ) or actual_constraint_contract != _canonical_constraint_contract(
        COUNTERPARTY_CONSTRAINT_CONTRACT
    ):
        raise BackupError("restored counterparty constraints differ from the required baseline")
    for item in constraints:
        if (
            item.get("validated") is not True
            or item.get("deferrable") is not False
            or item.get("initially_deferred") is not False
        ):
            raise BackupError("restored counterparty constraint definition is invalid")

    table_acls = _list("counterparty_table_acls")
    table_acl_keys: set[tuple[Any, ...]] = set()
    table_privileges = {
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
    }
    for item in table_acls:
        table_acl_key = (item.get("table"), item.get("grantee"), item.get("privilege"))
        if table_acl_key in table_acl_keys:
            raise BackupError("restored counterparty table ACL contains a duplicate entry")
        table_acl_keys.add(table_acl_key)
        if (
            item.get("table") not in COUNTERPARTY_PROTECTED_TABLES
            or item.get("grantee") != owner
            or item.get("privilege") not in table_privileges
            or not _grantable(item.get("grantable"))
        ):
            raise BackupError("restored counterparty table ACL contains an excess grant")

    executors = {
        ("internal_read", "list_candidate_counterparty_facts"): "ledgerbridge_reader",
        ("internal_read", "list_candidate_evidence_satisfactions"): "ledgerbridge_reader",
    }
    function_acls = _list("counterparty_function_acls")
    function_acl_keys: set[tuple[Any, ...]] = set()
    for item in function_acls:
        acl_function_key = (
            item.get("schema"),
            item.get("name"),
            item.get("identity_arguments"),
        )
        function_acl_key = (*acl_function_key, item.get("grantee"), item.get("privilege"))
        if function_acl_key in function_acl_keys:
            raise BackupError("restored counterparty function ACL contains a duplicate entry")
        function_acl_keys.add(function_acl_key)
        executor = executors.get(cast(tuple[str, str], acl_function_key[:2]))
        allowed_grantees = {owner} if executor is None else {owner, executor}
        if (
            acl_function_key not in expected_functions
            or item.get("grantee") not in allowed_grantees
            or item.get("privilege") != "EXECUTE"
            or not _grantable(item.get("grantable"))
            or (item.get("grantee") != owner and item.get("grantable") not in {False, "NO"})
        ):
            raise BackupError("restored counterparty function ACL contains an excess grant")

    roles = _list("r1_role_matrix")
    active_roles = {item.get("role") for item in roles if item.get("role") in R1_CONTROLLED_ROLES}
    tables = _list("counterparty_effective_table_privileges")
    expected_table_keys = {
        (role, table) for role in active_roles for table in COUNTERPARTY_PROTECTED_TABLES
    }
    actual_table_keys = {(item.get("role"), item.get("table")) for item in tables}
    privilege_names = (
        "select",
        "insert",
        "update",
        "delete",
        "truncate",
        "references",
        "trigger",
    )
    if len(actual_table_keys) != len(tables) or actual_table_keys != expected_table_keys:
        raise BackupError("restored counterparty table privilege matrix is incomplete")
    if any(any(item.get(name) is not False for name in privilege_names) for item in tables):
        raise BackupError("restored counterparty fact table has an unexpected privilege")

    grants = _list("counterparty_effective_function_privileges")
    expected_grant_keys = {
        (role, schema, name, args)
        for role in active_roles
        for schema, name, args in expected_functions
    }
    actual_grant_keys = {
        (item.get("role"), item.get("schema"), item.get("name"), item.get("identity_arguments"))
        for item in grants
    }
    if len(actual_grant_keys) != len(grants) or actual_grant_keys != expected_grant_keys:
        raise BackupError("restored counterparty function privilege matrix is incomplete")
    for item in grants:
        schema = item.get("schema")
        name = item.get("name")
        if not isinstance(schema, str) or not isinstance(name, str):
            raise BackupError("restored counterparty function privilege metadata is invalid")
        executor = executors.get((schema, name))
        if item.get("execute") is not (item.get("role") == executor):
            raise BackupError("restored counterparty function privilege matrix is invalid")


def _validate_bank_statement_security(metadata: dict[str, Any]) -> None:
    def _list(name: str) -> list[dict[str, Any]]:
        value = metadata.get(name)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise BackupError(f"restored bank statement metadata is invalid: {name}")
        return cast(list[dict[str, Any]], value)

    def _grantable(value: Any) -> bool:
        return (
            value is True or value is False or (isinstance(value, str) and value in {"YES", "NO"})
        )

    row_counts = metadata.get("bank_statement_row_counts")
    if (
        not isinstance(row_counts, dict)
        or set(row_counts) != set(BANK_STATEMENT_TABLES)
        or any(not isinstance(value, int) or value < 0 for value in row_counts.values())
    ):
        raise BackupError("restored bank statement row-count metadata is invalid")

    owner = metadata.get("database_owner")
    revision = metadata.get("alembic_version")
    if not isinstance(owner, str) or not isinstance(revision, str):
        raise BackupError("restored bank statement database owner is invalid")
    tables = _list("bank_statement_tables")
    actual_tables = {item.get("table") for item in tables}
    if len(actual_tables) != len(tables) or actual_tables != set(BANK_STATEMENT_TABLES):
        raise BackupError("restored bank statement tables differ from the required baseline")
    if any(item.get("owner") != owner or item.get("kind") != "r" for item in tables):
        raise BackupError("restored bank statement table security boundary is invalid")

    observed_schemas = _list("bank_statement_schemas")
    expected_schema_names = {"internal_import", "internal_command", "internal_read"}
    actual_schema_names = {item.get("schema") for item in observed_schemas}
    if (
        len(actual_schema_names) != len(observed_schemas)
        or actual_schema_names != expected_schema_names
        or any(item.get("owner") != owner for item in observed_schemas)
    ):
        raise BackupError("restored bank statement schema security boundary is invalid")

    functions = _list("bank_statement_functions")
    expected_functions = {
        (schema, name, args) for (schema, name), args in BANK_STATEMENT_FUNCTION_SIGNATURES.items()
    }
    actual_functions = {
        (item.get("schema"), item.get("name"), item.get("identity_arguments")) for item in functions
    }
    if len(actual_functions) != len(functions) or actual_functions != expected_functions:
        raise BackupError("restored bank statement functions differ from the required baseline")
    for item in functions:
        schema = item.get("schema")
        name = item.get("name")
        function_key = cast(tuple[str, str], (schema, name))
        if (
            item.get("owner") != owner
            or item.get("security_definer")
            is not (function_key in BANK_STATEMENT_SECURITY_DEFINER_FUNCTIONS)
            or item.get("proconfig") != ["search_path=pg_catalog"]
            or item.get("result") != BANK_STATEMENT_FUNCTION_RESULTS.get(function_key)
        ):
            raise BackupError("restored bank statement function security boundary is invalid")

    executors = {
        ("internal_import", "import_bank_statement"): "ledgerbridge_worker",
        ("internal_read", "get_bank_statement_summary"): "ledgerbridge_reader",
        ("internal_read", "list_bank_statement_transactions"): "ledgerbridge_reader",
    }

    triggers = _list("bank_statement_triggers")
    actual_trigger_contract = {
        item.get("name"): (
            item.get("table"),
            item.get("constraint"),
            item.get("trigger_type"),
            item.get("deferrable"),
            item.get("initially_deferred"),
            item.get("function_name"),
        )
        for item in triggers
    }
    expected_trigger_contract = dict(BANK_STATEMENT_TRIGGER_CONTRACT)
    if revision >= ACCOUNT_REGISTRY_SECURITY_REVISION:
        expected_trigger_contract.pop("validate_managed_account_audit")
        expected_trigger_contract.pop("require_statement_backed_account")
        expected_trigger_contract.update(ACCOUNT_REGISTRY_MANAGED_ACCOUNT_TRIGGER_CONTRACT)
    if (
        len(actual_trigger_contract) != len(triggers)
        or actual_trigger_contract != expected_trigger_contract
    ):
        raise BackupError(
            "restored bank statement trigger contract differs from the required baseline"
        )
    if any(
        item.get("enabled") != "O" or item.get("function_schema") != "public" for item in triggers
    ):
        raise BackupError("restored bank statement trigger security boundary is invalid")

    constraints = _list("bank_statement_constraints")
    actual_constraint_contract = _canonical_constraint_contract(
        {
            item.get("name"): (item.get("table"), item.get("type"), item.get("definition"))
            for item in constraints
        }
    )
    expected_constraint_contract = dict(BANK_STATEMENT_CONSTRAINT_CONTRACT)
    if revision >= ACCOUNT_REGISTRY_SECURITY_REVISION:
        expected_constraint_contract.pop("managed_account_institution_code_check")
        expected_constraint_contract.update(ACCOUNT_REGISTRY_MANAGED_ACCOUNT_CONSTRAINT_CONTRACT)
    if len(actual_constraint_contract) != len(
        constraints
    ) or actual_constraint_contract != _canonical_constraint_contract(expected_constraint_contract):
        raise BackupError("restored bank statement constraints differ from the required baseline")
    for item in constraints:
        if (
            item.get("validated") is not True
            or item.get("deferrable") is not False
            or item.get("initially_deferred") is not False
        ):
            raise BackupError("restored bank statement constraint definition is invalid")

    table_acls = _list("bank_statement_table_acls")
    table_acl_keys: set[tuple[Any, ...]] = set()
    table_privileges = {
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
    }
    for item in table_acls:
        table_acl_key = (item.get("table"), item.get("grantee"), item.get("privilege"))
        if table_acl_key in table_acl_keys:
            raise BackupError("restored bank statement table ACL contains a duplicate entry")
        table_acl_keys.add(table_acl_key)
        if (
            item.get("table") not in BANK_STATEMENT_TABLES
            or item.get("grantee") != owner
            or item.get("privilege") not in table_privileges
            or not _grantable(item.get("grantable"))
        ):
            raise BackupError("restored bank statement table ACL contains an excess grant")

    function_acls = _list("bank_statement_function_acls")
    function_acl_keys: set[tuple[Any, ...]] = set()
    for item in function_acls:
        acl_function_key = (
            item.get("schema"),
            item.get("name"),
            item.get("identity_arguments"),
        )
        function_acl_key = (*acl_function_key, item.get("grantee"), item.get("privilege"))
        if function_acl_key in function_acl_keys:
            raise BackupError("restored bank statement function ACL contains a duplicate entry")
        function_acl_keys.add(function_acl_key)
        executor = executors.get(cast(tuple[str, str], acl_function_key[:2]))
        allowed_grantees = {owner} if executor is None else {owner, executor}
        if (
            acl_function_key not in expected_functions
            or item.get("grantee") not in allowed_grantees
            or item.get("privilege") != "EXECUTE"
            or not _grantable(item.get("grantable"))
            or (item.get("grantee") != owner and item.get("grantable") not in {False, "NO"})
        ):
            raise BackupError("restored bank statement function ACL contains an excess grant")

    schema_acls = _list("bank_statement_schema_acls")
    schema_grants = {
        "internal_import": {owner: {"USAGE", "CREATE"}, "ledgerbridge_worker": {"USAGE"}},
        "internal_command": {owner: {"USAGE", "CREATE"}, "ledgerbridge_api": {"USAGE"}},
        "internal_read": {
            owner: {"USAGE", "CREATE"},
            "ledgerbridge_api": {"USAGE"},
            "ledgerbridge_reader": {"USAGE"},
        },
    }
    schema_acl_keys: set[tuple[Any, ...]] = set()
    for item in schema_acls:
        schema_acl_key = (item.get("schema"), item.get("grantee"), item.get("privilege"))
        if schema_acl_key in schema_acl_keys:
            raise BackupError("restored bank statement schema ACL contains a duplicate entry")
        schema_acl_keys.add(schema_acl_key)
        allowed_privileges = schema_grants.get(cast(str, item.get("schema")), {}).get(
            cast(str, item.get("grantee"))
        )
        if (
            allowed_privileges is None
            or item.get("privilege") not in allowed_privileges
            or not _grantable(item.get("grantable"))
            or (item.get("grantee") != owner and item.get("grantable") not in {False, "NO"})
        ):
            raise BackupError("restored bank statement schema ACL contains an excess grant")

    roles = _list("r1_role_matrix")
    active_roles = {item.get("role") for item in roles if item.get("role") in R1_CONTROLLED_ROLES}
    tables = _list("bank_statement_effective_table_privileges")
    expected_table_keys = {
        (role, table) for role in active_roles for table in BANK_STATEMENT_TABLES
    }
    actual_table_keys = {(item.get("role"), item.get("table")) for item in tables}
    privilege_names = (
        "select",
        "insert",
        "update",
        "delete",
        "truncate",
        "references",
        "trigger",
    )
    if len(actual_table_keys) != len(tables) or actual_table_keys != expected_table_keys:
        raise BackupError("restored bank statement table privilege matrix is incomplete")
    if any(any(item.get(name) is not False for name in privilege_names) for item in tables):
        raise BackupError("restored bank statement fact table has an unexpected privilege")

    grants = _list("bank_statement_effective_function_privileges")
    expected_grant_keys = {
        (role, schema, name, args)
        for role in active_roles
        for schema, name, args in expected_functions
    }
    actual_grant_keys = {
        (item.get("role"), item.get("schema"), item.get("name"), item.get("identity_arguments"))
        for item in grants
    }
    if len(actual_grant_keys) != len(grants) or actual_grant_keys != expected_grant_keys:
        raise BackupError("restored bank statement function privilege matrix is incomplete")
    for item in grants:
        schema = item.get("schema")
        name = item.get("name")
        if not isinstance(schema, str) or not isinstance(name, str):
            raise BackupError("restored bank statement function privilege metadata is invalid")
        executor = executors.get((schema, name))
        if item.get("execute") is not (item.get("role") == executor):
            raise BackupError("restored bank statement function privilege matrix is invalid")

    schemas = _list("bank_statement_effective_schema_privileges")
    expected_schema_keys = {
        (role, schema)
        for role in active_roles
        for schema in ("internal_import", "internal_command", "internal_read")
    }
    actual_schema_keys = {(item.get("role"), item.get("schema")) for item in schemas}
    if len(actual_schema_keys) != len(schemas) or actual_schema_keys != expected_schema_keys:
        raise BackupError("restored bank statement schema privilege matrix is incomplete")
    schema_users = {
        "internal_import": {"ledgerbridge_worker"},
        "internal_command": {"ledgerbridge_api"},
        "internal_read": {"ledgerbridge_api", "ledgerbridge_reader"},
    }
    for item in schemas:
        schema = item.get("schema")
        role = item.get("role")
        if not isinstance(schema, str) or not isinstance(role, str):
            raise BackupError("restored bank statement schema privilege metadata is invalid")
        if (
            item.get("usage") is not (role in schema_users[schema])
            or item.get("create") is not False
        ):
            raise BackupError("restored bank statement schema privilege matrix is invalid")


def _validate_account_registry_security(metadata: dict[str, Any]) -> None:
    def _list(name: str) -> list[dict[str, Any]]:
        value = metadata.get(name)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise BackupError(f"restored account registry metadata is invalid: {name}")
        return cast(list[dict[str, Any]], value)

    def _grantable(value: Any) -> bool:
        return value is True or value is False or value in {"YES", "NO"}

    owner = metadata.get("database_owner")
    if not isinstance(owner, str):
        raise BackupError("restored account registry database owner is invalid")
    row_counts = metadata.get("account_registry_row_counts")
    if (
        not isinstance(row_counts, dict)
        or set(row_counts) != set(ACCOUNT_REGISTRY_TABLES)
        or any(not isinstance(value, int) or value < 0 for value in row_counts.values())
    ):
        raise BackupError("restored account registry row-count metadata is invalid")

    tables = _list("account_registry_tables")
    actual_tables = {item.get("table") for item in tables}
    if (
        len(actual_tables) != len(tables)
        or actual_tables != set(ACCOUNT_REGISTRY_TABLES)
        or any(item.get("owner") != owner or item.get("kind") != "r" for item in tables)
    ):
        raise BackupError("restored account registry table boundary is invalid")

    functions = _list("account_registry_functions")
    expected_functions = {
        (schema, name, args)
        for (schema, name), args in ACCOUNT_REGISTRY_FUNCTION_SIGNATURES.items()
    }
    actual_functions = {
        (item.get("schema"), item.get("name"), item.get("identity_arguments")) for item in functions
    }
    if len(actual_functions) != len(functions) or actual_functions != expected_functions:
        raise BackupError("restored account registry functions differ from the required baseline")
    for item in functions:
        function_key = cast(tuple[str, str], (item.get("schema"), item.get("name")))
        if (
            item.get("owner") != owner
            or item.get("security_definer")
            is not (function_key in ACCOUNT_REGISTRY_SECURITY_DEFINER_FUNCTIONS)
            or item.get("proconfig") != ["search_path=pg_catalog"]
            or item.get("result") != ACCOUNT_REGISTRY_FUNCTION_RESULTS.get(function_key)
        ):
            raise BackupError("restored account registry function boundary is invalid")

    triggers = _list("account_registry_triggers")
    actual_trigger_contract = {
        item.get("name"): (
            item.get("table"),
            item.get("constraint"),
            item.get("deferrable"),
            item.get("initially_deferred"),
            item.get("function_name"),
        )
        for item in triggers
    }
    if (
        len(actual_trigger_contract) != len(triggers)
        or actual_trigger_contract != ACCOUNT_REGISTRY_TRIGGER_CONTRACT
        or any(item.get("enabled") != "O" for item in triggers)
    ):
        raise BackupError("restored account registry trigger contract is invalid")

    constraints = _list("account_registry_constraints")
    required_primary_keys = {f"{table}_pkey" for table in ACCOUNT_REGISTRY_TABLES}
    constraint_names = {item.get("name") for item in constraints}
    constraint_triggers = {
        name: (table, deferrable, initially_deferred)
        for name, (
            table,
            is_constraint,
            deferrable,
            initially_deferred,
            _,
        ) in ACCOUNT_REGISTRY_TRIGGER_CONTRACT.items()
        if is_constraint
    }

    def _constraint_is_invalid(item: dict[str, Any]) -> bool:
        name = item.get("name")
        if (
            not isinstance(name, str)
            or item.get("table") not in ACCOUNT_REGISTRY_TABLES
            or item.get("validated") is not True
        ):
            return True
        if item.get("type") == "t":
            return constraint_triggers.get(name) != (
                item.get("table"),
                item.get("deferrable"),
                item.get("initially_deferred"),
            )
        return item.get("deferrable") is not False or item.get("initially_deferred") is not False

    if (
        len(constraint_names) != len(constraints)
        or not required_primary_keys.issubset(constraint_names)
        or any(_constraint_is_invalid(item) for item in constraints)
    ):
        raise BackupError("restored account registry constraint contract is invalid")

    table_acls = _list("account_registry_table_acls")
    table_acl_keys = {
        (item.get("table"), item.get("grantee"), item.get("privilege")) for item in table_acls
    }
    if len(table_acl_keys) != len(table_acls) or any(
        item.get("table") not in ACCOUNT_REGISTRY_TABLES
        or item.get("grantee") != owner
        or not _grantable(item.get("grantable"))
        for item in table_acls
    ):
        raise BackupError("restored account registry table ACL contains an excess grant")

    function_acls = _list("account_registry_function_acls")
    function_acl_keys = {
        (
            item.get("schema"),
            item.get("name"),
            item.get("identity_arguments"),
            item.get("grantee"),
            item.get("privilege"),
        )
        for item in function_acls
    }
    if len(function_acl_keys) != len(function_acls):
        raise BackupError("restored account registry function ACL contains a duplicate")
    for item in function_acls:
        key = cast(tuple[str, str], (item.get("schema"), item.get("name")))
        executor = ACCOUNT_REGISTRY_FUNCTION_EXECUTORS.get(key)
        allowed_grantees = {owner} if executor is None else {owner, executor}
        if (
            item.get("grantee") not in allowed_grantees
            or item.get("privilege") != "EXECUTE"
            or not _grantable(item.get("grantable"))
            or (item.get("grantee") != owner and item.get("grantable") not in {False, "NO"})
        ):
            raise BackupError("restored account registry function ACL contains an excess grant")

    roles = _list("r1_role_matrix")
    active_roles = {item.get("role") for item in roles if item.get("role") in R1_CONTROLLED_ROLES}
    table_privileges = _list("account_registry_effective_table_privileges")
    expected_table_keys = {
        (role, table) for role in active_roles for table in ACCOUNT_REGISTRY_TABLES
    }
    actual_table_keys = {(item.get("role"), item.get("table")) for item in table_privileges}
    if len(actual_table_keys) != len(table_privileges) or actual_table_keys != expected_table_keys:
        raise BackupError("restored account registry table privilege matrix is incomplete")
    if any(
        any(
            item.get(privilege) is not False
            for privilege in ("select", "insert", "update", "delete")
        )
        for item in table_privileges
    ):
        raise BackupError("restored account registry table has an unexpected privilege")

    function_privileges = _list("account_registry_effective_function_privileges")
    expected_function_keys = {
        (role, schema, name, args)
        for role in active_roles
        for schema, name, args in expected_functions
    }
    actual_function_keys = {
        (item.get("role"), item.get("schema"), item.get("name"), item.get("identity_arguments"))
        for item in function_privileges
    }
    if (
        len(actual_function_keys) != len(function_privileges)
        or actual_function_keys != expected_function_keys
    ):
        raise BackupError("restored account registry function privilege matrix is incomplete")
    for item in function_privileges:
        key = cast(tuple[str, str], (item.get("schema"), item.get("name")))
        executor = ACCOUNT_REGISTRY_FUNCTION_EXECUTORS.get(key)
        if item.get("execute") is not (item.get("role") == executor):
            raise BackupError("restored account registry function privilege matrix is invalid")


def _validate_evidence_unlock_security(metadata: dict[str, Any]) -> None:
    def _list(name: str) -> list[dict[str, Any]]:
        value = metadata.get(name)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise BackupError(f"restored evidence unlock metadata is invalid: {name}")
        return cast(list[dict[str, Any]], value)

    def _grantable(value: Any) -> bool:
        return value is True or value is False or value in {"YES", "NO"}

    owner = metadata.get("database_owner")
    if not isinstance(owner, str):
        raise BackupError("restored evidence unlock database owner is invalid")
    row_counts = metadata.get("evidence_unlock_row_counts")
    if (
        not isinstance(row_counts, dict)
        or set(row_counts) != set(EVIDENCE_UNLOCK_TABLES)
        or any(not isinstance(value, int) or value < 0 for value in row_counts.values())
    ):
        raise BackupError("restored evidence unlock row-count metadata is invalid")

    tables = _list("evidence_unlock_tables")
    expected_tables = {(schema, table) for table, schema in EVIDENCE_UNLOCK_TABLE_SCHEMAS.items()}
    actual_tables = {(item.get("schema"), item.get("table")) for item in tables}
    if (
        len(actual_tables) != len(tables)
        or actual_tables != expected_tables
        or any(item.get("owner") != owner or item.get("kind") != "r" for item in tables)
    ):
        raise BackupError("restored evidence unlock table boundary is invalid")

    functions = _list("evidence_unlock_functions")
    expected_functions = {
        (EVIDENCE_UNLOCK_FUNCTION_SCHEMAS[name], name, arguments)
        for name, arguments in EVIDENCE_UNLOCK_FUNCTION_SIGNATURES.items()
    }
    actual_functions = {
        (item.get("schema"), item.get("name"), item.get("identity_arguments")) for item in functions
    }
    if len(actual_functions) != len(functions) or actual_functions != expected_functions:
        raise BackupError("restored evidence unlock functions differ from the required baseline")
    for item in functions:
        name = cast(str, item.get("name"))
        if (
            item.get("owner") != owner
            or item.get("security_definer")
            is not (name in EVIDENCE_UNLOCK_SECURITY_DEFINER_FUNCTIONS)
            or item.get("proconfig") != ["search_path=pg_catalog"]
            or item.get("result") != EVIDENCE_UNLOCK_FUNCTION_RESULTS[name]
        ):
            raise BackupError("restored evidence unlock function boundary is invalid")

    triggers = _list("evidence_unlock_triggers")
    actual_triggers = {
        item.get("name"): (
            item.get("schema"),
            item.get("table"),
            item.get("function_name"),
        )
        for item in triggers
    }
    if (
        len(actual_triggers) != len(triggers)
        or actual_triggers != EVIDENCE_UNLOCK_TRIGGER_CONTRACT
        or any(
            item.get("enabled") != "O" or item.get("constraint") is not False for item in triggers
        )
    ):
        raise BackupError("restored evidence unlock trigger contract is invalid")

    table_acls = _list("evidence_unlock_table_acls")
    table_acl_keys = {
        (item.get("schema"), item.get("table"), item.get("grantee"), item.get("privilege"))
        for item in table_acls
    }
    if len(table_acl_keys) != len(table_acls) or any(
        (item.get("schema"), item.get("table")) not in expected_tables
        or item.get("grantee") != owner
        or not _grantable(item.get("grantable"))
        for item in table_acls
    ):
        raise BackupError("restored evidence unlock table ACL contains an excess grant")

    function_acls = _list("evidence_unlock_function_acls")
    function_acl_keys = {
        (
            item.get("schema"),
            item.get("name"),
            item.get("identity_arguments"),
            item.get("grantee"),
            item.get("privilege"),
        )
        for item in function_acls
    }
    if len(function_acl_keys) != len(function_acls):
        raise BackupError("restored evidence unlock function ACL contains a duplicate")
    for item in function_acls:
        name = cast(str, item.get("name"))
        executor = EVIDENCE_UNLOCK_FUNCTION_EXECUTORS.get(name)
        allowed_grantees = {owner} if executor is None else {owner, executor}
        if (
            item.get("grantee") not in allowed_grantees
            or item.get("privilege") != "EXECUTE"
            or not _grantable(item.get("grantable"))
            or (item.get("grantee") != owner and item.get("grantable") not in {False, "NO"})
        ):
            raise BackupError("restored evidence unlock function ACL contains an excess grant")

    roles = _list("r1_role_matrix")
    active_roles = {item.get("role") for item in roles if item.get("role") in R1_CONTROLLED_ROLES}
    table_privileges = _list("evidence_unlock_effective_table_privileges")
    expected_table_keys = {
        (role, schema, table) for role in active_roles for schema, table in expected_tables
    }
    actual_table_keys = {
        (item.get("role"), item.get("schema"), item.get("table")) for item in table_privileges
    }
    if len(actual_table_keys) != len(table_privileges) or actual_table_keys != expected_table_keys:
        raise BackupError("restored evidence unlock table privilege matrix is incomplete")
    if any(
        any(
            item.get(privilege) is not False
            for privilege in ("select", "insert", "update", "delete")
        )
        for item in table_privileges
    ):
        raise BackupError("restored evidence unlock table has an unexpected privilege")

    function_privileges = _list("evidence_unlock_effective_function_privileges")
    expected_function_keys = {
        (role, schema, name, arguments)
        for role in active_roles
        for schema, name, arguments in expected_functions
    }
    actual_function_keys = {
        (item.get("role"), item.get("schema"), item.get("name"), item.get("identity_arguments"))
        for item in function_privileges
    }
    if (
        len(actual_function_keys) != len(function_privileges)
        or actual_function_keys != expected_function_keys
    ):
        raise BackupError("restored evidence unlock function privilege matrix is incomplete")
    for item in function_privileges:
        executor = EVIDENCE_UNLOCK_FUNCTION_EXECUTORS.get(cast(str, item.get("name")))
        if item.get("execute") is not (item.get("role") == executor):
            raise BackupError("restored evidence unlock function privilege matrix is invalid")


def _validate_company_reporting_security(metadata: dict[str, Any]) -> None:
    def _list(name: str) -> list[dict[str, Any]]:
        value = metadata.get(name)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise BackupError(f"restored company reporting metadata is invalid: {name}")
        return cast(list[dict[str, Any]], value)

    def _grantable(value: Any) -> bool:
        return (
            value is True or value is False or (isinstance(value, str) and value in {"YES", "NO"})
        )

    owner = metadata.get("database_owner")
    if not isinstance(owner, str):
        raise BackupError("restored company reporting database owner is invalid")
    schema = metadata.get("company_reporting_schema")
    if schema != {"schema": COMPANY_REPORTING_SCHEMA, "owner": owner}:
        raise BackupError("restored company reporting schema differs from the required baseline")

    functions = _list("company_reporting_functions")
    expected_function_keys = {
        (COMPANY_REPORTING_SCHEMA, name, arguments)
        for name, arguments in COMPANY_REPORTING_FUNCTION_SIGNATURES.items()
    }
    actual_function_keys = {
        (item.get("schema"), item.get("name"), item.get("identity_arguments")) for item in functions
    }
    if (
        len(actual_function_keys) != len(functions)
        or actual_function_keys != expected_function_keys
    ):
        raise BackupError("restored company reporting functions differ from the required baseline")
    for item in functions:
        name = item.get("name")
        if not isinstance(name, str):
            raise BackupError("restored company reporting function metadata is invalid")
        if (
            item.get("owner") != owner
            or item.get("result") != COMPANY_REPORTING_FUNCTION_RESULTS[name]
            or item.get("security_definer")
            is not (name in COMPANY_REPORTING_SECURITY_DEFINER_FUNCTIONS)
            or item.get("proconfig") != ["search_path=pg_catalog"]
        ):
            raise BackupError("restored company reporting function security boundary is invalid")

    tables = _list("company_reporting_required_tables")
    expected_table_keys = {("public", table) for table in COMPANY_REPORTING_REQUIRED_TABLES}
    actual_table_keys = {(item.get("schema"), item.get("table")) for item in tables}
    if len(actual_table_keys) != len(tables) or actual_table_keys != expected_table_keys:
        raise BackupError("restored company reporting required tables are incomplete")
    if any(item.get("owner") != owner or item.get("kind") != "r" for item in tables):
        raise BackupError("restored company reporting required table boundary is invalid")

    columns = _list("company_reporting_required_columns")
    expected_columns = {
        ("public", table, column, data_type)
        for (table, column), data_type in COMPANY_REPORTING_REQUIRED_COLUMNS.items()
    }
    actual_columns = {
        (item.get("schema"), item.get("table"), item.get("column"), item.get("type"))
        for item in columns
    }
    if len(actual_columns) != len(columns) or actual_columns != expected_columns:
        raise BackupError("restored company reporting immutable snapshot columns are incomplete")

    required_functions = _list("company_reporting_required_functions")
    expected_required_function_keys = set(COMPANY_REPORTING_REQUIRED_FUNCTIONS)
    actual_required_function_keys = {
        (item.get("schema"), item.get("name"), item.get("identity_arguments"))
        for item in required_functions
    }
    if (
        len(actual_required_function_keys) != len(required_functions)
        or actual_required_function_keys != expected_required_function_keys
    ):
        raise BackupError("restored company reporting required functions are incomplete")
    for item in required_functions:
        function_key = cast(
            tuple[str, str, str],
            (
                item.get("schema"),
                item.get("name"),
                item.get("identity_arguments"),
            ),
        )
        if (
            item.get("owner") != owner
            or item.get("result") != COMPANY_REPORTING_REQUIRED_FUNCTIONS[function_key]
            or item.get("security_definer") is not True
            or item.get("proconfig") != ["search_path=pg_catalog"]
        ):
            raise BackupError("restored company reporting required function boundary is invalid")

    triggers = _list("company_reporting_triggers")
    actual_trigger_contract = {
        item.get("name"): (
            item.get("table"),
            item.get("constraint"),
            item.get("trigger_type"),
            item.get("deferrable"),
            item.get("initially_deferred"),
            item.get("function_name"),
        )
        for item in triggers
    }
    if (
        len(actual_trigger_contract) != len(triggers)
        or actual_trigger_contract != COMPANY_REPORTING_TRIGGER_CONTRACT
    ):
        raise BackupError("restored company reporting trigger contract differs from the baseline")
    if any(
        item.get("enabled") != "O" or item.get("function_schema") != "public" for item in triggers
    ):
        raise BackupError("restored company reporting trigger boundary is invalid")

    schema_acls = _list("company_reporting_schema_acls")
    schema_acl_keys: set[tuple[Any, ...]] = set()
    for item in schema_acls:
        schema_acl_key = (item.get("schema"), item.get("grantee"), item.get("privilege"))
        if schema_acl_key in schema_acl_keys:
            raise BackupError("restored company reporting schema ACL has a duplicate entry")
        schema_acl_keys.add(schema_acl_key)
        allowed = {
            owner: {"USAGE", "CREATE"},
            "ledgerbridge_reader": {"USAGE"},
        }.get(cast(str, item.get("grantee")))
        if (
            item.get("schema") != COMPANY_REPORTING_SCHEMA
            or allowed is None
            or item.get("privilege") not in allowed
            or not _grantable(item.get("grantable"))
            or (item.get("grantee") != owner and item.get("grantable") not in {False, "NO"})
        ):
            raise BackupError("restored company reporting schema ACL contains an excess grant")
    if (COMPANY_REPORTING_SCHEMA, "ledgerbridge_reader", "USAGE") not in schema_acl_keys:
        raise BackupError("restored company reporting reader schema ACL is missing")

    function_acls = _list("company_reporting_function_acls")
    function_acl_keys: set[tuple[Any, ...]] = set()
    reader_function_key = (
        COMPANY_REPORTING_SCHEMA,
        "get_company_report_v1_as_of",
        COMPANY_REPORTING_FUNCTION_SIGNATURES["get_company_report_v1_as_of"],
        "ledgerbridge_reader",
        "EXECUTE",
    )
    for item in function_acls:
        object_key = (item.get("schema"), item.get("name"), item.get("identity_arguments"))
        function_acl_key = (*object_key, item.get("grantee"), item.get("privilege"))
        if function_acl_key in function_acl_keys:
            raise BackupError("restored company reporting function ACL has a duplicate entry")
        function_acl_keys.add(function_acl_key)
        allowed_grantees = {owner}
        if item.get("name") == "get_company_report_v1_as_of":
            allowed_grantees.add("ledgerbridge_reader")
        if (
            object_key not in expected_function_keys
            or item.get("grantee") not in allowed_grantees
            or item.get("privilege") != "EXECUTE"
            or not _grantable(item.get("grantable"))
            or (item.get("grantee") != owner and item.get("grantable") not in {False, "NO"})
        ):
            raise BackupError("restored company reporting function ACL contains an excess grant")
    if reader_function_key not in function_acl_keys:
        raise BackupError("restored company reporting reader function ACL is missing")

    roles = _list("r1_role_matrix")
    active_roles = {item.get("role") for item in roles if item.get("role") in R1_CONTROLLED_ROLES}
    schema_privileges = _list("company_reporting_effective_schema_privileges")
    expected_schema_keys = {(role, COMPANY_REPORTING_SCHEMA) for role in active_roles}
    actual_schema_keys = {(item.get("role"), item.get("schema")) for item in schema_privileges}
    if (
        len(actual_schema_keys) != len(schema_privileges)
        or actual_schema_keys != expected_schema_keys
    ):
        raise BackupError("restored company reporting schema privilege matrix is incomplete")
    for item in schema_privileges:
        if (
            item.get("usage") is not (item.get("role") == "ledgerbridge_reader")
            or item.get("create") is not False
        ):
            raise BackupError("restored company reporting schema privilege matrix is invalid")

    function_privileges = _list("company_reporting_effective_function_privileges")
    expected_privilege_keys = {
        (role, *function_key) for role in active_roles for function_key in expected_function_keys
    }
    actual_privilege_keys = {
        (
            item.get("role"),
            item.get("schema"),
            item.get("name"),
            item.get("identity_arguments"),
        )
        for item in function_privileges
    }
    if (
        len(actual_privilege_keys) != len(function_privileges)
        or actual_privilege_keys != expected_privilege_keys
    ):
        raise BackupError("restored company reporting function privilege matrix is incomplete")
    for item in function_privileges:
        expected_execute = (
            item.get("role") == "ledgerbridge_reader"
            and item.get("name") == "get_company_report_v1_as_of"
        )
        if item.get("execute") is not expected_execute:
            raise BackupError("restored company reporting function privilege matrix is invalid")


def _validate_rich_database_security(metadata: dict[str, Any]) -> None:
    if metadata.get("metadata_version") != 2:
        raise BackupError("restored database lacks v2 metadata observations")
    revision = metadata.get("alembic_version")
    if not isinstance(revision, str):
        raise BackupError("restored database revision is invalid")
    legacy_role_retired = _legacy_runtime_role_is_retired(metadata)
    if revision >= R1_SECURITY_REVISION:
        _validate_r1_database_security(metadata)
    if revision >= COUNTERPARTY_SECURITY_REVISION:
        _validate_counterparty_security(metadata)
    if revision >= BANK_STATEMENT_SECURITY_REVISION:
        _validate_bank_statement_security(metadata)
    if revision >= ACCOUNT_REGISTRY_SECURITY_REVISION:
        _validate_account_registry_security(metadata)
    if revision >= COMPANY_REPORTING_SECURITY_REVISION:
        _validate_company_reporting_security(metadata)
    if revision >= EVIDENCE_UNLOCK_SECURITY_REVISION:
        _validate_evidence_unlock_security(metadata)
    if metadata.get("database_temp_denied") is not True:
        raise BackupError("restored database TEMP privilege invariant failed")
    functions = metadata.get("security_functions")
    if not isinstance(functions, list):
        raise BackupError("restored function metadata is invalid")
    function_names: set[str] = set()
    for value in functions:
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            raise BackupError("restored function metadata is invalid")
        function_names.add(value["name"])
        proconfig = value.get("proconfig")
        if not isinstance(proconfig, list) or "search_path=pg_catalog" not in proconfig:
            raise BackupError("restored security function search path is not pinned")
    required_functions = set(PHASE_1_FUNCTIONS)
    if revision >= "20260821_0003":
        required_functions.update(PHASE_2_FUNCTIONS)
    if revision >= "20260822_0004":
        required_functions.update(PHASE_3_FUNCTIONS)
    missing_functions = sorted(required_functions - function_names)
    if missing_functions:
        raise BackupError(
            f"restored database lacks required security functions: {', '.join(missing_functions)}"
        )
    triggers = metadata.get("public_triggers")
    if not isinstance(triggers, list) or not triggers:
        raise BackupError("restored trigger metadata is invalid")
    disabled = [
        value.get("name", "invalid")
        for value in triggers
        if not isinstance(value, dict) or value.get("enabled") != "O"
    ]
    if disabled:
        raise BackupError(f"restored database has disabled triggers: {', '.join(disabled)}")
    required_triggers = set(PHASE_1_TRIGGERS)
    if revision >= "20260821_0003":
        required_triggers.update(PHASE_2_TRIGGERS)
    if revision >= "20260822_0004":
        required_triggers.update(PHASE_3_TRIGGERS)
    trigger_names = {
        value.get("name")
        for value in triggers
        if isinstance(value, dict) and isinstance(value.get("name"), str)
    }
    missing_triggers = sorted(required_triggers - trigger_names)
    if missing_triggers:
        raise BackupError(
            "restored database lacks required triggers: " + ", ".join(missing_triggers)
        )

    table_grants = metadata.get("table_grants")
    sequence_grants = metadata.get("sequence_grants")
    function_grants = metadata.get("function_grants")
    if not isinstance(table_grants, list):
        raise BackupError("restored grant metadata is invalid: table_grants")
    if not isinstance(sequence_grants, list):
        raise BackupError("restored grant metadata is invalid: sequence_grants")
    if not isinstance(function_grants, list):
        raise BackupError("restored grant metadata is invalid: function_grants")

    expected_table_grants: set[tuple[str, str]] = set()
    if not legacy_role_retired:
        expected_table_grants.update(PHASE_1_TABLE_PRIVILEGES)
        if revision >= "20260821_0003":
            expected_table_grants.update(PHASE_2_TABLE_PRIVILEGES)
        if revision >= "20260822_0004":
            expected_table_grants.update(PHASE_3_TABLE_PRIVILEGES)
    actual_table_grants: set[tuple[str, str]] = set()
    for value in table_grants:
        if not isinstance(value, dict):
            raise BackupError("restored table grant metadata is invalid")
        table = value.get("table")
        privilege = value.get("privilege")
        if not isinstance(table, str) or not isinstance(privilege, str):
            raise BackupError("restored table grant metadata is invalid")
        if value.get("grantable") != "NO":
            raise BackupError("restored runtime table grant is grantable")
        actual_table_grants.add((table, privilege))
    if actual_table_grants != expected_table_grants:
        raise BackupError("restored runtime table grants differ from the required baseline")

    column_grants = metadata.get("column_grants")
    if not isinstance(column_grants, list):
        raise BackupError("restored grant metadata is invalid: column_grants")
    expected_column_grants: set[tuple[str, str, str]] = set()
    if not legacy_role_retired:
        if revision >= "20260821_0003":
            expected_column_grants.update(PHASE_2_COLUMN_PRIVILEGES)
        if revision >= "20260822_0004":
            expected_column_grants.update(PHASE_3_COLUMN_PRIVILEGES)
    actual_column_grants: set[tuple[str, str, str]] = set()
    for value in column_grants:
        if not isinstance(value, dict):
            raise BackupError("restored column grant metadata is invalid")
        table = value.get("table")
        column = value.get("column")
        privilege = value.get("privilege")
        if (
            not isinstance(table, str)
            or not isinstance(column, str)
            or not isinstance(privilege, str)
        ):
            raise BackupError("restored column grant metadata is invalid")
        if value.get("grantable") != "NO":
            raise BackupError("restored runtime column grant is grantable")
        actual_column_grants.add((table, column, privilege))
    if actual_column_grants != expected_column_grants:
        raise BackupError("restored runtime column grants differ from the required baseline")

    if sequence_grants:
        raise BackupError("restored runtime role has unexpected sequence grants")
    runtime_function_grants: set[tuple[str, str]] = set()
    for value in function_grants:
        if not isinstance(value, dict):
            raise BackupError("restored function grant metadata is invalid")
        if value.get("grantee") != "ledgerbridge_app":
            continue
        function = value.get("function")
        privilege = value.get("privilege")
        if not isinstance(function, str) or not isinstance(privilege, str):
            raise BackupError("restored function grant metadata is invalid")
        if value.get("grantable") != "NO":
            raise BackupError("restored runtime function grant is grantable")
        runtime_function_grants.add((function, privilege))
    expected_function_grants = set() if legacy_role_retired else {("append_audit_event", "EXECUTE")}
    if runtime_function_grants != expected_function_grants:
        raise BackupError("restored runtime function grants differ from the required baseline")


def _deployment_root(
    runner: Runner,
    verifier_project_dir: Path,
    archive: Path,
    destination: Path,
    revision: str,
) -> Path:
    _safe_extract_tar(archive, destination)
    children = list(destination.iterdir())
    if len(children) != 1 or not children[0].is_dir() or children[0].is_symlink():
        raise BackupError("deployment archive must contain exactly one top-level directory")
    root = children[0]
    archived_revision = (root / "DEPLOYED_REVISION").read_text(encoding="utf-8").strip()
    if not hmac.compare_digest(archived_revision, revision):
        raise BackupError("restored deployment revision differs from backup metadata")
    _validate_private_file(root / ".env", "restored deployment .env")
    _verify_deployment_manifest(runner, verifier_project_dir, root, revision)
    return root


def _restore_artifacts(
    runner: Runner,
    *,
    image: str,
    volume: str,
    work_dir: Path,
    archive: Path,
) -> str:
    runner.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "DAC_READ_SEARCH",
            "--security-opt",
            "no-new-privileges",
            "-v",
            f"{volume}:/target:rw",
            "-v",
            f"{work_dir}:/backup:ro",
            image,
            "tar",
            "-C",
            "/target",
            "-xf",
            f"/backup/{archive.name}",
        ]
    )
    _deterministic_artifact_tar(
        runner,
        image=image,
        volume=volume,
        destination_dir=work_dir,
        output="restored-artifacts.tar",
    )
    return _sha256(work_dir / "restored-artifacts.tar")


def _runtime_identity(
    runner: Runner,
    *,
    image: str,
    network: str,
    database_url: str,
) -> str:
    process_env = os.environ.copy()
    process_env["LEDGERBRIDGE_DATABASE_URL"] = database_url
    return runner.capture(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "-e",
            "LEDGERBRIDGE_DATABASE_URL",
            image,
            "python",
            "-c",
            RUNTIME_IDENTITY_PROGRAM,
        ],
        env=process_env,
    )


def rehearse_restore(config: CommonConfig, backup: Path, runner: Runner | None = None) -> Path:
    """Restore a backup into fresh isolated resources and prove all invariants."""
    runner = runner or Runner()
    config = _validated_config(config, runner)
    backup = _validate_backup_directory(config, backup)
    sidecar, cipher = _validate_backup_sidecar(config, backup)
    revision = cast(str, sidecar["revision"])
    before = _collect_source_state(config, runner)
    resources = RestoreResources.create(secrets.token_hex(4))
    work_dir = Path(tempfile.mkdtemp(prefix="ledgerbridge-restore-", dir=config.work_root))
    started_at = _now()
    try:
        work_dir.chmod(0o700)
        payload = work_dir / "payload.tar"
        runner.run(
            [
                "gpg",
                "--homedir",
                str(config.gpg_home),
                "--batch",
                "--yes",
                "--output",
                str(payload),
                "--decrypt",
                str(cipher),
            ]
        )
        extracted = work_dir / "payload"
        _safe_extract_tar(
            payload,
            extracted,
            expected_files={*PAYLOAD_COMPONENTS, "PAYLOAD.sha256"},
        )
        _verify_payload_hashes(extracted)
        metadata = _load_json(extracted / "metadata.json", "encrypted backup metadata")
        source_format = cast(str, sidecar["format"])
        expected_metadata_keys = {
            "format",
            "created_at",
            "revision",
            "api_image",
            "artifact_volume",
            "database",
            "artifact_archive_sha256",
            "deployment_tree_sha256",
        }
        rich_format = source_format in {BACKUP_FORMAT_V2, BACKUP_FORMAT_V3}
        if rich_format:
            expected_metadata_keys.add("artifact_control")
        if source_format == BACKUP_FORMAT_V3:
            expected_metadata_keys.add("api_image_id")
        if (
            set(metadata) != expected_metadata_keys
            or metadata.get("format") != source_format
            or metadata.get("revision") != revision
        ):
            raise BackupError("encrypted metadata does not match the backup sidecar")
        backup_image = _validate_backup_image(
            runner,
            metadata.get("api_image"),
            metadata.get("api_image_id") if source_format == BACKUP_FORMAT_V3 else None,
            revision,
        )
        expected_database = metadata.get("database")
        if not isinstance(expected_database, dict):
            raise BackupError("encrypted database metadata is invalid")
        expected_database = cast(dict[str, Any], expected_database)
        database_name = _database_name(expected_database)
        archive_digests = (
            ("artifacts.tar", "artifact_archive_sha256"),
            ("deployment-tree.tar", "deployment_tree_sha256"),
        )
        for filename, field in archive_digests:
            expected_digest = metadata.get(field)
            if (
                not isinstance(expected_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
                or not hmac.compare_digest(_sha256(extracted / filename), expected_digest)
            ):
                raise BackupError(f"encrypted metadata digest differs: {field}")
        _validate_tar_archive(extracted / "artifacts.tar")

        runner.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                config.postgres_image,
                "pg_restore",
                "--list",
            ],
            stdin_path=extracted / "database.dump",
        )
        deployment = _deployment_root(
            runner,
            config.project_dir,
            extracted / "deployment-tree.tar",
            work_dir / "deployment",
            revision,
        )
        deployment_quota = _artifact_quota_config(deployment)
        artifact_observation = _artifact_archive_metadata(
            extracted / "artifacts.tar", deployment_quota
        )
        source_artifact_control = metadata.get("artifact_control")
        if rich_format:
            if not isinstance(source_artifact_control, dict):
                raise BackupError("encrypted artifact-control metadata is invalid")
            if source_artifact_control != artifact_observation:
                raise BackupError("restored artifact-control metadata differs from backup")

        try:
            runner.run(["docker", "network", "create", "--internal", resources.network])
            runner.run(["docker", "volume", "create", resources.database_volume])
            runner.run(["docker", "volume", "create", resources.artifact_volume])
            postgres_password = secrets.token_urlsafe(32)
            process_env = os.environ.copy()
            process_env["POSTGRES_PASSWORD"] = postgres_password
            runner.capture(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    resources.container,
                    "--network",
                    resources.network,
                    "--security-opt",
                    "no-new-privileges",
                    "--pids-limit",
                    "128",
                    "--memory",
                    "512m",
                    "-e",
                    "POSTGRES_PASSWORD",
                    "-e",
                    "POSTGRES_INITDB_ARGS=--data-checksums",
                    "-v",
                    f"{resources.database_volume}:/var/lib/postgresql/data",
                    config.postgres_image,
                ],
                env=process_env,
            )
            _wait_for_postgres(runner, resources.container)
            runner.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    resources.container,
                    "psql",
                    "--no-psqlrc",
                    "--username",
                    "postgres",
                    "--dbname",
                    "postgres",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "--file",
                    "-",
                ],
                stdin_path=extracted / "roles.sql",
            )
            runner.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    resources.container,
                    "pg_restore",
                    "--username",
                    "postgres",
                    "--dbname",
                    "postgres",
                    "--create",
                    "--exit-on-error",
                ],
                stdin_path=extracted / "database.dump",
            )
            actual_database = _database_metadata(runner, resources.container, database_name)
            compared_database_fields = _validate_restored_database(
                expected_database, actual_database
            )

            artifact_digest = _restore_artifacts(
                runner,
                image=backup_image,
                volume=resources.artifact_volume,
                work_dir=extracted,
                archive=extracted / "artifacts.tar",
            )
            expected_artifact_digest = metadata.get("artifact_archive_sha256")
            if not isinstance(expected_artifact_digest, str) or not hmac.compare_digest(
                artifact_digest, expected_artifact_digest
            ):
                raise BackupError("restored artifact volume digest differs from backup")
            if rich_format and (
                actual_database.get("artifact_count") != artifact_observation.get("artifact_count")
                or actual_database.get("artifact_manifest_sha256")
                != artifact_observation.get("artifact_manifest_sha256")
            ):
                raise BackupError(
                    "restored database artifact manifest differs from the artifact archive"
                )

            environment = _parse_env(deployment / ".env")
            if _legacy_runtime_role_is_retired(expected_database):
                runtime_identities = {
                    "LEDGERBRIDGE_API_DATABASE_URL": "ledgerbridge_api",
                    "LEDGERBRIDGE_WORKER_DATABASE_URL": "ledgerbridge_worker",
                    "LEDGERBRIDGE_READER_DATABASE_URL": "ledgerbridge_reader",
                }
            else:
                runtime_identities = {
                    "LEDGERBRIDGE_DATABASE_URL": "ledgerbridge_app",
                }
            for variable, expected_role in runtime_identities.items():
                source_url = environment.get(variable)
                if source_url is None:
                    raise BackupError(f"deployment .env lacks {variable}")
                restored_url = _replace_database_host(source_url, resources.container)
                identity = _runtime_identity(
                    runner,
                    image=backup_image,
                    network=resources.network,
                    database_url=restored_url,
                )
                expected_identity = f"{expected_role}|{expected_role}"
                if not hmac.compare_digest(identity, expected_identity):
                    raise BackupError(f"application image did not connect as {expected_role}")
        finally:
            _cleanup_restore_resources(runner, resources)
            after = _collect_source_state(config, runner)
            _assert_source_unchanged(before, after)

        report = backup / f"restore-rehearsal-{_timestamp()}.json"
        _write_json(
            report,
            {
                "format": RESTORE_REPORT_FORMAT,
                "status": "passed",
                "started_at": started_at.isoformat(),
                "completed_at": _now().isoformat(),
                "backup": backup.name,
                "revision": revision,
                "source_format": source_format.removeprefix("ledgerbridge-encrypted-backup-"),
                "database": database_name,
                "database_compared_fields": compared_database_fields,
                "source_database_metadata": expected_database,
                "post_restore_database_observations": actual_database,
                "unpaired_database_observation_fields": (
                    [] if rich_format else sorted(set(actual_database) - set(expected_database))
                ),
                "source_artifact_control": source_artifact_control,
                "post_restore_artifact_observations": artifact_observation,
                "artifact_archive_sha256": metadata["artifact_archive_sha256"],
                "deployment_tree_sha256": metadata["deployment_tree_sha256"],
                "connector_runner_boundary_present": (
                    "connector-runner:"
                    in (deployment / "docker-compose.yml").read_text(encoding="utf-8")
                ),
                "production_unchanged": True,
                "isolated_resources_removed": True,
            },
        )
        return report
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _common_config(args: argparse.Namespace) -> CommonConfig:
    return CommonConfig(
        project_dir=args.project_dir,
        backup_root=args.backup_root,
        work_root=args.work_root,
        gpg_home=args.gpg_home,
        fingerprint=args.fingerprint,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path("/srv/ai-center/ledgerbridge"))
    parser.add_argument(
        "--backup-root", type=Path, default=Path("/srv/ai-center/backups/ledgerbridge")
    )
    parser.add_argument(
        "--work-root",
        type=Path,
        default=Path("/dev/shm"),  # nosec B108
    )
    parser.add_argument(
        "--gpg-home",
        type=Path,
        default=Path("/srv/ai-center/ledgerbridge-secrets/backup-gnupg"),
    )
    parser.add_argument("--fingerprint", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("backup")
    rehearse = commands.add_parser("rehearse")
    rehearse.add_argument("--backup", required=True, type=Path)
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        config = _common_config(args)
        if args.command == "backup":
            result = create_backup(config)
            print(f"encrypted backup created: {result}")
            return
        report = rehearse_restore(config, args.backup)
        print(f"isolated restore rehearsal passed: {report}")
    except (BackupError, OSError, json.JSONDecodeError, tarfile.TarError) as error:
        print(f"backup_restore: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
