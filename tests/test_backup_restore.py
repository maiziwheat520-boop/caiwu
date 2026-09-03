from __future__ import annotations

import hashlib
import io
import tarfile
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import scripts.backup_restore as backup_restore_module
from scripts.backup_restore import (
    ACCOUNT_REGISTRY_FUNCTION_EXECUTORS,
    ACCOUNT_REGISTRY_FUNCTION_RESULTS,
    ACCOUNT_REGISTRY_FUNCTION_SIGNATURES,
    ACCOUNT_REGISTRY_MANAGED_ACCOUNT_CONSTRAINT_CONTRACT,
    ACCOUNT_REGISTRY_MANAGED_ACCOUNT_TRIGGER_CONTRACT,
    ACCOUNT_REGISTRY_SECURITY_DEFINER_FUNCTIONS,
    ACCOUNT_REGISTRY_SECURITY_SQL,
    ACCOUNT_REGISTRY_TABLES,
    ACCOUNT_REGISTRY_TRIGGER_CONTRACT,
    BACKUP_FORMAT_V1,
    BACKUP_FORMAT_V2,
    BACKUP_FORMAT_V3,
    BANK_STATEMENT_CONSTRAINT_CONTRACT,
    BANK_STATEMENT_FUNCTION_RESULTS,
    BANK_STATEMENT_FUNCTION_SIGNATURES,
    BANK_STATEMENT_SECURITY_DEFINER_FUNCTIONS,
    BANK_STATEMENT_SECURITY_SQL,
    BANK_STATEMENT_TABLES,
    BANK_STATEMENT_TRIGGER_CONTRACT,
    CASH_RECONCILIATION_FUNCTION_KEYS,
    CASH_RECONCILIATION_TRIGGER_NAMES,
    CASH_RECONCILIATION_V2_FUNCTION_KEYS,
    CLASSIFICATION_BATCH_CONSTRAINT_DEFINITION_MARKERS,
    CLASSIFICATION_BATCH_CONSTRAINT_TABLES,
    CLASSIFICATION_BATCH_FUNCTION_EXECUTORS,
    CLASSIFICATION_BATCH_FUNCTION_RESULTS,
    CLASSIFICATION_BATCH_FUNCTION_SIGNATURES,
    CLASSIFICATION_BATCH_REQUIRED_COLUMNS,
    CLASSIFICATION_BATCH_REQUIRED_CONSTRAINTS,
    CLASSIFICATION_BATCH_SECURITY_SQL,
    CLASSIFICATION_BATCH_TABLES,
    CLASSIFICATION_BATCH_TRIGGER_CONTRACT,
    COMPANY_REPORTING_BASE_REQUIRED_TABLES,
    COMPANY_REPORTING_FUNCTION_RESULTS,
    COMPANY_REPORTING_FUNCTION_SIGNATURES,
    COMPANY_REPORTING_READER_FUNCTIONS,
    COMPANY_REPORTING_REQUIRED_COLUMNS,
    COMPANY_REPORTING_REQUIRED_TABLES,
    COMPANY_REPORTING_SCHEMA,
    COMPANY_REPORTING_SECURITY_DEFINER_FUNCTIONS,
    COMPANY_REPORTING_SECURITY_SQL,
    COMPANY_REPORTING_TRIGGER_CONTRACT,
    COUNTERPARTY_CONSTRAINT_CONTRACT,
    COUNTERPARTY_FUNCTION_RESULTS,
    COUNTERPARTY_FUNCTION_SIGNATURES,
    COUNTERPARTY_PROTECTED_TABLES,
    COUNTERPARTY_SECURITY_SQL,
    COUNTERPARTY_TRIGGER_CONTRACT,
    PHASE_1_FUNCTIONS,
    PHASE_1_TABLE_PRIVILEGES,
    PHASE_1_TRIGGERS,
    PHASE_2_COLUMN_PRIVILEGES,
    PHASE_2_FUNCTIONS,
    PHASE_2_TABLE_PRIVILEGES,
    PHASE_2_TRIGGERS,
    PHASE_3_COLUMN_PRIVILEGES,
    PHASE_3_FUNCTIONS,
    PHASE_3_TABLE_PRIVILEGES,
    PHASE_3_TRIGGERS,
    R1_CUTOVER_INVENTORY_TABLES,
    R1_INTERNAL_READ_FUNCTION_SIGNATURES,
    R1_INTERNAL_READ_FUNCTIONS,
    R1_INTERNAL_READ_VIEWS,
    R1_OPTIONAL_ROLES,
    R1_PUBLIC_TABLES,
    R1_REQUIRED_CONSTRAINTS,
    R1_REQUIRED_TRIGGERS,
    R1_ROLES,
    R1_SECURITY_SQL,
    BackupError,
    CommonConfig,
    CutoverInventory,
    RestoreResources,
    Runner,
    SourceState,
    _artifact_archive_metadata,
    _assert_source_unchanged,
    _normalize_fingerprint,
    _replace_database_host,
    _restore_artifacts,
    _safe_extract_tar,
    _validate_backup_image,
    _validate_classification_batch_security,
    _validate_company_reporting_security,
    _validate_r1_database_security,
    _validate_restored_database,
    _verify_payload_hashes,
    _write_payload_hashes,
    create_backup,
    validate_mybank_cutover_inventory_sequence,
    validate_mybank_existing_account_inventory_sequence,
)

FINGERPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"


def test_restored_artifacts_are_owned_and_readable_by_runtime_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    class RecordingRunner:
        def run(self, command: list[str], **_: object) -> None:
            commands.append(command)

    def fake_deterministic_tar(
        _: object,
        *,
        image: str,
        volume: str,
        destination_dir: Path,
        output: str,
    ) -> None:
        assert image == "ledgerbridge-app:abcdef0"
        assert volume == "restored-artifacts"
        (destination_dir / output).write_bytes(b"restored")

    monkeypatch.setattr(
        backup_restore_module, "_deterministic_artifact_tar", fake_deterministic_tar
    )
    archive = tmp_path / "artifacts.tar"
    archive.write_bytes(b"archive")

    digest = _restore_artifacts(
        RecordingRunner(),  # type: ignore[arg-type]
        image="ledgerbridge-app:abcdef0",
        volume="restored-artifacts",
        work_dir=tmp_path,
        archive=archive,
    )

    assert digest == hashlib.sha256(b"restored").hexdigest()
    assert any(
        command[-4:] == ["chown", "-R", "10001:10001", "/target"] and "CHOWN" in command
        for command in commands
    )
    assert any(
        "10001:10001" in command
        and command[-6:] == ["tar", "-C", "/target", "-cf", "/dev/null", "."]
        for command in commands
    )


def _cutover_inventory(
    *,
    schema_revision: str = "20260830_0023",
    audit_events: int = 1_000,
    candidate_total: int = 20,
    latest_pending: int = 14,
    changes: dict[str, int] | None = None,
) -> CutoverInventory:
    row_counts = {table: 0 for table in R1_CUTOVER_INVENTORY_TABLES}
    row_counts.update(changes or {})
    return CutoverInventory(
        schema_revision=schema_revision,
        candidate_total=candidate_total,
        latest_pending_candidates=latest_pending,
        audit_events=audit_events,
        row_counts=tuple(sorted(row_counts.items())),
    )


def test_mybank_restore_inventory_accepts_current_integrated_schema_revision() -> None:
    inventory = _cutover_inventory(schema_revision="20260901_0027")

    assert inventory.schema_revision == "20260901_0027"


@pytest.mark.parametrize(
    "schema_revision",
    ("20260902_0031", "20260902_0032", "20260902_0033"),
)
def test_bank_statement_restore_inventory_accepts_reviewed_profile_revision(
    schema_revision: str,
) -> None:
    inventory = _cutover_inventory(schema_revision=schema_revision)

    assert inventory.schema_revision == schema_revision


def test_account_registry_privilege_probe_uses_catalog_function_oid() -> None:
    assert "p.oid function_oid" in ACCOUNT_REGISTRY_SECURITY_SQL
    assert (
        "has_function_privilege(r.role_name::text,f.function_oid,'EXECUTE')"
        in ACCOUNT_REGISTRY_SECURITY_SQL
    )
    assert "format('%I.%I(%s)',f.schema_name,f.function_name,f.identity_arguments)" not in (
        ACCOUNT_REGISTRY_SECURITY_SQL
    )


def test_mybank_restore_inventory_rejects_unreviewed_future_schema_revision() -> None:
    with pytest.raises(BackupError, match="schema revision"):
        _cutover_inventory(schema_revision="20260901_0030")


def test_mybank_restore_inventory_accepts_exact_import_replay_and_conflict_sequence() -> None:
    before = _cutover_inventory()
    after = _cutover_inventory(
        audit_events=1_014,
        changes={
            "evidence_object": 1,
            "encrypted_object_identity": 1,
            "encrypted_blob_version": 1,
            "managed_account": 1,
            "managed_account_lifecycle": 1,
            "account_registry_operation": 1,
            "managed_account_alias": 1,
            "bank_statement": 1,
            "bank_statement_transaction": 3,
            "bank_statement_observation": 3,
            "bank_statement_review": 1,
        },
    )

    report = validate_mybank_cutover_inventory_sequence(
        before=before,
        after=after,
        replay=after,
        conflict=after,
        transaction_count=3,
        alias_count=1,
        assignment_count=0,
    )

    assert report["candidate_total"] == 20
    assert report["latest_pending_candidates"] == 14
    assert report["audit_event_delta"] == 14
    assert report["replay_delta"] == 0
    assert report["conflict_delta"] == 0


def test_mybank_restore_inventory_rejects_unrelated_r1_table_drift() -> None:
    before = _cutover_inventory()
    after = _cutover_inventory(
        audit_events=1_014,
        changes={
            "evidence_object": 1,
            "encrypted_object_identity": 1,
            "encrypted_blob_version": 1,
            "managed_account": 1,
            "managed_account_lifecycle": 1,
            "account_registry_operation": 1,
            "managed_account_alias": 1,
            "bank_statement": 1,
            "bank_statement_transaction": 3,
            "bank_statement_observation": 3,
            "bank_statement_review": 1,
            "candidate_event": 1,
        },
    )

    with pytest.raises(BackupError, match="unrelated"):
        validate_mybank_cutover_inventory_sequence(
            before=before,
            after=after,
            replay=after,
            conflict=after,
            transaction_count=3,
            alias_count=1,
            assignment_count=0,
        )


def test_mybank_existing_account_inventory_preserves_registry_and_posting_facts() -> None:
    before = _cutover_inventory(
        schema_revision="20260901_0029",
        changes={
            "managed_account": 5,
            "managed_account_lifecycle": 5,
            "account_registry_operation": 5,
            "managed_account_alias": 5,
            "account_business_unit_assignment": 5,
            "journal_entry": 3,
            "posting": 6,
        },
    )
    after = _cutover_inventory(
        schema_revision="20260901_0029",
        audit_events=1_010,
        changes={
            "managed_account": 5,
            "managed_account_lifecycle": 5,
            "account_registry_operation": 5,
            "managed_account_alias": 5,
            "account_business_unit_assignment": 5,
            "journal_entry": 3,
            "posting": 6,
            "evidence_object": 1,
            "encrypted_object_identity": 1,
            "encrypted_blob_version": 1,
            "bank_statement": 1,
            "bank_statement_transaction": 3,
            "bank_statement_observation": 3,
            "bank_statement_review": 1,
        },
    )

    report = validate_mybank_existing_account_inventory_sequence(
        before=before,
        after=after,
        replay=after,
        conflict=after,
        transaction_count=3,
        evidence_mode="CREATE_NEW",
    )

    assert report["audit_event_delta"] == 10
    assert report["replay_delta"] == report["conflict_delta"] == 0


def test_mybank_existing_account_inventory_reuses_evidence_without_new_blob() -> None:
    before = _cutover_inventory(
        schema_revision="20260901_0029",
        changes={
            "evidence_object": 1,
            "encrypted_object_identity": 1,
            "encrypted_blob_version": 1,
            "managed_account": 1,
        },
    )
    after = _cutover_inventory(
        schema_revision="20260901_0029",
        audit_events=1_006,
        changes={
            "evidence_object": 1,
            "encrypted_object_identity": 1,
            "encrypted_blob_version": 1,
            "managed_account": 1,
            "bank_statement": 1,
            "bank_statement_transaction": 2,
            "bank_statement_observation": 2,
            "bank_statement_review": 1,
        },
    )

    report = validate_mybank_existing_account_inventory_sequence(
        before=before,
        after=after,
        replay=after,
        conflict=after,
        transaction_count=2,
        evidence_mode="REUSE_EXISTING",
    )

    assert report["audit_event_delta"] == 6


def test_mybank_existing_account_inventory_rejects_unknown_evidence_mode() -> None:
    inventory = _cutover_inventory(schema_revision="20260901_0029")

    with pytest.raises(BackupError, match="evidence mode"):
        validate_mybank_existing_account_inventory_sequence(
            before=inventory,
            after=inventory,
            replay=inventory,
            conflict=inventory,
            transaction_count=2,
            evidence_mode="AUTO",
        )


@pytest.mark.parametrize(
    "table", ["managed_account", "candidate_event", "journal_entry", "posting"]
)
def test_mybank_existing_account_inventory_rejects_unrelated_writes(table: str) -> None:
    before = _cutover_inventory(schema_revision="20260901_0029")
    changes = {
        "evidence_object": 1,
        "encrypted_object_identity": 1,
        "encrypted_blob_version": 1,
        "bank_statement": 1,
        "bank_statement_transaction": 2,
        "bank_statement_observation": 2,
        "bank_statement_review": 1,
        table: 1,
    }
    after = _cutover_inventory(
        schema_revision="20260901_0029",
        audit_events=1_008,
        changes=changes,
    )

    with pytest.raises(BackupError, match="unrelated"):
        validate_mybank_existing_account_inventory_sequence(
            before=before,
            after=after,
            replay=after,
            conflict=after,
            transaction_count=2,
            evidence_mode="CREATE_NEW",
        )


