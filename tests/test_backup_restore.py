from __future__ import annotations

import hashlib
import io
import tarfile
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from scripts.backup_restore import (
    BACKUP_FORMAT_V1,
    BACKUP_FORMAT_V2,
    BACKUP_FORMAT_V3,
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
    RestoreResources,
    Runner,
    SourceState,
    _artifact_archive_metadata,
    _assert_source_unchanged,
    _normalize_fingerprint,
    _replace_database_host,
    _safe_extract_tar,
    _validate_backup_image,
    _validate_restored_database,
    _verify_payload_hashes,
    _write_payload_hashes,
    create_backup,
)

FINGERPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"


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
                        role == "ledgerbridge_reader" and function["schema"] == "internal_read"
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
    for drifted in (unknown_role, extra_acl, owner_acl_drift, extra_field):
        with pytest.raises(BackupError, match="metadata differs"):
            _validate_restored_database(expected, drifted)
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
    assert summary["identity_arguments"] == "uuid, uuid, date, date, bigint, bytea"
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
