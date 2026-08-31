from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID

import pytest

from ledgerbridge.models import EntityType
from ledgerbridge.mybank_cutover_command import (
    MYBANK_CUTOVER_PLAN_SCHEMA,
    MyBankCutoverCommandError,
    load_private_mybank_cutover_plan,
    run_mybank_cutover_command,
)
from ledgerbridge.mybank_statement_cutover import (
    MyBankStatementCutoverReceipt,
    ProductionCounts,
)

ENTITY_REF = UUID("84000000-0000-4000-8000-000000000001")
BUSINESS_UNIT_REF = UUID("84000000-0000-4000-8000-000000000002")
EVIDENCE_REF = UUID("84000000-0000-4000-8000-000000000003")
ACCOUNT_REF = UUID("84000000-0000-4000-8000-000000000004")
OPERATION_REF = UUID("84000000-0000-4000-8000-000000000005")
ALIAS_REF = UUID("84000000-0000-4000-8000-000000000006")


def _synthetic_plan(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": MYBANK_CUTOVER_PLAN_SCHEMA,
        "target_revision": "a" * 40,
        "source": {
            "path": str((tmp_path / "synthetic-statement.xlsx").resolve()),
            "sha256": "b" * 64,
            "size": 1234,
            "account_suffix": "1357",
            "transaction_count": 2,
        },
        "scope": {
            "evidence_ref": str(EVIDENCE_REF),
            "owner_entity_ref": str(ENTITY_REF),
            "business_unit_ref": str(BUSINESS_UNIT_REF),
            "owner_kind": "PERSON",
        },
        "account": {
            "operation_id": str(OPERATION_REF),
            "expected_registry_revision": 0,
            "managed_account_ref": str(ACCOUNT_REF),
            "account_key": "managed-account:synthetic-personal",
            "account_kind": "BANK_CHECKING",
            "aliases": [
                {
                    "alias_ref": str(ALIAS_REF),
                    "alias_kind": "ACCOUNT_NUMBER",
                    "alias_value": "0000 0000 0000 1357",
                }
            ],
            "business_unit_assignment": None,
        },
        "principal": {
            "principal_ref": "workload:synthetic-cutover",
            "san_uri": "spiffe://ledgerbridge.test/synthetic-cutover",
            "policy_generation": 1,
        },
        "audit": {
            "actor": "worker:synthetic-cutover",
            "reason": "operator-confirmed synthetic whole-statement import",
        },
        "safety": {
            "backup_directory": str((tmp_path / "synthetic-backup").resolve()),
            "restore_report": str(
                (tmp_path / "synthetic-backup" / "restore-rehearsal-synthetic.json").resolve()
            ),
            "key_file": str((tmp_path / "synthetic-key.json").resolve()),
            "artifact_root": str((tmp_path / "synthetic-artifacts").resolve()),
        },
    }