def _source_state() -> SourceState:
    return SourceState(
        revision="a" * 40,
        postgres_container="postgres-id",
        api_container="api-id",
        worker_container="worker-id",
        api_image="ledgerbridge-app:abcdef0",
        api_image_id=f"sha256:{'a' * 64}",
        artifact_volume="ledgerbridge_artifacts",
        database={"alembic_version": "20260821_0002"},
    )


def _database_metadata() -> dict[str, object]:
    return {
        "database_name": "ledgerbridge",
        "database_owner": "ledgerbridge",
        "alembic_version": "20260821_0002",
        "data_checksums": "on",
        "role_grant_count": 10,
        "runtime_role_valid": True,
        "audit_select_only": True,
        "schema_create_denied": True,
        "function_count": 2,
        "trigger_count": 5,
        "row_counts": {
            "entity": 0,
            "account": 0,
            "journal_entry": 0,
            "posting": 0,
            "audit_event": 0,
        },
    }


def _r1_database_metadata(*, include_backup: bool = False) -> dict[str, object]:
    observed_roles = (*R1_ROLES, *R1_OPTIONAL_ROLES) if include_backup else R1_ROLES
    metadata = _database_metadata() | {
        "metadata_version": 2,
        "alembic_version": "20260824_0015",
        "database_owner": "ledgerbridge_owner",
        "database_temp_denied": True,
        "security_functions": [
            {"name": name, "proconfig": ["search_path=pg_catalog"]}
            for name in sorted(PHASE_1_FUNCTIONS | PHASE_2_FUNCTIONS | PHASE_3_FUNCTIONS)
        ],
        "public_triggers": [
            {"name": name, "enabled": "O"}
            for name in sorted(PHASE_1_TRIGGERS | PHASE_2_TRIGGERS | PHASE_3_TRIGGERS)
        ],
        "table_grants": [
            {"table": table, "privilege": privilege, "grantable": "NO"}
            for table, privilege in sorted(
                PHASE_1_TABLE_PRIVILEGES | PHASE_2_TABLE_PRIVILEGES | PHASE_3_TABLE_PRIVILEGES
            )
        ],
        "column_grants": [
            {"table": table, "column": column, "privilege": privilege, "grantable": "NO"}
            for table, column, privilege in sorted(
                PHASE_2_COLUMN_PRIVILEGES | PHASE_3_COLUMN_PRIVILEGES
            )
        ],
        "sequence_grants": [],
        "function_grants": [
            {
                "function": "append_audit_event",
                "grantee": "ledgerbridge_app",
                "privilege": "EXECUTE",
                "grantable": "NO",
            }
        ],
        "r1_role_matrix": [
            {
                "role": role,
                "login": True,
                "superuser": False,
                "create_database": False,
                "create_role": False,
                "inherit": False,
                "replication": False,
                "bypass_rls": False,
                "memberships": [],
            }
            for role in observed_roles
        ]
        + [
            {
                "role": "ledgerbridge_owner",
                "login": True,
                "superuser": False,
                "create_database": False,
                "create_role": False,
                "inherit": False,
                "replication": False,
                "bypass_rls": False,
                "memberships": [],
            }
        ],
        "r1_database_acl": [
            {"grantee": role, "privilege": "CONNECT", "grantable": "NO"}
            for role in (*observed_roles, "ledgerbridge_owner")
        ],
        "r1_schema_acl": [
            {"schema": "public", "grantee": "PUBLIC", "privilege": "USAGE", "grantable": "NO"},
            {
                "schema": "public",
                "grantee": "pg_database_owner",
                "privilege": "USAGE",
                "grantable": "NO",
            },
            {
                "schema": "public",
                "grantee": "pg_database_owner",
                "privilege": "CREATE",
                "grantable": "NO",
            },
            {
                "schema": "public",
                "grantee": "ledgerbridge_owner",
                "privilege": "USAGE",
                "grantable": "NO",
            },
            {
                "schema": "public",
                "grantee": "ledgerbridge_owner",
                "privilege": "CREATE",
                "grantable": "NO",
            },
            {
                "schema": "internal_read",
                "grantee": "ledgerbridge_reader",
                "privilege": "USAGE",
                "grantable": "NO",
            },
            {
                "schema": "internal_read",
                "grantee": "ledgerbridge_owner",
                "privilege": "USAGE",
                "grantable": "NO",
            },
            {
                "schema": "internal_read",
                "grantee": "ledgerbridge_owner",
                "privilege": "CREATE",
                "grantable": "NO",
            },
        ],
        "r1_default_acls": [],
        "r1_constraints": [
            {
                "schema": "public",
                "table": "candidate",
                "name": name,
                "type": "f",
                "deferrable": True,
                "initially_deferred": True,
                "validated": True,
            }
            for name in sorted(R1_REQUIRED_CONSTRAINTS)
        ],
        "r1_triggers": [
            {
                "schema": "public",
                "table": "candidate_event",
                "name": name,
                "enabled": "O",
                "constraint": True,
            }
            for name in sorted(R1_REQUIRED_TRIGGERS)
        ],
        "r1_views": [
            {
                "schema": "internal_read",
                "name": name,
                "security_barrier": True,
                "security_invoker": False,
                "owner": "ledgerbridge_owner",
            }
            for name in R1_INTERNAL_READ_VIEWS
        ],
        "r1_functions": [
            {
                "schema": "internal_read",
                "name": name,
                "identity_arguments": R1_INTERNAL_READ_FUNCTION_SIGNATURES[name],
                "owner": "ledgerbridge_owner",
                "security_definer": True,
                "proconfig": ["search_path=pg_catalog"],
            }
            for name in R1_INTERNAL_READ_FUNCTIONS
        ]
        + [
            {
                "schema": "public",
                "name": "r1_assert_posted_total_integrity",
                "identity_arguments": "",
                "owner": "ledgerbridge_owner",
                "security_definer": True,
                "proconfig": ["search_path=pg_catalog"],
            }
        ],
        "r1_effective_table_privileges": [],
        "r1_effective_function_privileges": [],
        "r1_effective_schema_privileges": [],
    }
    table_rows = cast(list[dict[str, object]], metadata["r1_effective_table_privileges"])
    for role in observed_roles:
        for table in R1_PUBLIC_TABLES:
            table_rows.append(
                {
                    "role": role,
                    "schema": "public",
                    "object": table,
                    "kind": "table",
                    "select": False,
                    "insert": False,
                    "update": False,
                    "delete": False,
                    "truncate": False,
                    "references": False,
                    "trigger": False,
                }
            )
        for view in R1_INTERNAL_READ_VIEWS:
            table_rows.append(
                {
                    "role": role,
                    "schema": "internal_read",
                    "object": view,
                    "kind": "view",
                    "select": False,
                    "insert": False,
                    "update": False,
                    "delete": False,
                    "truncate": False,
                    "references": False,
                    "trigger": False,
                }
            )
        table_rows.append(
            {
                "role": role,
                "schema": "internal_read",
                "object": "evidence_read_receipt",
                "kind": "table",
                "select": False,
                "insert": False,
                "update": False,
                "delete": False,
                "truncate": False,
                "references": False,
                "trigger": False,
            }
        )
    function_rows = cast(list[dict[str, object]], metadata["r1_effective_function_privileges"])
    functions = cast(list[dict[str, object]], metadata["r1_functions"])
    for role in observed_roles:
        for function in functions:
            function_rows.append(
                {
                    "role": role,
                    "schema": function["schema"],
                    "name": function["name"],
                    "identity_arguments": function["identity_arguments"],
                    "execute": (
                        function["schema"] == "internal_read"
                        and (
                            (
                                function["name"] == "append_internal_evidence_read_audit"
                                and role == "ledgerbridge_api"
                            )
                            or (
                                function["name"] != "append_internal_evidence_read_audit"
                                and role == "ledgerbridge_reader"
                            )
                        )
                    ),
                }
            )
    schema_rows = cast(list[dict[str, object]], metadata["r1_effective_schema_privileges"])
    for role in observed_roles:
        schema_rows.extend(
            [
                {"role": role, "schema": "public", "usage": True, "create": False},
                {
                    "role": role,
                    "schema": "internal_read",
                    "usage": role == "ledgerbridge_reader",
                    "create": False,
                },
            ]
        )
    return metadata


def _counterparty_database_metadata() -> dict[str, object]:
    metadata = _r1_database_metadata()
    metadata["alembic_version"] = "20260830_0020"
    schema_rows = cast(list[dict[str, object]], metadata["r1_effective_schema_privileges"])
    metadata["r1_effective_schema_privileges"] = [
        {**item, "usage": True}
        if item.get("role") == "ledgerbridge_api" and item.get("schema") == "internal_read"
        else item
        for item in schema_rows
    ]
    metadata["counterparty_row_counts"] = {table: 0 for table in COUNTERPARTY_PROTECTED_TABLES}
    metadata["counterparty_tables"] = [
        {"table": table, "owner": "ledgerbridge_owner", "kind": "r"}
        for table in COUNTERPARTY_PROTECTED_TABLES
    ]
    metadata["counterparty_functions"] = [
        {
            "schema": schema,
            "name": name,
            "identity_arguments": args,
            "result": COUNTERPARTY_FUNCTION_RESULTS[(schema, name)],
            "owner": "ledgerbridge_owner",
            "security_definer": schema == "internal_read",
            "proconfig": ["search_path=pg_catalog"],
        }
        for (schema, name), args in COUNTERPARTY_FUNCTION_SIGNATURES.items()
    ]
    metadata["counterparty_triggers"] = [
        {
            "table": table,
            "name": name,
            "enabled": "O",
            "constraint": constraint,
            "trigger_type": trigger_type,
            "function_schema": "public",
            "function_name": function_name,
        }
        for name, (table, constraint, trigger_type, function_name) in sorted(
            COUNTERPARTY_TRIGGER_CONTRACT.items()
        )
    ]
    metadata["counterparty_constraints"] = [
        {
            "table": table,
            "name": name,
            "type": constraint_type,
            "validated": True,
            "deferrable": False,
            "initially_deferred": False,
            "definition": definition,
        }
        for name, (table, constraint_type, definition) in COUNTERPARTY_CONSTRAINT_CONTRACT.items()
    ]
    metadata["counterparty_table_acls"] = [
        {
            "table": table,
            "grantee": "ledgerbridge_owner",
            "privilege": privilege,
            "grantable": False,
        }
        for table in COUNTERPARTY_PROTECTED_TABLES
        for privilege in (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
        )
    ]
    executors = {
        ("internal_read", "list_candidate_counterparty_facts"): "ledgerbridge_reader",
        ("internal_read", "list_candidate_evidence_satisfactions"): "ledgerbridge_reader",
    }
    metadata["counterparty_function_acls"] = [
        {
            "schema": schema,
            "name": name,
            "identity_arguments": args,
            "grantee": grantee,
            "privilege": "EXECUTE",
            "grantable": False,
        }
        for (schema, name), args in COUNTERPARTY_FUNCTION_SIGNATURES.items()
        for grantee in (
            ["ledgerbridge_owner", executors[(schema, name)]]
            if (schema, name) in executors
            else ["ledgerbridge_owner"]
        )
    ]
    metadata["counterparty_effective_table_privileges"] = [
        {
            "role": role,
            "table": table,
            "select": False,
            "insert": False,
            "update": False,
            "delete": False,
            "truncate": False,
            "references": False,
            "trigger": False,
        }
        for role in R1_ROLES
        for table in COUNTERPARTY_PROTECTED_TABLES
    ]
    metadata["counterparty_effective_function_privileges"] = [
        {
            "role": role,
            "schema": schema,
            "name": name,
            "identity_arguments": args,
            "execute": role == executors.get((schema, name)),
        }
        for role in R1_ROLES
        for (schema, name), args in COUNTERPARTY_FUNCTION_SIGNATURES.items()
    ]
    return metadata


