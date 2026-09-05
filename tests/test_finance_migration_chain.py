"""Exercise the migration graph and restore contracts at each release boundary."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from scripts import backup_restore as backup
from tests.test_company_transaction_classification import _restore_metadata

PRODUCTION = "20260905_0046"
FEE = "20260905_0047"
PAYROLL = "20260905_0048"
CORRECTION = "20260905_0049"
# 0050 only replaces a trigger function body; it registers no new object.
SNAPSHOT_TRIGGER_REPAIR = "20260906_0050"
CORRECTION_FUNCTION = ("internal_import", "correct_company_transaction_reporting_item")
STAGES = (PRODUCTION, FEE, PAYROLL, CORRECTION, SNAPSHOT_TRIGGER_REPAIR)
OWNER = "ledgerbridge_owner"
V1_COMMAND = ("internal_command", "review_company_transaction_classification")
V1_SUMMARY = ("internal_read", "get_company_transaction_classification_summary_as_of")
V2_COMMAND = ("internal_command", "review_company_transaction_classification_v2")
V2_SUMMARY = ("internal_read", "get_company_transaction_classification_summary_v2_as_of")
PAYROLL_READER = ("internal_read", "list_payroll_disbursement_records_as_of")
# Independent catalog observations: deriving these from the backup allowlist
# would let an accidentally omitted registration disappear from both sides.
ADDITIONS = {
    CORRECTION_FUNCTION: (
        CORRECTION,
        "p_transaction_ref uuid, p_expected_revision integer, "
        "p_expected_category_code text, p_expected_reporting_item_code text, "
        "p_reporting_item_code text, p_operation_id uuid, p_actor_ref text, p_reason text",
        "jsonb",
        None,
    ),
    V2_COMMAND: (
        FEE,
        "p_transaction_ref uuid, p_entity_ref uuid, p_operation_id uuid, "
        "p_assertion_jti uuid, p_actor_ref text, p_workload_principal_ref text, "
        "p_expected_revision integer, p_category_code text, p_reporting_item_code text, "
        "p_reason text",
        "jsonb",
        "ledgerbridge_api",
    ),
    V2_SUMMARY: (
        FEE,
        "p_entity_ref uuid, p_from_date date, p_to_date_exclusive date, "
        "p_audit_horizon_sequence bigint, p_audit_horizon_hash bytea",
        "jsonb",
        "ledgerbridge_reader",
    ),
    PAYROLL_READER: (
        PAYROLL,
        "p_entity_ref uuid, p_pay_period text, p_audit_horizon_sequence bigint, "
        "p_audit_horizon_hash bytea, p_limit integer",
        "TABLE(item jsonb)",
        "ledgerbridge_reader",
    ),
}


def _key(row: dict[str, object]) -> tuple[str, str]:
    return str(row["schema"]), str(row["name"])


def _rows(metadata: dict[str, object], name: str) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], metadata[name])


def _stage_metadata(revision: str) -> dict[str, object]:
    metadata = _restore_metadata()
    functions = [
        row
        for row in _rows(metadata, "company_transaction_classification_functions")
        if _key(row) not in ADDITIONS
    ]
    executors = dict(backup.COMPANY_TRANSACTION_CLASSIFICATION_FUNCTION_EXECUTORS)
    for key in ADDITIONS:
        executors.pop(key, None)
    if revision == PRODUCTION:
        executors[V1_COMMAND] = "ledgerbridge_api"
        executors[V1_SUMMARY] = "ledgerbridge_reader"
    else:
        executors.pop(V1_COMMAND, None)
        executors.pop(V1_SUMMARY, None)
    for (schema, name), (introduced, arguments, result, executor) in ADDITIONS.items():
        if STAGES.index(revision) < STAGES.index(introduced):
            continue
        functions.append(
            {
                "schema": schema,
                "name": name,
                "identity_arguments": arguments,
                "result": result,
                "owner": OWNER,
                "security_definer": True,
                "proconfig": ["search_path=pg_catalog"],
            }
        )
        if executor is not None:
            executors[(schema, name)] = executor
    metadata["company_transaction_classification_functions"] = functions
    metadata["company_transaction_classification_function_acls"] = [
        {
            "schema": row["schema"],
            "name": row["name"],
            "identity_arguments": row["identity_arguments"],
            "grantee": grantee,
            "privilege": "EXECUTE",
            "grantable": False,
        }
        for row in functions
        for grantee in [OWNER, *([executors[_key(row)]] if _key(row) in executors else [])]
    ]
    metadata["company_transaction_classification_effective_function_privileges"] = [
        {
            "schema": row["schema"],
            "name": row["name"],
            "identity_arguments": row["identity_arguments"],
            "role": role,
            "execute": role == executors.get(_key(row)),
        }
        for row in functions
        for role in backup.R1_ROLES
    ]
    return metadata


def test_finance_release_has_one_unambiguous_migration_path() -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    # Alembic warns on duplicate revision IDs instead of reliably rejecting
    # them. Escalate warnings while constructing its actual revision map.
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        revisions = list(scripts.walk_revisions())
        assert scripts.get_heads() == [SNAPSHOT_TRIGGER_REPAIR]
    assert len({item.revision for item in revisions}) == len(revisions)
    release_path = list(scripts.iterate_revisions(SNAPSHOT_TRIGGER_REPAIR, "20260904_0045"))
    assert [item.revision for item in release_path] == list(reversed(STAGES))
    assert all(item.dependencies is None for item in release_path)


@pytest.mark.parametrize("revision", STAGES)
def test_restore_accepts_each_released_stage(revision: str) -> None:
    assert revision in backup.MYBANK_CUTOVER_SCHEMA_REVISIONS
    backup._validate_company_transaction_classification_security(
        _stage_metadata(revision), revision=revision
    )


@pytest.mark.parametrize("function", [V2_COMMAND, V2_SUMMARY, PAYROLL_READER, CORRECTION_FUNCTION])
def test_restore_rejects_missing_new_function(function: tuple[str, str]) -> None:
    revision = ADDITIONS[function][0]
    metadata = _stage_metadata(revision)
    metadata["company_transaction_classification_functions"] = [
        row
        for row in _rows(metadata, "company_transaction_classification_functions")
        if _key(row) != function
    ]
    with pytest.raises(backup.BackupError):
        backup._validate_company_transaction_classification_security(metadata, revision=revision)


@pytest.mark.parametrize("before,after", [(PRODUCTION, FEE), (FEE, PAYROLL), (PAYROLL, CORRECTION)])
def test_restore_rejects_future_functions_in_an_older_backup(before: str, after: str) -> None:
    with pytest.raises(backup.BackupError):
        backup._validate_company_transaction_classification_security(
            _stage_metadata(after), revision=before
        )


@pytest.mark.parametrize(
    "revision,function,role",
    [
        (FEE, V1_COMMAND, "ledgerbridge_api"),
        (FEE, V1_SUMMARY, "ledgerbridge_reader"),
        (PAYROLL, PAYROLL_READER, "ledgerbridge_api"),
    ],
)
def test_restore_rejects_retired_or_cross_role_execute(
    revision: str, function: tuple[str, str], role: str
) -> None:
    metadata = _stage_metadata(revision)
    privileges = _rows(metadata, "company_transaction_classification_effective_function_privileges")
    observed = next(row for row in privileges if _key(row) == function and row["role"] == role)
    observed["execute"] = True
    with pytest.raises(backup.BackupError):
        backup._validate_company_transaction_classification_security(metadata, revision=revision)


@pytest.mark.parametrize(
    "role", ["ledgerbridge_worker", "ledgerbridge_api", "ledgerbridge_reader", "ledgerbridge_app"]
)
def test_correction_function_remains_owner_only(role: str) -> None:
    metadata = _stage_metadata(CORRECTION)
    privileges = _rows(metadata, "company_transaction_classification_effective_function_privileges")
    observed = next(
        row for row in privileges if _key(row) == CORRECTION_FUNCTION and row["role"] == role
    )
    observed["execute"] = True
    with pytest.raises(backup.BackupError):
        backup._validate_company_transaction_classification_security(metadata, revision=CORRECTION)