def _write_private_plan(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def _counts(*, imported: bool) -> ProductionCounts:
    return ProductionCounts(
        evidence_objects=1 if imported else 0,
        encrypted_object_identities=1 if imported else 0,
        encrypted_blob_versions=1 if imported else 0,
        managed_accounts=1 if imported else 0,
        managed_account_lifecycles=1 if imported else 0,
        account_registry_operations=1 if imported else 0,
        managed_account_aliases=1 if imported else 0,
        account_business_unit_assignments=0,
        fact_business_unit_allocation_sets=0,
        fact_business_unit_allocation_items=0,
        bank_statements=1 if imported else 0,
        bank_statement_transactions=2 if imported else 0,
        bank_statement_observations=2 if imported else 0,
        bank_statement_reviews=1 if imported else 0,
        candidates=7,
        latest_pending_candidates=3,
        audit_events=12 if imported else 0,
    )


def _receipt() -> MyBankStatementCutoverReceipt:
    return MyBankStatementCutoverReceipt(
        statement_ref=UUID("84000000-0000-4000-8000-000000000011"),
        evidence_ref=EVIDENCE_REF,
        managed_account_ref=ACCOUNT_REF,
        registry_created=True,
        created=True,
        registry_replay_created=False,
        replay_created=False,
        transaction_count=2,
        review_status="PENDING",
        before_counts=_counts(imported=False),
        after_counts=_counts(imported=True),
        replay_counts=_counts(imported=True),
        fact_conflict_rejected=True,
    )


def test_private_plan_loads_one_explicit_owner_account_mapping(tmp_path: Path) -> None:
    path = (tmp_path / "cutover-plan.json").resolve()
    _write_private_plan(path, _synthetic_plan(tmp_path))

    loaded = load_private_mybank_cutover_plan(path)

    assert loaded.target_revision == "a" * 40
    assert loaded.cutover.entity_ref == ENTITY_REF
    assert loaded.cutover.business_unit_ref == BUSINESS_UNIT_REF
    assert loaded.cutover.owner_kind is EntityType.PERSON
    assert loaded.cutover.managed_account_ref == ACCOUNT_REF
    assert loaded.cutover.registry_plan.accounts[0].aliases[0].alias_value.endswith("1357")
    assert loaded.principal.grants[0].entity_ref == ENTITY_REF
    assert loaded.principal.grants[0].allow_account_registry is True
    assert loaded.safety_proof.restore_report.parent == loaded.safety_proof.backup_directory


def test_preflight_writes_private_bound_receipt_without_disclosing_plan_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path = (tmp_path / "cutover-plan.json").resolve()
    receipt_path = (tmp_path / "preflight-receipt.json").resolve()
    payload = _synthetic_plan(tmp_path)
    _write_private_plan(plan_path, payload)
    calls: list[tuple[str, bool]] = []

    def execute(_loaded: object, database_url: str, *, commit: bool) -> object:
        calls.append((database_url, commit))
        return _receipt()

    result = run_mybank_cutover_command(
        ["--preflight-only"],
        environ={
            "LEDGERBRIDGE_ENV": "test",
            "LEDGERBRIDGE_MYBANK_DATABASE_TARGET": "isolated",
            "LEDGERBRIDGE_MYBANK_DATABASE_URL": "postgresql://synthetic-isolated",
            "LEDGERBRIDGE_MYBANK_PRIVATE_PLAN": str(plan_path),
            "LEDGERBRIDGE_MYBANK_PREFLIGHT_RECEIPT": str(receipt_path),
            "LEDGERBRIDGE_DEPLOYED_REVISION": "a" * 40,
        },
        executor=execute,
    )

    assert result == 0
    assert calls == [("postgresql://synthetic-isolated", False)]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert set(receipt) == {
        "schema_version",
        "plan_sha256",
        "target_revision",
        "transaction_count",
        "candidate_delta",
        "replay_zero_delta",
        "fact_conflict_rejected",
    }
    rendered = capsys.readouterr().out
    assert "MYBANK_CUTOVER_PREFLIGHT_OK" in rendered
    assert payload["source"]["path"] not in rendered  # type: ignore[index]
    assert payload["account"]["aliases"][0]["alias_value"] not in rendered  # type: ignore[index]


def test_production_execution_requires_explicit_gate_and_bound_preflight(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path = (tmp_path / "cutover-plan.json").resolve()
    receipt_path = (tmp_path / "preflight-receipt.json").resolve()
    _write_private_plan(plan_path, _synthetic_plan(tmp_path))
    isolated_environment = {
        "LEDGERBRIDGE_ENV": "test",
        "LEDGERBRIDGE_MYBANK_DATABASE_TARGET": "isolated",
        "LEDGERBRIDGE_MYBANK_DATABASE_URL": "postgresql://synthetic-isolated",
        "LEDGERBRIDGE_MYBANK_PRIVATE_PLAN": str(plan_path),
        "LEDGERBRIDGE_MYBANK_PREFLIGHT_RECEIPT": str(receipt_path),
        "LEDGERBRIDGE_DEPLOYED_REVISION": "a" * 40,
    }
    run_mybank_cutover_command(
        ["--preflight-only"],
        environ=isolated_environment,
        executor=lambda *_args, **_kwargs: _receipt(),
    )
    capsys.readouterr()
    production_environment = {
        **isolated_environment,
        "LEDGERBRIDGE_ENV": "production",
        "LEDGERBRIDGE_MYBANK_DATABASE_TARGET": "production",
        "LEDGERBRIDGE_MYBANK_DATABASE_URL": "postgresql://synthetic-production",
    }
    calls: list[tuple[str, bool]] = []

    def execute(_loaded: object, database_url: str, *, commit: bool) -> object:
        calls.append((database_url, commit))
        return _receipt()

    with pytest.raises(MyBankCutoverCommandError, match="production execution gate"):
        run_mybank_cutover_command(
            ["--execute-production"],
            environ=production_environment,
            executor=execute,
        )
    assert calls == []

    production_environment["LEDGERBRIDGE_MYBANK_PRODUCTION_EXECUTION"] = (
        "execute-reviewed-cutover-v1"
    )
    result = run_mybank_cutover_command(
        ["--execute-production"],
        environ=production_environment,
        executor=execute,
    )

    assert result == 0
    assert calls == [("postgresql://synthetic-production", True)]
    assert "MYBANK_CUTOVER_PRODUCTION_OK" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="private file modes are a POSIX contract")
def test_private_plan_rejects_group_readable_permissions(tmp_path: Path) -> None:
    path = (tmp_path / "cutover-plan.json").resolve()
    _write_private_plan(path, _synthetic_plan(tmp_path))
    path.chmod(0o640)

    with pytest.raises(MyBankCutoverCommandError, match="private plan is unavailable"):
        load_private_mybank_cutover_plan(path)