def _bank_statement_database_metadata() -> dict[str, object]:
    metadata = _counterparty_database_metadata()
    metadata["alembic_version"] = "20260830_0021"
    function_signatures = dict(BANK_STATEMENT_FUNCTION_SIGNATURES)
    for function_key in CASH_RECONCILIATION_FUNCTION_KEYS:
        function_signatures.pop(function_key)
    for function_key in CASH_RECONCILIATION_V2_FUNCTION_KEYS:
        function_signatures.pop(function_key)
    trigger_contract = dict(BANK_STATEMENT_TRIGGER_CONTRACT)
    for trigger_name in CASH_RECONCILIATION_TRIGGER_NAMES:
        trigger_contract.pop(trigger_name)
    schema_rows = cast(list[dict[str, object]], metadata["r1_effective_schema_privileges"])
    metadata["r1_effective_schema_privileges"] = [
        {**item, "usage": True}
        if item.get("role") == "ledgerbridge_api" and item.get("schema") == "internal_read"
        else item
        for item in schema_rows
    ]
    metadata["bank_statement_row_counts"] = {table: 0 for table in BANK_STATEMENT_TABLES}
    metadata["bank_statement_tables"] = [
        {"table": table, "owner": "ledgerbridge_owner", "kind": "r"}
        for table in BANK_STATEMENT_TABLES
    ]
    metadata["bank_statement_schemas"] = [
        {"schema": schema, "owner": "ledgerbridge_owner"}
        for schema in ("internal_import", "internal_command", "internal_read")
    ]
    metadata["bank_statement_functions"] = [
        {
            "schema": schema,
            "name": name,
            "identity_arguments": args,
            "result": BANK_STATEMENT_FUNCTION_RESULTS[(schema, name)],
            "owner": "ledgerbridge_owner",
            "security_definer": (schema, name) in BANK_STATEMENT_SECURITY_DEFINER_FUNCTIONS,
            "proconfig": ["search_path=pg_catalog"],
        }
        for (schema, name), args in function_signatures.items()
    ]
    metadata["bank_statement_triggers"] = [
        {
            "table": table,
            "name": name,
            "enabled": "O",
            "constraint": constraint,
            "trigger_type": trigger_type,
            "deferrable": deferrable,
            "initially_deferred": initially_deferred,
            "function_schema": "public",
            "function_name": function_name,
        }
        for name, (
            table,
            constraint,
            trigger_type,
            deferrable,
            initially_deferred,
            function_name,
        ) in sorted(trigger_contract.items())
    ]
    metadata["bank_statement_constraints"] = [
        {
            "table": table,
            "name": name,
            "type": constraint_type,
            "validated": True,
            "deferrable": False,
            "initially_deferred": False,
            "definition": definition,
        }
        for name, (table, constraint_type, definition) in sorted(
            BANK_STATEMENT_CONSTRAINT_CONTRACT.items()
        )
    ]
    metadata["bank_statement_table_acls"] = [
        {
            "table": table,
            "grantee": "ledgerbridge_owner",
            "privilege": privilege,
            "grantable": False,
        }
        for table in BANK_STATEMENT_TABLES
        for privilege in (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
        )
    ]
    executors = {
        ("internal_import", "import_bank_statement"): "ledgerbridge_worker",
        ("internal_read", "get_bank_statement_summary"): "ledgerbridge_reader",
        ("internal_read", "list_bank_statement_transactions"): "ledgerbridge_reader",
    }
    metadata["bank_statement_function_acls"] = [
        {
            "schema": schema,
            "name": name,
            "identity_arguments": args,
            "grantee": grantee,
            "privilege": "EXECUTE",
            "grantable": False,
        }
        for (schema, name), args in function_signatures.items()
        for grantee in (
            ["ledgerbridge_owner", executors[(schema, name)]]
            if (schema, name) in executors
            else ["ledgerbridge_owner"]
        )
    ]
    schema_grantees = {
        "internal_import": ("ledgerbridge_owner", "ledgerbridge_worker"),
        "internal_command": ("ledgerbridge_owner", "ledgerbridge_api"),
        "internal_read": (
            "ledgerbridge_owner",
            "ledgerbridge_api",
            "ledgerbridge_reader",
        ),
    }
    metadata["bank_statement_schema_acls"] = [
        {
            "schema": schema,
            "grantee": grantee,
            "privilege": privilege,
            "grantable": False,
        }
        for schema, grantees in schema_grantees.items()
        for grantee in grantees
        for privilege in (("USAGE", "CREATE") if grantee == "ledgerbridge_owner" else ("USAGE",))
    ]
    roles = R1_ROLES
    metadata["bank_statement_effective_table_privileges"] = [
        {
            "role": role,
            "table": table,
            "select": False,
            "insert": False,
            "update": False,
            "delete": False,
            "truncate": False,
            "references": False,
            "trigger": False,
        }
        for role in roles
        for table in BANK_STATEMENT_TABLES
    ]
    metadata["bank_statement_effective_function_privileges"] = [
        {
            "role": role,
            "schema": schema,
            "name": name,
            "identity_arguments": args,
            "execute": role == executors.get((schema, name)),
        }
        for role in roles
        for (schema, name), args in function_signatures.items()
    ]
    schema_users = {
        "internal_import": {"ledgerbridge_worker"},
        "internal_command": {"ledgerbridge_api"},
        "internal_read": {"ledgerbridge_api", "ledgerbridge_reader"},
    }
    metadata["bank_statement_effective_schema_privileges"] = [
        {
            "role": role,
            "schema": schema,
            "usage": role in users,
            "create": False,
        }
        for role in roles
        for schema, users in schema_users.items()
    ]
    return metadata


def _account_registry_database_metadata() -> dict[str, object]:
    metadata = _bank_statement_database_metadata()
    metadata["alembic_version"] = "20260830_0023"
    bank_triggers = dict(BANK_STATEMENT_TRIGGER_CONTRACT)
    for trigger_name in CASH_RECONCILIATION_TRIGGER_NAMES:
        bank_triggers.pop(trigger_name)
    bank_triggers.pop("validate_managed_account_audit")
    bank_triggers.pop("require_statement_backed_account")
    bank_triggers.update(ACCOUNT_REGISTRY_MANAGED_ACCOUNT_TRIGGER_CONTRACT)
    metadata["bank_statement_triggers"] = [
        {
            "table": table,
            "name": name,
            "enabled": "O",
            "constraint": constraint,
            "trigger_type": trigger_type,
            "deferrable": deferrable,
            "initially_deferred": initially_deferred,
            "function_schema": "public",
            "function_name": function_name,
        }
        for name, (
            table,
            constraint,
            trigger_type,
            deferrable,
            initially_deferred,
            function_name,
        ) in sorted(bank_triggers.items())
    ]
    bank_constraints = dict(BANK_STATEMENT_CONSTRAINT_CONTRACT)
    bank_constraints.pop("managed_account_institution_code_check")
    bank_constraints.update(ACCOUNT_REGISTRY_MANAGED_ACCOUNT_CONSTRAINT_CONTRACT)
    metadata["bank_statement_constraints"] = [
        {
            "table": table,
            "name": name,
            "type": constraint_type,
            "validated": True,
            "deferrable": False,
            "initially_deferred": False,
            "definition": definition,
        }
        for name, (table, constraint_type, definition) in sorted(bank_constraints.items())
    ]
    metadata["account_registry_row_counts"] = {table: 0 for table in ACCOUNT_REGISTRY_TABLES}
    metadata["account_registry_tables"] = [
        {"table": table, "owner": "ledgerbridge_owner", "kind": "r"}
        for table in ACCOUNT_REGISTRY_TABLES
    ]
    metadata["account_registry_functions"] = [
        {
            "schema": schema,
            "name": name,
            "identity_arguments": arguments,
            "result": ACCOUNT_REGISTRY_FUNCTION_RESULTS[(schema, name)],
            "owner": "ledgerbridge_owner",
            "security_definer": (schema, name) in ACCOUNT_REGISTRY_SECURITY_DEFINER_FUNCTIONS,
            "proconfig": ["search_path=pg_catalog"],
        }
        for (schema, name), arguments in ACCOUNT_REGISTRY_FUNCTION_SIGNATURES.items()
    ]
    metadata["account_registry_triggers"] = [
        {
            "table": table,
            "name": name,
            "enabled": "O",
            "constraint": constraint,
            "deferrable": deferrable,
            "initially_deferred": initially_deferred,
            "function_name": function_name,
        }
        for name, (
            table,
            constraint,
            deferrable,
            initially_deferred,
            function_name,
        ) in ACCOUNT_REGISTRY_TRIGGER_CONTRACT.items()
    ]
    metadata["account_registry_constraints"] = [
        {
            "table": table,
            "name": f"{table}_pkey",
            "type": "p",
            "validated": True,
            "deferrable": False,
            "initially_deferred": False,
        }
        for table in ACCOUNT_REGISTRY_TABLES
    ] + [
        {
            "table": table,
            "name": name,
            "type": "t",
            "validated": True,
            "deferrable": deferrable,
            "initially_deferred": initially_deferred,
        }
        for name, (
            table,
            constraint,
            deferrable,
            initially_deferred,
            _,
        ) in ACCOUNT_REGISTRY_TRIGGER_CONTRACT.items()
        if constraint
    ]
    metadata["account_registry_table_acls"] = [
        {
            "table": table,
            "grantee": "ledgerbridge_owner",
            "privilege": privilege,
            "grantable": False,
        }
        for table in ACCOUNT_REGISTRY_TABLES
        for privilege in (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
        )
    ]
    metadata["account_registry_function_acls"] = [
        {
            "schema": schema,
            "name": name,
            "identity_arguments": arguments,
            "grantee": grantee,
            "privilege": "EXECUTE",
            "grantable": False,
        }
        for (schema, name), arguments in ACCOUNT_REGISTRY_FUNCTION_SIGNATURES.items()
        for grantee in (
            ("ledgerbridge_owner", ACCOUNT_REGISTRY_FUNCTION_EXECUTORS[(schema, name)])
            if (schema, name) in ACCOUNT_REGISTRY_FUNCTION_EXECUTORS
            else ("ledgerbridge_owner",)
        )
    ]
    metadata["account_registry_effective_table_privileges"] = [
        {
            "role": role,
            "table": table,
            "select": False,
            "insert": False,
            "update": False,
            "delete": False,
        }
        for role in R1_ROLES
        for table in ACCOUNT_REGISTRY_TABLES
    ]
    metadata["account_registry_effective_function_privileges"] = [
        {
            "role": role,
            "schema": schema,
            "name": name,
            "identity_arguments": arguments,
            "execute": role == ACCOUNT_REGISTRY_FUNCTION_EXECUTORS.get((schema, name)),
        }
        for role in R1_ROLES
        for (schema, name), arguments in ACCOUNT_REGISTRY_FUNCTION_SIGNATURES.items()
    ]
    return metadata


def _company_reporting_database_metadata(*, composition: bool = False) -> dict[str, object]:
    metadata = _account_registry_database_metadata()
    metadata["alembic_version"] = "20260901_0028" if composition else "20260830_0024"
    function_signatures = dict(COMPANY_REPORTING_FUNCTION_SIGNATURES)
    reader_functions = set(COMPANY_REPORTING_READER_FUNCTIONS)
    required_tables: tuple[str, ...] = COMPANY_REPORTING_REQUIRED_TABLES
    if not composition:
        function_signatures.pop("get_company_report_composition_v1_as_of")
        reader_functions.discard("get_company_report_composition_v1_as_of")
        required_tables = COMPANY_REPORTING_BASE_REQUIRED_TABLES
    metadata["company_reporting_schema"] = {
        "schema": COMPANY_REPORTING_SCHEMA,
        "owner": "ledgerbridge_owner",
    }
    metadata["company_reporting_functions"] = [
        {
            "schema": COMPANY_REPORTING_SCHEMA,
            "name": name,
            "identity_arguments": arguments,
            "result": COMPANY_REPORTING_FUNCTION_RESULTS[name],
            "owner": "ledgerbridge_owner",
            "security_definer": name in COMPANY_REPORTING_SECURITY_DEFINER_FUNCTIONS,
            "proconfig": ["search_path=pg_catalog"],
        }
        for name, arguments in function_signatures.items()
    ]
    metadata["company_reporting_required_tables"] = [
        {"schema": "public", "table": table, "owner": "ledgerbridge_owner", "kind": "r"}
        for table in required_tables
    ]
    metadata["company_reporting_required_columns"] = [
        {
            "schema": "public",
            "table": table,
            "column": column,
            "type": data_type,
        }
        for (table, column), data_type in COMPANY_REPORTING_REQUIRED_COLUMNS.items()
    ]
    metadata["company_reporting_required_functions"] = [
        {
            "schema": "public",
            "name": "r1_assert_posted_total_integrity",
            "identity_arguments": "",
            "result": "boolean",
            "owner": "ledgerbridge_owner",
            "security_definer": True,
            "proconfig": ["search_path=pg_catalog"],
        }
    ]
    metadata["company_reporting_triggers"] = [
        {
            "table": table,
            "name": name,
            "enabled": "O",
            "constraint": is_constraint,
            "trigger_type": trigger_type,
            "deferrable": deferrable,
            "initially_deferred": initially_deferred,
            "function_schema": "public",
            "function_name": function_name,
        }
        for name, (
            table,
            is_constraint,
            trigger_type,
            deferrable,
            initially_deferred,
            function_name,
        ) in COMPANY_REPORTING_TRIGGER_CONTRACT.items()
    ]
    metadata["company_reporting_schema_acls"] = [
        {
            "schema": COMPANY_REPORTING_SCHEMA,
            "grantee": grantee,
            "privilege": privilege,
            "grantable": False,
        }
        for grantee, privileges in {
            "ledgerbridge_owner": ("USAGE", "CREATE"),
            "ledgerbridge_reader": ("USAGE",),
        }.items()
        for privilege in privileges
    ]
    metadata["company_reporting_function_acls"] = [
        {
            "schema": COMPANY_REPORTING_SCHEMA,
            "name": name,
            "identity_arguments": arguments,
            "grantee": grantee,
            "privilege": "EXECUTE",
            "grantable": False,
        }
        for name, arguments in function_signatures.items()
        for grantee in (
            ("ledgerbridge_owner", "ledgerbridge_reader")
            if name in reader_functions
            else ("ledgerbridge_owner",)
        )
    ]
    metadata["company_reporting_effective_schema_privileges"] = [
        {
            "role": role,
            "schema": COMPANY_REPORTING_SCHEMA,
            "usage": role == "ledgerbridge_reader",
            "create": False,
        }
        for role in R1_ROLES
    ]
    metadata["company_reporting_effective_function_privileges"] = [
        {
            "role": role,
            "schema": COMPANY_REPORTING_SCHEMA,
            "name": name,
            "identity_arguments": arguments,
            "execute": role == "ledgerbridge_reader" and name in reader_functions,
        }
        for role in R1_ROLES
        for name, arguments in function_signatures.items()
    ]
    return metadata


_CLASSIFICATION_BATCH_PRETTY_FK_FIXTURES = {
    "candidate_classification_batch_receipt_entity_fk": (
        "public",
        "entity",
        "FOREIGN KEY (authorized_entity_id) REFERENCES entity(id) ON DELETE RESTRICT",
    ),
    "candidate_classification_batch_receipt_target_unit_fk": (
        "public",
        "business_unit",
        "FOREIGN KEY (target_business_unit_id) REFERENCES business_unit(id) ON DELETE RESTRICT",
    ),
    "candidate_classification_batch_receipt_source_fk": (
        "public",
        "candidate",
        "FOREIGN KEY (source_candidate_id) REFERENCES candidate(id) ON DELETE RESTRICT",
    ),
    "candidate_classification_batch_receipt_audit_fk": (
        "public",
        "audit_event",
        "FOREIGN KEY (audit_event_id) REFERENCES audit_event(id) ON DELETE RESTRICT",
    ),
    "candidate_classification_batch_member_batch_fk": (
        "internal_command",
        "candidate_classification_batch_receipt",
        "FOREIGN KEY (batch_operation_id) REFERENCES "
        "internal_command.candidate_classification_batch_receipt(operation_id) "
        "ON DELETE RESTRICT",
    ),
    "candidate_classification_batch_member_candidate_fk": (
        "public",
        "candidate",
        "FOREIGN KEY (candidate_id) REFERENCES candidate(id) ON DELETE RESTRICT",
    ),
    "candidate_classification_batch_member_operation_fk": (
        "internal_command",
        "candidate_decision_receipt",
        "FOREIGN KEY (member_operation_id) REFERENCES "
        "internal_command.candidate_decision_receipt(operation_id) ON DELETE RESTRICT",
    ),
    "candidate_classification_batch_assertion_operation_fk": (
        "internal_command",
        "candidate_classification_batch_receipt",
        "FOREIGN KEY (operation_id) REFERENCES "
        "internal_command.candidate_classification_batch_receipt(operation_id) "
        "ON DELETE RESTRICT",
    ),
}


def _classification_batch_security_metadata() -> dict[str, object]:
    owner = "ledgerbridge_owner"
    return {
        "database_owner": owner,
        "r1_role_matrix": [{"role": role} for role in R1_ROLES],
        "classification_batch_row_counts": {table: 0 for table in CLASSIFICATION_BATCH_TABLES},
        "classification_batch_tables": [
            {"table": table, "owner": owner, "kind": "r"} for table in CLASSIFICATION_BATCH_TABLES
        ],
        "classification_batch_functions": [
            {
                "name": name,
                "identity_arguments": arguments,
                "result": CLASSIFICATION_BATCH_FUNCTION_RESULTS[name],
                "owner": owner,
                "security_definer": True,
                "proconfig": ["search_path=pg_catalog"],
            }
            for name, arguments in CLASSIFICATION_BATCH_FUNCTION_SIGNATURES.items()
        ],
        "classification_batch_triggers": [
            {
                "table": table,
                "name": name,
                "enabled": "O",
                "trigger_type": trigger_type,
                "function_schema": "internal_command",
                "function_name": function_name,
            }
            for name, (table, trigger_type, function_name) in (
                CLASSIFICATION_BATCH_TRIGGER_CONTRACT.items()
            )
        ],
        "classification_batch_constraints": [
            {
                "table": CLASSIFICATION_BATCH_CONSTRAINT_TABLES[name],
                "name": name,
                "type": "f" if name in _CLASSIFICATION_BATCH_PRETTY_FK_FIXTURES else "c",
                "validated": True,
                "definition": (
                    _CLASSIFICATION_BATCH_PRETTY_FK_FIXTURES[name][2]
                    if name in _CLASSIFICATION_BATCH_PRETTY_FK_FIXTURES
                    else " ".join(CLASSIFICATION_BATCH_CONSTRAINT_DEFINITION_MARKERS[name])
                ),
                "reference_schema": (
                    _CLASSIFICATION_BATCH_PRETTY_FK_FIXTURES[name][0]
                    if name in _CLASSIFICATION_BATCH_PRETTY_FK_FIXTURES
                    else None
                ),
                "reference_table": (
                    _CLASSIFICATION_BATCH_PRETTY_FK_FIXTURES[name][1]
                    if name in _CLASSIFICATION_BATCH_PRETTY_FK_FIXTURES
                    else None
                ),
            }
            for name in CLASSIFICATION_BATCH_REQUIRED_CONSTRAINTS
        ],
        "classification_batch_columns": [
            {
                "table": table,
                "column": column,
                "data_type": data_type,
                "not_null": not_null,
            }
            for table, columns in CLASSIFICATION_BATCH_REQUIRED_COLUMNS.items()
            for column, (data_type, not_null) in columns.items()
        ],
        "classification_batch_table_acls": [
            {
                "table": table,
                "grantee": owner,
                "privilege": privilege,
                "grantable": False,
            }
            for table in CLASSIFICATION_BATCH_TABLES
            for privilege in (
                "SELECT",
                "INSERT",
                "UPDATE",
                "DELETE",
                "TRUNCATE",
                "REFERENCES",
                "TRIGGER",
            )
        ],
        "classification_batch_function_acls": [
            {
                "name": name,
                "identity_arguments": arguments,
                "grantee": grantee,
                "privilege": "EXECUTE",
                "grantable": False,
            }
            for name, arguments in CLASSIFICATION_BATCH_FUNCTION_SIGNATURES.items()
            for grantee in (
                (owner, CLASSIFICATION_BATCH_FUNCTION_EXECUTORS[name])
                if name in CLASSIFICATION_BATCH_FUNCTION_EXECUTORS
                else (owner,)
            )
        ],
        "classification_batch_effective_table_privileges": [
            {
                "role": role,
                "table": table,
                "select": False,
                "insert": False,
                "update": False,
                "delete": False,
            }
            for role in R1_ROLES
            for table in CLASSIFICATION_BATCH_TABLES
        ],
        "classification_batch_effective_function_privileges": [
            {
                "role": role,
                "name": name,
                "identity_arguments": arguments,
                "execute": role == CLASSIFICATION_BATCH_FUNCTION_EXECUTORS.get(name),
            }
            for role in R1_ROLES
            for name, arguments in CLASSIFICATION_BATCH_FUNCTION_SIGNATURES.items()
        ],
    }


def _classification_batch_roundtrip_metadata() -> dict[str, object]:
    metadata = _company_reporting_database_metadata()
    classification = _classification_batch_security_metadata()
    metadata.update(
        {
            key: value
            for key, value in classification.items()
            if key not in {"database_owner", "r1_role_matrix"}
        }
    )
    return metadata


def test_classification_batch_restore_metadata_covers_0026_security_boundary() -> None:
    metadata = _classification_batch_security_metadata()

    _validate_classification_batch_security(metadata)

    for table in CLASSIFICATION_BATCH_TABLES:
        assert f"internal_command.{table}" in CLASSIFICATION_BATCH_SECURITY_SQL
    for name, arguments in CLASSIFICATION_BATCH_FUNCTION_SIGNATURES.items():
        assert f"('{name}', '{arguments}')" in CLASSIFICATION_BATCH_SECURITY_SQL
    assert "con.confrelid" in CLASSIFICATION_BATCH_SECURITY_SQL
    assert "'reference_schema',reference_schema" in CLASSIFICATION_BATCH_SECURITY_SQL
    assert "'reference_table',reference_table" in CLASSIFICATION_BATCH_SECURITY_SQL


def test_classification_batch_restore_accepts_implicit_default_owner_table_acls() -> None:
    expected = _classification_batch_roundtrip_metadata()
    actual = {**expected, "classification_batch_table_acls": []}

    _validate_restored_database(expected, actual)


def test_classification_batch_restore_rejects_partial_owner_table_acl() -> None:
    expected = _classification_batch_roundtrip_metadata()
    owner_acls = cast(list[dict[str, object]], expected["classification_batch_table_acls"])
    actual = {
        **expected,
        "classification_batch_table_acls": [
            item
            for item in owner_acls
            if not (
                item.get("table") == "candidate_classification_batch_receipt"
                and item.get("privilege") == "SELECT"
            )
        ],
    }

    with pytest.raises(BackupError, match="classification_batch_table_acls"):
        _validate_restored_database(expected, actual)


def test_classification_batch_restore_rejects_effective_table_privilege_drift() -> None:
    expected = _classification_batch_roundtrip_metadata()
    privileges = cast(
        list[dict[str, object]], expected["classification_batch_effective_table_privileges"]
    )
    actual = {
        **expected,
        "classification_batch_table_acls": [],
        "classification_batch_effective_table_privileges": [
            {**item, "select": True}
            if item.get("role") == "ledgerbridge_api"
            and item.get("table") == "candidate_classification_batch_receipt"
            else item
            for item in privileges
        ],
    }

    with pytest.raises(BackupError, match="classification_batch_effective_table_privileges"):
        _validate_restored_database(expected, actual)


def test_classification_batch_security_query_treats_null_acls_as_no_rows() -> None:
    assert "aclexplode(t.acl)" in CLASSIFICATION_BATCH_SECURITY_SQL
    assert "aclexplode(f.acl)" in CLASSIFICATION_BATCH_SECURITY_SQL
    assert "COALESCE(t.acl,'{}'::aclitem[])" not in CLASSIFICATION_BATCH_SECURITY_SQL
    assert "COALESCE(f.acl,'{}'::aclitem[])" not in CLASSIFICATION_BATCH_SECURITY_SQL


@pytest.mark.parametrize(
    "mutation",
    [
        "table_missing",
        "function_security",
        "trigger_disabled",
        "constraint_missing",
        "constraint_semantics",
        "constraint_reference",
        "constraint_type",
        "non_fk_reference",
        "column_contract",
        "api_execute_missing",
        "direct_table_grant",
    ],
)
def test_classification_batch_restore_rejects_security_metadata_drift(
    mutation: str,
) -> None:
    metadata = _classification_batch_security_metadata()
    if mutation == "table_missing":
        metadata["classification_batch_tables"] = cast(
            list[dict[str, object]], metadata["classification_batch_tables"]
        )[1:]
    elif mutation == "function_security":
        rows = cast(list[dict[str, object]], metadata["classification_batch_functions"])
        metadata["classification_batch_functions"] = [
            {**rows[0], "security_definer": False},
            *rows[1:],
        ]
    elif mutation == "trigger_disabled":
        rows = cast(list[dict[str, object]], metadata["classification_batch_triggers"])
        metadata["classification_batch_triggers"] = [{**rows[0], "enabled": "D"}, *rows[1:]]
    elif mutation == "constraint_missing":
        metadata["classification_batch_constraints"] = cast(
            list[dict[str, object]], metadata["classification_batch_constraints"]
        )[1:]
    elif mutation == "constraint_semantics":
        rows = cast(list[dict[str, object]], metadata["classification_batch_constraints"])
        metadata["classification_batch_constraints"] = [
            {**rows[0], "definition": "CHECK (true)"},
            *rows[1:],
        ]
    elif mutation == "constraint_reference":
        rows = cast(list[dict[str, object]], metadata["classification_batch_constraints"])
        metadata["classification_batch_constraints"] = [
            {**item, "reference_table": "audit_event"}
            if item["name"] == "candidate_classification_batch_receipt_entity_fk"
            else item
            for item in rows
        ]
    elif mutation == "constraint_type":
        rows = cast(list[dict[str, object]], metadata["classification_batch_constraints"])
        metadata["classification_batch_constraints"] = [
            {**item, "type": "c"}
            if item["name"] == "candidate_classification_batch_receipt_entity_fk"
            else item
            for item in rows
        ]
    elif mutation == "non_fk_reference":
        rows = cast(list[dict[str, object]], metadata["classification_batch_constraints"])
        metadata["classification_batch_constraints"] = [
            {**item, "reference_schema": "public", "reference_table": "entity"}
            if item["name"] == "candidate_classification_batch_receipt_pkey"
            else item
            for item in rows
        ]
    elif mutation == "column_contract":
        rows = cast(list[dict[str, object]], metadata["classification_batch_columns"])
        metadata["classification_batch_columns"] = [
            {**rows[0], "data_type": "text"},
            *rows[1:],
        ]
    elif mutation == "api_execute_missing":
        rows = cast(
            list[dict[str, object]],
            metadata["classification_batch_effective_function_privileges"],
        )
        metadata["classification_batch_effective_function_privileges"] = [
            {**row, "execute": False}
            if row["role"] == "ledgerbridge_api"
            and row["name"] == "apply_candidate_classification_batch"
            else row
            for row in rows
        ]
    else:
        rows = cast(
            list[dict[str, object]],
            metadata["classification_batch_effective_table_privileges"],
        )
        metadata["classification_batch_effective_table_privileges"] = [
            {**rows[0], "select": True},
            *rows[1:],
        ]

    with pytest.raises(BackupError, match="classification batch"):
        _validate_classification_batch_security(metadata)


def test_counterparty_restore_metadata_covers_0020_contract() -> None:
    expected = _counterparty_database_metadata()
    _validate_restored_database(expected, expected.copy())
    assert set(cast(dict[str, int], expected["counterparty_row_counts"])) == set(
        COUNTERPARTY_PROTECTED_TABLES
    )
    for table in COUNTERPARTY_PROTECTED_TABLES:
        assert f"FROM public.{table}" in COUNTERPARTY_SECURITY_SQL
    for (schema, name), args in COUNTERPARTY_FUNCTION_SIGNATURES.items():
        assert f"('{schema}', '{name}', '{args}')" in COUNTERPARTY_SECURITY_SQL


@pytest.mark.parametrize(
    "mutation",
    [
        "row_counts",
        "table_missing",
        "table_owner",
        "table_kind",
        "function_missing",
        "function_signature",
        "function_result",
        "function_owner",
        "function_security_definer",
        "function_search_path",
        "function_extra_config",
        "trigger_missing",
        "trigger_table",
        "trigger_disabled",
        "trigger_type",
        "trigger_function",
        "trigger_function_schema",
        "constraint_missing",
        "constraint_values",
        "constraint_validated",
        "constraint_deferrable",
        "effective_table_acl",
        "effective_table_truncate",
        "effective_function_acl",
        "raw_table_acl",
        "raw_function_acl",
    ],
)
def test_counterparty_restore_metadata_rejects_drift(mutation: str) -> None:
    expected = _counterparty_database_metadata()
    actual = {**expected}
    if mutation == "row_counts":
        actual["counterparty_row_counts"] = {"candidate_evidence_link": 0}
    elif mutation.startswith("table_"):
        rows = cast(list[dict[str, object]], expected["counterparty_tables"])
        if mutation == "table_missing":
            actual["counterparty_tables"] = rows[1:]
        else:
            table_field, table_value = {
                "table_owner": ("owner", "stale_owner"),
                "table_kind": ("kind", "v"),
            }[mutation]
            actual["counterparty_tables"] = [
                {**rows[0], table_field: table_value},
                *rows[1:],
            ]
    elif mutation.startswith("function_"):
        rows = cast(list[dict[str, object]], expected["counterparty_functions"])
        if mutation == "function_missing":
            actual["counterparty_functions"] = rows[1:]
        else:
            function_field, function_value = {
                "function_signature": ("identity_arguments", "p_entity_id text"),
                "function_result": ("result", "void"),
                "function_owner": ("owner", "stale_owner"),
                "function_security_definer": (
                    "security_definer",
                    not cast(bool, rows[0]["security_definer"]),
                ),
                "function_search_path": ("proconfig", ["search_path=pg_temp"]),
                "function_extra_config": (
                    "proconfig",
                    ["search_path=pg_catalog", "statement_timeout=0"],
                ),
            }[mutation]
            actual["counterparty_functions"] = [
                {**rows[0], function_field: function_value},
                *rows[1:],
            ]
    elif mutation.startswith("trigger_"):
        rows = cast(list[dict[str, object]], expected["counterparty_triggers"])
        if mutation == "trigger_missing":
            actual["counterparty_triggers"] = rows[1:]
        else:
            trigger_field, trigger_value = {
                "trigger_table": ("table", "candidate"),
                "trigger_disabled": ("enabled", "D"),
                "trigger_type": ("trigger_type", 7),
                "trigger_function": ("function_name", "r1_validate_candidate_counterparty"),
                "trigger_function_schema": ("function_schema", "pg_catalog"),
            }[mutation]
            actual["counterparty_triggers"] = [
                {**rows[0], trigger_field: trigger_value},
                *rows[1:],
            ]
    elif mutation.startswith("constraint_"):
        rows = cast(list[dict[str, object]], expected["counterparty_constraints"])
        if mutation == "constraint_missing":
            actual["counterparty_constraints"] = rows[1:]
        elif mutation == "constraint_values":
            actual["counterparty_constraints"] = [
                {**rows[0], "definition": "CHECK (risk_code IN ('UNSUPPORTED'))"},
                *rows[1:],
            ]
        elif mutation == "constraint_validated":
            actual["counterparty_constraints"] = [
                {**rows[0], "validated": False},
                *rows[1:],
            ]
        else:
            actual["counterparty_constraints"] = [
                {**rows[0], "deferrable": True},
                *rows[1:],
            ]
    elif mutation == "effective_table_acl":
        rows = cast(list[dict[str, object]], expected["counterparty_effective_table_privileges"])
        actual["counterparty_effective_table_privileges"] = [
            {**rows[0], "select": True},
            *rows[1:],
        ]
    elif mutation == "effective_table_truncate":
        rows = cast(list[dict[str, object]], expected["counterparty_effective_table_privileges"])
        actual["counterparty_effective_table_privileges"] = [
            {**rows[0], "truncate": True},
            *rows[1:],
        ]
    elif mutation == "effective_function_acl":
        rows = cast(list[dict[str, object]], expected["counterparty_effective_function_privileges"])
        target = next(i for i, row in enumerate(rows) if row["execute"] is True)
        actual["counterparty_effective_function_privileges"] = [
            {**row, "execute": False} if i == target else row for i, row in enumerate(rows)
        ]
    else:
        field = {
            "raw_table_acl": "counterparty_table_acls",
            "raw_function_acl": "counterparty_function_acls",
        }[mutation]
        rows = cast(list[dict[str, object]], expected[field])
        actual[field] = [*rows, {**rows[0], "grantee": "stale_role"}]
    if mutation == "row_counts":
        with pytest.raises(BackupError, match="metadata differs"):
            _validate_restored_database(expected, actual)
    else:
        with pytest.raises(BackupError, match="counterparty"):
            _validate_restored_database(actual, actual.copy())


def test_bank_statement_restore_metadata_covers_0021_contract() -> None:
    expected = _bank_statement_database_metadata()
    _validate_restored_database(expected, expected.copy())
    assert set(cast(dict[str, int], expected["bank_statement_row_counts"])) == set(
        BANK_STATEMENT_TABLES
    )
    for table in BANK_STATEMENT_TABLES:
        assert f"FROM public.{table}" in BANK_STATEMENT_SECURITY_SQL
    for (schema, name), args in BANK_STATEMENT_FUNCTION_SIGNATURES.items():
        assert f"('{schema}', '{name}', '{args}')" in BANK_STATEMENT_SECURITY_SQL
    assert "observed_constraints AS" in BANK_STATEMENT_SECURITY_SQL
    assert "'bank_statement_constraints'" in BANK_STATEMENT_SECURITY_SQL
    assert len(BANK_STATEMENT_CONSTRAINT_CONTRACT) == 76


def test_restore_accepts_pg_dump_equivalent_text_array_check_definition() -> None:
    expected = _bank_statement_database_metadata()
    bank_constraints = cast(list[dict[str, object]], expected["bank_statement_constraints"])
    counterparty_constraints = cast(list[dict[str, object]], expected["counterparty_constraints"])
    actual = {
        **expected,
        "bank_statement_constraints": [
            {
                **item,
                "definition": (
                    "CHECK (((status)::text = ANY (ARRAY[('PENDING'::character varying)::text, "
                    "('CONFIRMED'::character varying)::text, "
                    "('REJECTED'::character varying)::text])))"
                ),
            }
            if item.get("name") == "bank_statement_review_status_check"
            else item
            for item in bank_constraints
        ],
        "counterparty_constraints": [
            {
                **item,
                "definition": (
                    "CHECK (((relation)::text = ANY "
                    "(ARRAY[('SAME_ECONOMIC_TRANSACTION'::character varying)::text, "
                    "('PARTIAL_REFUND'::character varying)::text])))"
                ),
            }
            if item.get("name") == "ck_candidate_evidence_link_candidate_evidence_link_rela_edfc"
            else item
            for item in counterparty_constraints
        ],
    }

    _validate_restored_database(expected, actual)


def test_restore_rejects_non_equivalent_text_array_check_definition() -> None:
    expected = _bank_statement_database_metadata()
    constraints = cast(list[dict[str, object]], expected["bank_statement_constraints"])
    actual = {
        **expected,
        "bank_statement_constraints": [
            {
                **item,
                "definition": (
                    "CHECK (((status)::text = ANY (ARRAY[('PENDING'::character varying)::text, "
                    "('CONFIRMED'::character varying)::text, "
                    "('REVERSED'::character varying)::text])))"
                ),
            }
            if item.get("name") == "bank_statement_review_status_check"
            else item
            for item in constraints
        ],
    }

    with pytest.raises(BackupError, match="metadata differs"):
        _validate_restored_database(expected, actual)


def test_restore_accepts_omitted_redundant_table_owner_acls() -> None:
    expected = _bank_statement_database_metadata()
    actual = {
        **expected,
        "bank_statement_table_acls": [],
        "counterparty_table_acls": [],
    }

    _validate_restored_database(expected, actual)


def test_restore_rejects_partial_owner_table_acl_during_roundtrip_comparison() -> None:
    expected = _bank_statement_database_metadata()
    owner_acls = cast(list[dict[str, object]], expected["bank_statement_table_acls"])
    actual = {
        **expected,
        "bank_statement_table_acls": [
            item
            for item in owner_acls
            if not (item.get("table") == "bank_statement" and item.get("privilege") == "SELECT")
        ],
    }

    with pytest.raises(BackupError, match="metadata differs"):
        _validate_restored_database(expected, actual)


def test_restore_rejects_changed_owner_table_acl_grantability() -> None:
    expected = _bank_statement_database_metadata()
    owner_acls = cast(list[dict[str, object]], expected["bank_statement_table_acls"])
    actual = {
        **expected,
        "bank_statement_table_acls": [
            {**item, "grantable": True}
            if item.get("table") == "bank_statement" and item.get("privilege") == "SELECT"
            else item
            for item in owner_acls
        ],
    }

    with pytest.raises(BackupError, match="metadata differs"):
        _validate_restored_database(expected, actual)


def test_restore_rejects_non_owner_table_acl_during_roundtrip_comparison() -> None:
    expected = _bank_statement_database_metadata()
    actual = {
        **expected,
        "bank_statement_table_acls": [
            {
                "table": "bank_statement",
                "grantee": "stale_role",
                "privilege": "SELECT",
                "grantable": False,
            }
        ],
    }

    with pytest.raises(BackupError, match="metadata differs"):
        _validate_restored_database(expected, actual)


def test_r1_restore_allows_0021_metadata_without_accounting_dimensions_function() -> None:
    metadata = _bank_statement_database_metadata()
    metadata["r1_functions"] = [
        item
        for item in cast(list[dict[str, object]], metadata["r1_functions"])
        if item.get("name") != "get_accounting_dimensions"
    ]
    metadata["r1_effective_function_privileges"] = [
        item
        for item in cast(list[dict[str, object]], metadata["r1_effective_function_privileges"])
        if item.get("name") != "get_accounting_dimensions"
    ]

    _validate_restored_database(metadata, metadata.copy())


def test_r1_restore_requires_accounting_dimensions_function_by_0024() -> None:
    metadata = _company_reporting_database_metadata()
    metadata["r1_functions"] = [
        item
        for item in cast(list[dict[str, object]], metadata["r1_functions"])
        if item.get("name") != "get_accounting_dimensions"
    ]
    metadata["r1_effective_function_privileges"] = [
        item
        for item in cast(list[dict[str, object]], metadata["r1_effective_function_privileges"])
        if item.get("name") != "get_accounting_dimensions"
    ]

    with pytest.raises(BackupError, match="internal_read functions"):
        _validate_restored_database(metadata, metadata.copy())


def test_r1_restore_requires_accounting_dimensions_function_from_0022() -> None:
    metadata = _bank_statement_database_metadata()
    metadata["alembic_version"] = "20260830_0022"
    metadata["r1_functions"] = [
        item
        for item in cast(list[dict[str, object]], metadata["r1_functions"])
        if item.get("name") != "get_accounting_dimensions"
    ]
    metadata["r1_effective_function_privileges"] = [
        item
        for item in cast(list[dict[str, object]], metadata["r1_effective_function_privileges"])
        if item.get("name") != "get_accounting_dimensions"
    ]

    with pytest.raises(BackupError, match="internal_read functions"):
        _validate_restored_database(metadata, metadata.copy())


def test_company_reporting_restore_metadata_covers_0028_contract() -> None:
    expected = _company_reporting_database_metadata(composition=True)

    _validate_company_reporting_security(expected)

    for table in COMPANY_REPORTING_REQUIRED_TABLES:
        assert f"'{table}'" in COMPANY_REPORTING_SECURITY_SQL
    for name, arguments in COMPANY_REPORTING_FUNCTION_SIGNATURES.items():
        assert f"('{name}', '{arguments}')" in COMPANY_REPORTING_SECURITY_SQL
    assert "company_reporting_effective_function_privileges" in COMPANY_REPORTING_SECURITY_SQL


@pytest.mark.parametrize("mutation", ["unknown", "wrong_deferrability"])
def test_account_registry_restore_rejects_constraint_trigger_drift(mutation: str) -> None:
    metadata = _company_reporting_database_metadata()
    constraints = cast(list[dict[str, object]], metadata["account_registry_constraints"])
    constraint_trigger = next(item for item in constraints if item.get("type") == "t")
    if mutation == "unknown":
        drifted = {**constraint_trigger, "name": "unreviewed_constraint_trigger"}
    else:
        drifted = {
            **constraint_trigger,
            "deferrable": not cast(bool, constraint_trigger["deferrable"]),
        }
    metadata["account_registry_constraints"] = [
        drifted if item is constraint_trigger else item for item in constraints
    ]

    with pytest.raises(BackupError, match="account registry constraint contract"):
        _validate_restored_database(metadata, metadata.copy())


def test_company_reporting_restore_rejects_missing_posted_integrity_dependency() -> None:
    actual = _company_reporting_database_metadata()
    actual["company_reporting_required_functions"] = []

    with pytest.raises(BackupError, match="company reporting"):
        _validate_restored_database(actual, actual.copy())


@pytest.mark.parametrize(
    "mutation",
    [
        "schema_missing",
        "schema_owner",
        "function_missing",
        "function_result",
        "function_security_definer",
        "function_search_path",
        "function_extra_config",
        "required_table_missing",
        "required_column_missing",
        "required_function_security",
        "trigger_missing",
        "trigger_disabled",
        "raw_schema_acl_excess",
        "raw_schema_acl_reader_missing",
        "raw_function_acl_excess",
        "raw_function_acl_reader_missing",
        "schema_usage",
        "schema_create",
        "function_reader_execute",
        "function_other_execute",
    ],
)
def test_company_reporting_restore_metadata_rejects_drift(mutation: str) -> None:
    expected = _company_reporting_database_metadata()
    actual = {**expected}
    if mutation == "schema_missing":
        actual["company_reporting_schema"] = None
    elif mutation == "schema_owner":
        actual["company_reporting_schema"] = {
            "schema": COMPANY_REPORTING_SCHEMA,
            "owner": "stale_owner",
        }
    elif mutation.startswith("function_") and mutation not in {
        "function_reader_execute",
        "function_other_execute",
    }:
        rows = cast(list[dict[str, object]], expected["company_reporting_functions"])
        if mutation == "function_missing":
            actual["company_reporting_functions"] = rows[1:]
        else:
            field, value = {
                "function_result": ("result", "void"),
                "function_security_definer": (
                    "security_definer",
                    not cast(bool, rows[0]["security_definer"]),
                ),
                "function_search_path": ("proconfig", ["search_path=pg_temp"]),
                "function_extra_config": (
                    "proconfig",
                    ["search_path=pg_catalog", "row_security=off"],
                ),
            }[mutation]
            actual["company_reporting_functions"] = [{**rows[0], field: value}, *rows[1:]]
    elif mutation == "required_table_missing":
        rows = cast(list[dict[str, object]], expected["company_reporting_required_tables"])
        actual["company_reporting_required_tables"] = rows[1:]
    elif mutation == "required_column_missing":
        rows = cast(list[dict[str, object]], expected["company_reporting_required_columns"])
        actual["company_reporting_required_columns"] = rows[1:]
    elif mutation == "required_function_security":
        rows = cast(list[dict[str, object]], expected["company_reporting_required_functions"])
        actual["company_reporting_required_functions"] = [{**rows[0], "security_definer": False}]
    elif mutation.startswith("trigger_"):
        rows = cast(list[dict[str, object]], expected["company_reporting_triggers"])
        actual["company_reporting_triggers"] = (
            rows[1:] if mutation == "trigger_missing" else [{**rows[0], "enabled": "D"}, *rows[1:]]
        )
    elif mutation.startswith("raw_schema_acl_"):
        rows = cast(list[dict[str, object]], expected["company_reporting_schema_acls"])
        actual["company_reporting_schema_acls"] = (
            [*rows, {**rows[0], "grantee": "ledgerbridge_api"}]
            if mutation == "raw_schema_acl_excess"
            else [row for row in rows if row["grantee"] != "ledgerbridge_reader"]
        )
    elif mutation.startswith("raw_function_acl_"):
        rows = cast(list[dict[str, object]], expected["company_reporting_function_acls"])
        actual["company_reporting_function_acls"] = (
            [*rows, {**rows[0], "grantee": "ledgerbridge_api"}]
            if mutation == "raw_function_acl_excess"
            else [row for row in rows if row["grantee"] != "ledgerbridge_reader"]
        )
    elif mutation.startswith("schema_"):
        rows = cast(
            list[dict[str, object]],
            expected["company_reporting_effective_schema_privileges"],
        )
        target = next(i for i, row in enumerate(rows) if row["role"] == "ledgerbridge_reader")
        field, value = ("usage", False) if mutation == "schema_usage" else ("create", True)
        actual["company_reporting_effective_schema_privileges"] = [
            {**row, field: value} if i == target else row for i, row in enumerate(rows)
        ]
    else:
        rows = cast(
            list[dict[str, object]],
            expected["company_reporting_effective_function_privileges"],
        )
        target = next(
            i
            for i, row in enumerate(rows)
            if (
                mutation == "function_reader_execute"
                and row["role"] == "ledgerbridge_reader"
                and row["execute"] is True
            )
            or (
                mutation == "function_other_execute"
                and row["role"] == "ledgerbridge_api"
                and row["name"] == "get_company_report_v1_as_of"
            )
        )
        actual["company_reporting_effective_function_privileges"] = [
            {**row, "execute": not cast(bool, row["execute"])} if i == target else row
            for i, row in enumerate(rows)
        ]

    with pytest.raises(BackupError, match="company reporting"):
        _validate_restored_database(actual, actual.copy())


@pytest.mark.parametrize(
    "mutation",
    [
        "row_counts",
        "table_missing",
        "table_owner",
        "table_kind",
        "schema_missing",
        "schema_owner",
        "function_missing",
        "function_signature",
        "function_result",
        "function_owner",
        "function_security_definer",
        "function_search_path",
        "function_extra_config",
        "trigger_missing",
        "trigger_table",
        "trigger_constraint",
        "trigger_disabled",
        "trigger_type",
        "trigger_deferrable",
        "trigger_initially_deferred",
        "trigger_function",
        "trigger_function_schema",
        "constraint_missing",
        "constraint_table",
        "constraint_type",
        "constraint_definition",
        "constraint_validated",
        "constraint_deferrable",
        "constraint_initially_deferred",
        "constraint_extra",
        "effective_table_acl",
        "effective_table_truncate",
        "effective_function_acl",
        "raw_table_acl",
        "raw_function_acl",
        "raw_schema_acl",
        "schema_usage",
        "schema_create",
    ],
)
def test_bank_statement_restore_metadata_rejects_drift(mutation: str) -> None:
    expected = _bank_statement_database_metadata()
    actual = {**expected}
    if mutation == "row_counts":
        actual["bank_statement_row_counts"] = {"bank_statement": 0}
    elif mutation.startswith("table_"):
        rows = cast(list[dict[str, object]], expected["bank_statement_tables"])
        if mutation == "table_missing":
            actual["bank_statement_tables"] = rows[1:]
        else:
            table_field, table_value = {
                "table_owner": ("owner", "stale_owner"),
                "table_kind": ("kind", "v"),
            }[mutation]
            actual["bank_statement_tables"] = [
                {**rows[0], table_field: table_value},
                *rows[1:],
            ]
    elif mutation.startswith("schema_") and mutation in {"schema_missing", "schema_owner"}:
        rows = cast(list[dict[str, object]], expected["bank_statement_schemas"])
        actual["bank_statement_schemas"] = (
            rows[1:]
            if mutation == "schema_missing"
            else [{**rows[0], "owner": "stale_owner"}, *rows[1:]]
        )
    elif mutation.startswith("function_"):
        rows = cast(list[dict[str, object]], expected["bank_statement_functions"])
        if mutation == "function_missing":
            actual["bank_statement_functions"] = rows[1:]
        else:
            function_field, function_value = {
                "function_signature": ("identity_arguments", "p_request text"),
                "function_result": ("result", "void"),
                "function_owner": ("owner", "stale_owner"),
                "function_security_definer": (
                    "security_definer",
                    not cast(bool, rows[0]["security_definer"]),
                ),
                "function_search_path": ("proconfig", ["search_path=pg_temp"]),
                "function_extra_config": (
                    "proconfig",
                    ["search_path=pg_catalog", "row_security=off"],
                ),
            }[mutation]
            actual["bank_statement_functions"] = [
                {**rows[0], function_field: function_value},
                *rows[1:],
            ]
    elif mutation == "trigger_missing":
        actual["bank_statement_triggers"] = cast(
            list[dict[str, object]], expected["bank_statement_triggers"]
        )[1:]
    elif mutation.startswith("trigger_"):
        rows = cast(list[dict[str, object]], expected["bank_statement_triggers"])
        trigger_field, trigger_value = {
            "trigger_table": ("table", "bank_statement_review"),
            "trigger_constraint": (
                "constraint",
                not cast(bool, rows[0]["constraint"]),
            ),
            "trigger_disabled": ("enabled", "D"),
            "trigger_type": ("trigger_type", 7),
            "trigger_deferrable": (
                "deferrable",
                not cast(bool, rows[0]["deferrable"]),
            ),
            "trigger_initially_deferred": (
                "initially_deferred",
                not cast(bool, rows[0]["initially_deferred"]),
            ),
            "trigger_function": ("function_name", "r1_validate_bank_statement"),
            "trigger_function_schema": ("function_schema", "pg_catalog"),
        }[mutation]
        changed = {**rows[0], trigger_field: trigger_value}
        actual["bank_statement_triggers"] = [changed, *rows[1:]]
    elif mutation.startswith("constraint_"):
        rows = cast(list[dict[str, object]], expected["bank_statement_constraints"])
        if mutation == "constraint_missing":
            actual["bank_statement_constraints"] = rows[1:]
        elif mutation == "constraint_extra":
            actual["bank_statement_constraints"] = [
                *rows,
                {**rows[0], "name": "unexpected_constraint"},
            ]
        else:
            constraint_field, constraint_value = {
                "constraint_table": ("table", "bank_statement_review"),
                "constraint_type": ("type", "x"),
                "constraint_definition": ("definition", "CHECK (false)"),
                "constraint_validated": ("validated", False),
                "constraint_deferrable": ("deferrable", True),
                "constraint_initially_deferred": ("initially_deferred", True),
            }[mutation]
            actual["bank_statement_constraints"] = [
                {**rows[0], constraint_field: constraint_value},
                *rows[1:],
            ]
    elif mutation == "effective_table_acl":
        rows = cast(list[dict[str, object]], expected["bank_statement_effective_table_privileges"])
        actual["bank_statement_effective_table_privileges"] = [
            {**rows[0], "select": True},
            *rows[1:],
        ]
    elif mutation == "effective_table_truncate":
        rows = cast(list[dict[str, object]], expected["bank_statement_effective_table_privileges"])
        actual["bank_statement_effective_table_privileges"] = [
            {**rows[0], "truncate": True},
            *rows[1:],
        ]
    elif mutation == "effective_function_acl":
        rows = cast(
            list[dict[str, object]], expected["bank_statement_effective_function_privileges"]
        )
        target = next(i for i, row in enumerate(rows) if row["execute"] is True)
        actual["bank_statement_effective_function_privileges"] = [
            {**row, "execute": False} if i == target else row for i, row in enumerate(rows)
        ]
    elif mutation in {"raw_table_acl", "raw_function_acl", "raw_schema_acl"}:
        field = {
            "raw_table_acl": "bank_statement_table_acls",
            "raw_function_acl": "bank_statement_function_acls",
            "raw_schema_acl": "bank_statement_schema_acls",
        }[mutation]
        rows = cast(list[dict[str, object]], expected[field])
        actual[field] = [*rows, {**rows[0], "grantee": "stale_role"}]
    else:
        rows = cast(list[dict[str, object]], expected["bank_statement_effective_schema_privileges"])
        if mutation == "schema_usage":
            target = next(i for i, row in enumerate(rows) if row["usage"] is True)
            actual["bank_statement_effective_schema_privileges"] = [
                {**row, "usage": False} if i == target else row for i, row in enumerate(rows)
            ]
        else:
            actual["bank_statement_effective_schema_privileges"] = [
                {**rows[0], "create": True},
                *rows[1:],
            ]
    if mutation == "row_counts":
        with pytest.raises(BackupError, match="metadata differs"):
            _validate_restored_database(expected, actual)
    else:
        with pytest.raises(BackupError, match="bank statement"):
            _validate_restored_database(actual, actual.copy())


def test_fingerprint_normalization_and_validation() -> None:
    spaced = "0123 4567 89ab cdef 0123 4567 89ab cdef 0123 4567"
    assert _normalize_fingerprint(spaced) == FINGERPRINT

    with pytest.raises(BackupError, match="fingerprint"):
        _normalize_fingerprint("short")


def test_database_url_host_replacement_preserves_credentials_and_port() -> None:
    source = "postgresql+psycopg://ledgerbridge_app:p%40ss@postgres:5432/ledgerbridge"

    replaced = _replace_database_host(source, "ledgerbridge-restore-postgres-deadbeef")

    assert replaced == (
        "postgresql+psycopg://ledgerbridge_app:p%40ss@"
        "ledgerbridge-restore-postgres-deadbeef:5432/ledgerbridge"
    )


def test_restore_resource_names_are_exact_and_guarded() -> None:
    resources = RestoreResources.create("deadbeef")
    assert resources.container == "ledgerbridge-restore-postgres-deadbeef"
    assert resources.network == "ledgerbridge-restore-network-deadbeef"
    assert resources.database_volume == "ledgerbridge_restore_db_deadbeef"
    assert resources.artifact_volume == "ledgerbridge_restore_artifacts_deadbeef"

    with pytest.raises(BackupError, match="eight lowercase hex"):
        RestoreResources.create("../unsafe")


def test_source_state_comparison_detects_production_drift() -> None:
    before = _source_state()
    _assert_source_unchanged(before, before)

    after = replace(before, database={"alembic_version": "unexpected"})
    with pytest.raises(BackupError, match="database metadata"):
        _assert_source_unchanged(before, after)


class _ImageIdentityRunner:
    def __init__(self, *, image_id: str, revision: str) -> None:
        self.image_id = image_id
        self.revision = revision

    def capture(self, args: list[str], **kwargs: object) -> str:
        del kwargs
        output_format = args[4]
        if output_format == "{{.Id}}":
            return self.image_id
        if "org.opencontainers.image.revision" in output_format:
            return self.revision
        raise AssertionError(f"unexpected image inspection: {args}")


def test_backup_image_rejects_mutable_tag_drift() -> None:
    revision = "a" * 40
    backup_image_id = f"sha256:{'b' * 64}"
    runner = cast(
        Runner,
        _ImageIdentityRunner(image_id=f"sha256:{'c' * 64}", revision=revision),
    )

    with pytest.raises(BackupError, match="immutable image ID"):
        _validate_backup_image(
            runner,
            "ledgerbridge-app:abcdef0",
            backup_image_id,
            revision,
        )


def test_backup_image_returns_verified_immutable_id() -> None:
    revision = "a" * 40
    image_id = f"sha256:{'b' * 64}"
    runner = cast(Runner, _ImageIdentityRunner(image_id=image_id, revision=revision))

    assert (
        _validate_backup_image(
            runner,
            "ledgerbridge-app:abcdef0",
            image_id,
            revision,
        )
        == image_id
    )


def test_safe_extract_accepts_only_expected_regular_file(tmp_path: Path) -> None:
    archive = tmp_path / "payload.tar"
    contents = b"verified"
    with tarfile.open(archive, "w:") as bundle:
        member = tarfile.TarInfo("metadata.json")
        member.size = len(contents)
        member.mode = 0o600
        bundle.addfile(member, io.BytesIO(contents))

    destination = tmp_path / "payload"
    _safe_extract_tar(archive, destination, expected_files={"metadata.json"})

    assert (destination / "metadata.json").read_bytes() == contents


@pytest.mark.parametrize(("name", "link"), [("../escape", ""), ("link", "/etc/passwd")])
def test_safe_extract_rejects_traversal_and_symlink(tmp_path: Path, name: str, link: str) -> None:
    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w:") as bundle:
        member = tarfile.TarInfo(name)
        if link:
            member.type = tarfile.SYMTYPE
            member.linkname = link
        bundle.addfile(member)

    with pytest.raises(BackupError, match="unsafe"):
        _safe_extract_tar(archive, tmp_path / "extract")


def test_payload_hash_manifest_detects_tampering(tmp_path: Path) -> None:
    for name in (
        "database.dump",
        "roles.sql",
        "artifacts.tar",
        "deployment-tree.tar",
        "metadata.json",
    ):
        (tmp_path / name).write_bytes(name.encode())
    _write_payload_hashes(tmp_path)
    _verify_payload_hashes(tmp_path)

    (tmp_path / "roles.sql").write_text("tampered", encoding="utf-8")
    with pytest.raises(BackupError, match="hash mismatch"):
        _verify_payload_hashes(tmp_path)


def test_restored_database_requires_nonempty_runtime_grants() -> None:
    expected = _database_metadata()
    _validate_restored_database(expected, expected.copy())

    invalid = expected | {"role_grant_count": 0}
    with pytest.raises(BackupError, match="no restored table grants"):
        _validate_restored_database(invalid, invalid.copy())


def test_v1_database_metadata_compares_only_legacy_source_fields() -> None:
    expected = _database_metadata()
    actual = expected | {
        "metadata_version": 2,
        "security_functions": [{"name": "legacy", "proconfig": []}],
    }

    compared = _validate_restored_database(expected, actual)

    assert compared == sorted(expected)
    assert BACKUP_FORMAT_V1 != BACKUP_FORMAT_V2
    assert BACKUP_FORMAT_V2 != BACKUP_FORMAT_V3


def test_v2_database_metadata_requires_exact_rich_comparison() -> None:
    expected = _database_metadata() | {"metadata_version": 2}
    actual = expected | {"unexpected": True}

    with pytest.raises(BackupError, match="metadata differs"):
        _validate_restored_database(expected, actual)


def test_v2_database_metadata_requires_trigger_and_grant_baseline() -> None:
    expected = _database_metadata() | {
        "metadata_version": 2,
        "alembic_version": "20260822_0004",
        "database_temp_denied": True,
        "security_functions": [
            {"name": name, "proconfig": ["search_path=pg_catalog"]}
            for name in sorted(PHASE_1_FUNCTIONS | PHASE_2_FUNCTIONS | PHASE_3_FUNCTIONS)
        ],
        "public_triggers": [
            {"name": name, "enabled": "O"}
            for name in sorted(PHASE_1_TRIGGERS | PHASE_2_TRIGGERS | PHASE_3_TRIGGERS)
        ],
        "table_grants": [
            {"table": table, "privilege": privilege, "grantable": "NO"}
            for table, privilege in sorted(
                PHASE_1_TABLE_PRIVILEGES | PHASE_2_TABLE_PRIVILEGES | PHASE_3_TABLE_PRIVILEGES
            )
        ],
        "column_grants": [
            {
                "table": table,
                "column": column,
                "privilege": privilege,
                "grantable": "NO",
            }
            for table, column, privilege in sorted(
                PHASE_2_COLUMN_PRIVILEGES | PHASE_3_COLUMN_PRIVILEGES
            )
        ],
        "sequence_grants": [],
        "function_grants": [
            {
                "function": "append_audit_event",
                "grantee": "ledgerbridge_app",
                "privilege": "EXECUTE",
                "grantable": "NO",
            }
        ],
    }
    _validate_restored_database(expected, expected.copy())

    missing_trigger = expected | {
        "public_triggers": cast(list[str], expected["public_triggers"])[:-1],
    }
    with pytest.raises(BackupError, match="required triggers"):
        _validate_restored_database(missing_trigger, missing_trigger.copy())

    excess_grant = expected | {
        "table_grants": [
            *cast(list[dict[str, object]], expected["table_grants"]),
            {"table": "audit_event", "privilege": "UPDATE", "grantable": "NO"},
        ]
    }
    with pytest.raises(BackupError, match="table grants"):
        _validate_restored_database(excess_grant, excess_grant.copy())

    excess_column_grant = expected | {
        "column_grants": [
            *cast(list[dict[str, object]], expected["column_grants"]),
            {
                "table": "import_job",
                "column": "source_system",
                "privilege": "UPDATE",
                "grantable": "NO",
            },
        ]
    }
    with pytest.raises(BackupError, match="column grants"):
        _validate_restored_database(excess_column_grant, excess_column_grant.copy())


def test_r1_database_metadata_verifies_role_acl_catalog_and_effective_privileges() -> None:
    expected = _r1_database_metadata()
    _validate_restored_database(expected, expected.copy())

    app_fact_grant = {
        **expected,
        "r1_effective_table_privileges": [
            {
                **cast(list[dict[str, object]], expected["r1_effective_table_privileges"])[0],
                "role": "ledgerbridge_app",
                "select": True,
            },
            *cast(list[dict[str, object]], expected["r1_effective_table_privileges"])[1:],
        ],
    }
    with pytest.raises(BackupError, match=r"ledgerbridge_app.*R1 table grant"):
        _validate_restored_database(app_fact_grant, app_fact_grant.copy())

    worker_truncate = {
        **expected,
        "r1_effective_table_privileges": [
            {
                **row,
                "truncate": True,
            }
            if row.get("role") == "ledgerbridge_worker"
            and row.get("schema") == "public"
            and row.get("object") == R1_PUBLIC_TABLES[0]
            else row
            for row in cast(list[dict[str, object]], expected["r1_effective_table_privileges"])
        ],
    }
    with pytest.raises(BackupError, match="fact table"):
        _validate_restored_database(worker_truncate, worker_truncate.copy())

    public_create = {
        **expected,
        "r1_schema_acl": [
            {
                "schema": "public",
                "grantee": "PUBLIC",
                "privilege": "CREATE",
                "grantable": "NO",
            }
        ],
    }
    with pytest.raises(BackupError, match="schema CREATE"):
        _validate_restored_database(public_create, public_create.copy())

    public_function_execute = {
        **expected,
        "r1_effective_function_privileges": [
            {
                **row,
                "execute": True,
            }
            if row.get("schema") == "public" and row.get("role") == "ledgerbridge_reader"
            else row
            for row in cast(list[dict[str, object]], expected["r1_effective_function_privileges"])
        ],
    }
    with pytest.raises(BackupError, match="public validator"):
        _validate_restored_database(public_function_execute, public_function_execute.copy())


def test_r1_security_sql_and_verifier_cover_optional_backup_role() -> None:
    assert "ledgerbridge_backup" in R1_SECURITY_SQL
    assert "FROM pg_database" in R1_SECURITY_SQL
    assert (
        "observed_roles(role_name) AS (\n"
        "    SELECT role_name FROM expected_roles\n"
        "    UNION\n"
        "    SELECT role_name FROM database_owner\n"
        ")"
    ) in R1_SECURITY_SQL
    assert "FROM expected_roles AS e\n      JOIN pg_roles AS r" in R1_SECURITY_SQL
    assert "FROM observed_roles AS e\n      JOIN pg_roles AS r" in R1_SECURITY_SQL
    assert "'truncate', has_table_privilege" in R1_SECURITY_SQL
    expected = _r1_database_metadata(include_backup=True)
    _validate_restored_database(expected, expected.copy())

    backup_database_acl = [
        item
        for item in cast(list[dict[str, object]], expected["r1_database_acl"])
        if item.get("grantee") == "ledgerbridge_backup"
    ]
    assert backup_database_acl == [
        {"grantee": "ledgerbridge_backup", "privilege": "CONNECT", "grantable": "NO"}
    ]

    backup_fact_grant = {
        **expected,
        "r1_effective_table_privileges": [
            {
                **row,
                "select": True,
            }
            if row.get("role") == "ledgerbridge_backup"
            and row.get("schema") == "public"
            and row.get("object") == R1_PUBLIC_TABLES[0]
            else row
            for row in cast(list[dict[str, object]], expected["r1_effective_table_privileges"])
        ],
    }
    with pytest.raises(BackupError, match="fact table"):
        _validate_restored_database(backup_fact_grant, backup_fact_grant.copy())


@pytest.mark.parametrize("direction", ["member", "granted"])
def test_r1_database_metadata_rejects_database_owner_membership_drift(direction: str) -> None:
    expected = _r1_database_metadata()
    drifted = {
        **expected,
        "r1_role_matrix": [
            {
                **item,
                "memberships": [
                    {
                        "direction": direction,
                        "role": "stale_login",
                        "admin_option": False,
                        "inherit_option": False,
                        "set_option": False,
                    }
                ],
            }
            if item.get("role") == "ledgerbridge_owner"
            else item
            for item in cast(list[dict[str, object]], expected["r1_role_matrix"])
        ],
    }
    with pytest.raises(BackupError, match="role matrix is privileged or non-isolated"):
        _validate_restored_database(drifted, drifted.copy())


def test_r1_database_metadata_accepts_legacy_matrix_without_owner_observation() -> None:
    actual = _r1_database_metadata()
    expected = {
        **actual,
        "r1_role_matrix": [
            item
            for item in cast(list[dict[str, object]], actual["r1_role_matrix"])
            if item.get("role") != "ledgerbridge_owner"
        ],
    }
    _validate_restored_database(expected, actual)


def test_r1_owner_compatibility_does_not_hide_other_metadata_drift() -> None:
    actual = _r1_database_metadata()
    expected = {
        **actual,
        "r1_role_matrix": [
            item
            for item in cast(list[dict[str, object]], actual["r1_role_matrix"])
            if item.get("role") != "ledgerbridge_owner"
        ],
    }
    unknown_role = {
        **actual,
        "r1_role_matrix": [
            *cast(list[dict[str, object]], actual["r1_role_matrix"]),
            {"role": "stale_login"},
        ],
    }
    extra_acl = {
        **actual,
        "r1_database_acl": [
            *cast(list[dict[str, object]], actual["r1_database_acl"]),
            {"grantee": "stale_login", "privilege": "CONNECT", "grantable": "NO"},
        ],
    }
    owner_membership_drift = {
        **actual,
        "r1_role_matrix": [
            {
                **item,
                "memberships": [{"direction": "granted", "role": "stale_login"}],
            }
            if item.get("role") == "ledgerbridge_owner"
            else item
            for item in cast(list[dict[str, object]], actual["r1_role_matrix"])
        ],
    }
    owner_acl_drift = {
        **actual,
        "r1_schema_acl": [
            item
            for item in cast(list[dict[str, object]], actual["r1_schema_acl"])
            if not (
                item.get("schema") == "public"
                and item.get("grantee") == "ledgerbridge_owner"
                and item.get("privilege") == "CREATE"
            )
        ],
    }
    extra_field = {**actual, "unexpected": True}
    for drifted in (unknown_role, extra_acl, extra_field):
        with pytest.raises(BackupError, match="metadata differs"):
            _validate_restored_database(expected, drifted)
    # PostgreSQL may omit or represent owner privileges through
    # pg_database_owner during a restore; effective ownership is checked
    # separately and must not make an equivalent restore fail.
    _validate_restored_database(actual, owner_acl_drift)
    with pytest.raises(BackupError, match="role matrix is privileged"):
        _validate_restored_database(expected, owner_membership_drift)


@pytest.mark.parametrize(
    ("field", "entry"),
    [
        (
            "r1_database_acl",
            {"grantee": "retired_role", "privilege": "CONNECT", "grantable": "NO"},
        ),
        (
            "r1_schema_acl",
            {
                "schema": "public",
                "grantee": "retired_role",
                "privilege": "USAGE",
                "grantable": "NO",
            },
        ),
        (
            "r1_default_acls",
            {
                "owner": "ledgerbridge_owner",
                "schema": "public",
                "object_type": "r",
                "grantee": "retired_role",
                "privilege": "SELECT",
                "grantable": "NO",
            },
        ),
    ],
)
def test_r1_database_metadata_rejects_unknown_or_stale_acl_grantees(
    field: str, entry: dict[str, object]
) -> None:
    expected = _r1_database_metadata()
    drifted = {
        **expected,
        field: [*cast(list[dict[str, object]], expected[field]), entry],
    }
    with pytest.raises(BackupError, match="unknown or stale grantee"):
        _validate_restored_database(drifted, drifted.copy())


def test_r1_database_metadata_rejects_excess_allowlisted_acl_grants() -> None:
    expected = _r1_database_metadata()
    excess_cases = (
        (
            "r1_database_acl",
            {"grantee": "ledgerbridge_reader", "privilege": "CREATE", "grantable": "NO"},
        ),
        (
            "r1_schema_acl",
            {
                "schema": "public",
                "grantee": "ledgerbridge_reader",
                "privilege": "USAGE",
                "grantable": "NO",
            },
        ),
        (
            "r1_default_acls",
            {
                "owner": "ledgerbridge_owner",
                "schema": "public",
                "object_type": "r",
                "grantee": "ledgerbridge_reader",
                "privilege": "SELECT",
                "grantable": "NO",
            },
        ),
    )
    for field, entry in excess_cases:
        drifted = {
            **expected,
            field: [*cast(list[dict[str, object]], expected[field]), entry],
        }
        with pytest.raises(BackupError, match=r"(excess|over-privileged)"):
            _validate_restored_database(drifted, drifted.copy())


def test_r1_database_metadata_requires_fixed_owner_for_views_and_functions() -> None:
    expected = _r1_database_metadata()
    view_owner_drift = {
        **expected,
        "r1_views": [
            {**item, "owner": "stale_owner"}
            if item.get("name") == R1_INTERNAL_READ_VIEWS[0]
            else item
            for item in cast(list[dict[str, object]], expected["r1_views"])
        ],
    }
    with pytest.raises(BackupError, match="view security boundary"):
        _validate_restored_database(view_owner_drift, view_owner_drift.copy())

    function_owner_drift = {
        **expected,
        "r1_functions": [
            {**item, "owner": "stale_owner"}
            if item.get("schema") == "internal_read"
            and item.get("name") == R1_INTERNAL_READ_FUNCTIONS[0]
            else item
            for item in cast(list[dict[str, object]], expected["r1_functions"])
        ],
    }
    with pytest.raises(BackupError, match="function security boundary"):
        _validate_restored_database(function_owner_drift, function_owner_drift.copy())


@pytest.mark.parametrize(
    ("field", "value"),
    [("owner", "stale_owner"), ("security_definer", False), ("proconfig", [])],
)
def test_r1_database_metadata_checks_ledger_summary_function_security(
    field: str, value: object
) -> None:
    expected = _r1_database_metadata()
    drifted = {
        **expected,
        "r1_functions": [
            {**item, field: value}
            if item.get("schema") == "internal_read"
            and item.get("name") == "get_ledger_summary_as_of"
            else item
            for item in cast(list[dict[str, object]], expected["r1_functions"])
        ],
    }
    with pytest.raises(BackupError, match="function security boundary"):
        _validate_restored_database(drifted, drifted.copy())


def test_r1_database_metadata_requires_ledger_summary_reader_execute() -> None:
    expected = _r1_database_metadata()
    functions = cast(list[dict[str, object]], expected["r1_functions"])
    summary = next(
        item
        for item in functions
        if item.get("schema") == "internal_read" and item.get("name") == "get_ledger_summary_as_of"
    )
    assert (
        summary["identity_arguments"]
        == R1_INTERNAL_READ_FUNCTION_SIGNATURES["get_ledger_summary_as_of"]
    )
    assert summary["owner"] == "ledgerbridge_owner"
    assert summary["security_definer"] is True
    assert summary["proconfig"] == ["search_path=pg_catalog"]

    function_privileges = cast(
        list[dict[str, object]], expected["r1_effective_function_privileges"]
    )
    reader_summary = [
        item
        for item in function_privileges
        if item.get("role") == "ledgerbridge_reader"
        and item.get("schema") == "internal_read"
        and item.get("name") == "get_ledger_summary_as_of"
    ]
    assert len(reader_summary) == 1
    assert reader_summary[0]["execute"] is True

    missing = {
        **expected,
        "r1_functions": [
            item for item in functions if item.get("name") != "get_ledger_summary_as_of"
        ],
        "r1_effective_function_privileges": [
            item for item in function_privileges if item.get("name") != "get_ledger_summary_as_of"
        ],
    }
    with pytest.raises(BackupError, match="internal_read functions"):
        _validate_restored_database(missing, missing.copy())

    reader_execute_drift = {
        **expected,
        "r1_effective_function_privileges": [
            {**item, "execute": False}
            if item.get("role") == "ledgerbridge_reader"
            and item.get("schema") == "internal_read"
            and item.get("name") == "get_ledger_summary_as_of"
            else item
            for item in function_privileges
        ],
    }
    with pytest.raises(BackupError, match="function privilege matrix"):
        _validate_restored_database(reader_execute_drift, reader_execute_drift.copy())


def test_r1_security_sql_matches_fixed_function_signatures() -> None:
    assert tuple(R1_INTERNAL_READ_FUNCTION_SIGNATURES) == R1_INTERNAL_READ_FUNCTIONS
    assert "expected_r1_functions(function_name, identity_arguments) AS" in R1_SECURITY_SQL
    assert "expected.function_name = p.proname" in R1_SECURITY_SQL
    assert (
        "expected.identity_arguments = pg_get_function_identity_arguments(p.oid)" in R1_SECURITY_SQL
    )
    assert "p.proname IN" not in R1_SECURITY_SQL
    for name, identity_arguments in R1_INTERNAL_READ_FUNCTION_SIGNATURES.items():
        assert f"('{name}', '{identity_arguments}')" in R1_SECURITY_SQL


def test_r1_0029_requires_multi_scope_candidate_reader_metadata() -> None:
    metadata = _r1_database_metadata()
    metadata["alembic_version"] = "20260901_0029"
    functions = cast(list[dict[str, object]], metadata["r1_functions"])
    metadata["r1_functions"] = [
        item for item in functions if item.get("name") != "list_candidates_for_scopes_as_of"
    ]

    with pytest.raises(BackupError, match="internal_read functions"):
        _validate_restored_database(metadata, metadata.copy())


def test_r1_0030_sibling_does_not_require_0029_multi_scope_reader() -> None:
    metadata = _r1_database_metadata()
    metadata["alembic_version"] = "20260902_0030"
    functions = cast(list[dict[str, object]], metadata["r1_functions"])
    metadata["r1_functions"] = [
        item for item in functions if item.get("name") != "list_candidates_for_scopes_as_of"
    ]
    function_privileges = cast(
        list[dict[str, object]], metadata["r1_effective_function_privileges"]
    )
    metadata["r1_effective_function_privileges"] = [
        item
        for item in function_privileges
        if item.get("name") != "list_candidates_for_scopes_as_of"
    ]
    schema_privileges = cast(list[dict[str, object]], metadata["r1_effective_schema_privileges"])
    metadata["r1_effective_schema_privileges"] = [
        {**item, "usage": True}
        if item.get("role") == "ledgerbridge_api" and item.get("schema") == "internal_read"
        else item
        for item in schema_privileges
    ]

    _validate_r1_database_security(metadata)


@pytest.mark.parametrize("mutation", ["missing", "wrong_signature", "overload"])
def test_r1_database_metadata_rejects_non_allowlisted_function_signatures(
    mutation: str,
) -> None:
    expected = _r1_database_metadata()
    functions = cast(list[dict[str, object]], expected["r1_functions"])
    summary = next(
        item
        for item in functions
        if item.get("schema") == "internal_read" and item.get("name") == "get_ledger_summary_as_of"
    )
    if mutation == "missing":
        drifted_functions = [item for item in functions if item is not summary]
    elif mutation == "wrong_signature":
        drifted_functions = [
            {**item, "identity_arguments": "uuid"} if item is summary else item
            for item in functions
        ]
    else:
        drifted_functions = [*functions, {**summary, "identity_arguments": "uuid"}]

    drifted = {**expected, "r1_functions": drifted_functions}
    with pytest.raises(BackupError, match="signature baseline"):
        _validate_restored_database(drifted, drifted.copy())


@pytest.mark.parametrize("mutation", ["missing", "wrong_signature", "overload"])
def test_r1_database_metadata_uses_fixed_signatures_for_function_privileges(
    mutation: str,
) -> None:
    expected = _r1_database_metadata()
    function_privileges = cast(
        list[dict[str, object]], expected["r1_effective_function_privileges"]
    )
    summary_rows = [
        item
        for item in function_privileges
        if item.get("schema") == "internal_read" and item.get("name") == "get_ledger_summary_as_of"
    ]
    assert summary_rows
    if mutation == "missing":
        drifted_privileges = [item for item in function_privileges if item not in summary_rows]
    elif mutation == "wrong_signature":
        drifted_privileges = [
            {**item, "identity_arguments": "uuid"} if item in summary_rows else item
            for item in function_privileges
        ]
    else:
        drifted_privileges = [
            *function_privileges,
            {**summary_rows[0], "identity_arguments": "uuid"},
        ]

    drifted = {
        **expected,
        "r1_effective_function_privileges": drifted_privileges,
    }
    with pytest.raises(BackupError, match="function privilege matrix"):
        _validate_restored_database(drifted, drifted.copy())


def test_r1_database_metadata_requires_closed_objects_and_default_acl() -> None:
    expected = _r1_database_metadata()
    missing_role = {
        **expected,
        "r1_role_matrix": cast(list[dict[str, object]], expected["r1_role_matrix"])[1:],
    }
    with pytest.raises(BackupError, match="role matrix"):
        _validate_restored_database(missing_role, missing_role.copy())

    missing_constraint = {
        **expected,
        "r1_constraints": cast(list[dict[str, object]], expected["r1_constraints"])[1:],
    }
    with pytest.raises(BackupError, match="constraints"):
        _validate_restored_database(missing_constraint, missing_constraint.copy())

    missing_trigger = {
        **expected,
        "r1_triggers": cast(list[dict[str, object]], expected["r1_triggers"])[1:],
    }
    with pytest.raises(BackupError, match="triggers"):
        _validate_restored_database(missing_trigger, missing_trigger.copy())

    missing_view = {
        **expected,
        "r1_views": cast(list[dict[str, object]], expected["r1_views"])[1:],
    }
    with pytest.raises(BackupError, match="views"):
        _validate_restored_database(missing_view, missing_view.copy())

    missing_function = {
        **expected,
        "r1_functions": cast(list[dict[str, object]], expected["r1_functions"])[1:],
    }
    with pytest.raises(BackupError, match="functions"):
        _validate_restored_database(missing_function, missing_function.copy())

    missing_function_privilege = {
        **expected,
        "r1_effective_function_privileges": cast(
            list[dict[str, object]], expected["r1_effective_function_privileges"]
        )[1:],
    }
    with pytest.raises(BackupError, match="effective function"):
        _validate_restored_database(missing_function_privilege, missing_function_privilege.copy())

    default_acl_drift = {
        **expected,
        "r1_default_acls": [
            {
                "owner": "ledgerbridge_owner",
                "schema": "public",
                "object_type": "r",
                "grantee": "PUBLIC",
                "privilege": "SELECT",
                "grantable": "NO",
            }
        ],
    }
    with pytest.raises(BackupError, match="default ACL"):
        _validate_restored_database(default_acl_drift, default_acl_drift.copy())


def test_artifact_archive_metadata_counts_published_and_staging_bytes(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "artifacts.tar"
    published = b"published"
    digest = hashlib.sha256(published).hexdigest()
    with tarfile.open(archive, "w:") as bundle:
        for directory in (".", "./.staging", "./sha256", "./sha256/aa", "./sha256/aa/bb"):
            member = tarfile.TarInfo(directory)
            member.type = tarfile.DIRTYPE
            bundle.addfile(member)
        for name, contents in (
            (f"./sha256/{digest[:2]}/{digest[2:4]}/{digest}", published),
            ("./.staging/artifact-partial", b"stage"),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            bundle.addfile(member, io.BytesIO(contents))
    quota = {
        "per_artifact_max_bytes": 100,
        "published_max_bytes": 100,
        "staging_max_bytes": 100,
        "staging_ttl_seconds": 60,
    }

    observed = _artifact_archive_metadata(archive, quota)

    assert observed == {
        "published_bytes": 9,
        "staging_bytes": 5,
        "unsafe_entries": 0,
        "quota": quota,
        "artifact_count": 1,
        "artifact_manifest_sha256": hashlib.sha256(
            f"{digest}:9:sha256/{digest[:2]}/{digest[2:4]}/{digest}".encode()
        ).hexdigest(),
    }


def test_artifact_archive_metadata_rejects_digest_content_mismatch(tmp_path: Path) -> None:
    archive = tmp_path / "artifacts.tar"
    digest = "aabb" + "0" * 60
    with tarfile.open(archive, "w:") as bundle:
        member = tarfile.TarInfo(f"sha256/aa/bb/{digest}")
        member.size = len(b"wrong")
        bundle.addfile(member, io.BytesIO(b"wrong"))

    with pytest.raises(BackupError, match="digest"):
        _artifact_archive_metadata(
            archive,
            {
                "per_artifact_max_bytes": 100,
                "published_max_bytes": 100,
                "staging_max_bytes": 100,
                "staging_ttl_seconds": 60,
            },
        )


@pytest.mark.parametrize("field", ["function_count", "trigger_count"])
def test_restored_database_requires_schema_objects(field: str) -> None:
    invalid = _database_metadata() | {field: 0}

    with pytest.raises(BackupError, match="lacks required objects"):
        _validate_restored_database(invalid, invalid.copy())


class _InterruptingRunner:
    def run(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt


def test_backup_interrupt_restarts_services_and_removes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    backup_root = tmp_path / "backups"
    work_root = tmp_path / "work"
    gpg_home = tmp_path / "gpg"
    for directory in (project, backup_root, work_root, gpg_home):
        directory.mkdir()
    config = CommonConfig(
        project_dir=project,
        backup_root=backup_root,
        work_root=work_root,
        gpg_home=gpg_home,
        fingerprint=FINGERPRINT,
    )
    state = _source_state()
    restarts: list[SourceState] = []
    monkeypatch.setattr(
        "scripts.backup_restore._validated_config",
        lambda value, runner: value,
    )
    monkeypatch.setattr(
        "scripts.backup_restore._collect_source_state",
        lambda value, runner: state,
    )
    monkeypatch.setattr(
        "scripts.backup_restore._assert_tree_has_no_symlinks",
        lambda value: None,
    )
    monkeypatch.setattr(
        "scripts.backup_restore._restart_application",
        lambda runner, value: restarts.append(value),
    )

    with pytest.raises(KeyboardInterrupt):
        create_backup(config, cast(Runner, _InterruptingRunner()))

    assert restarts == [state]
    assert list(backup_root.iterdir()) == []
    assert list(work_root.iterdir()) == []
