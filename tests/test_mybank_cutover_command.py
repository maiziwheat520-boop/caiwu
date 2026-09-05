from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from ledgerbridge.models import EntityType
from ledgerbridge.mybank_cutover_command import (
    MYBANK_CUTOVER_PLAN_SCHEMA,
    MYBANK_EXISTING_ACCOUNT_PLAN_SCHEMA,
    MyBankCutoverCommandError,
    load_private_mybank_cutover_plan,
    run_mybank_cutover_command,
)
from ledgerbridge.mybank_cutover_plan_builder import (
    MYBANK_CUTOVER_DRAFT_SCHEMA,
    MYBANK_EXISTING_ACCOUNT_DRAFT_SCHEMA,
    MyBankCutoverPlanBuildError,
    finalize_private_mybank_cutover_plan,
    run_mybank_cutover_plan_builder,
)
from ledgerbridge.mybank_statement_cutover import (
    MyBankEvidenceMode,
    MyBankExistingAccountStatementPlan,
    MyBankStatementCutoverPlan,
    MyBankStatementCutoverReceipt,
    ProductionCounts,
)

ENTITY_REF = UUID("84000000-0000-4000-8000-000000000001")
BUSINESS_UNIT_REF = UUID("84000000-0000-4000-8000-000000000002")
EVIDENCE_REF = UUID("84000000-0000-4000-8000-000000000003")
ACCOUNT_REF = UUID("84000000-0000-4000-8000-000000000004")
OPERATION_REF = UUID("84000000-0000-4000-8000-000000000005")
ALIAS_REF = UUID("84000000-0000-4000-8000-000000000006")


def _synthetic_plan(tmp_path: Path) -> dict[str, Any]:
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


def _write_synthetic_mybank_xlsx(path: Path, *, empty: bool = False) -> bytes:
    from tests.test_mybank_statement import _write_synthetic_mybank_xlsx as write_fixture

    return write_fixture(path, empty=empty)


def _synthetic_draft(tmp_path: Path) -> dict[str, Any]:
    payload = _synthetic_plan(tmp_path)
    payload["schema_version"] = MYBANK_CUTOVER_DRAFT_SCHEMA
    payload["scope"]["owner_kind"] = "COMPANY"
    payload["source"] = {
        "path": str((tmp_path / "synthetic-statement.xlsx").resolve()),
        "account_suffix": "7968",
    }
    return payload


def _synthetic_existing_account_plan(tmp_path: Path) -> dict[str, Any]:
    payload = _synthetic_plan(tmp_path)
    payload["schema_version"] = MYBANK_EXISTING_ACCOUNT_PLAN_SCHEMA
    payload["scope"]["owner_kind"] = "COMPANY"
    payload["scope"]["evidence_mode"] = "CREATE_NEW"
    payload["account"] = {"managed_account_ref": str(ACCOUNT_REF)}
    payload.pop("principal")
    return payload


def _synthetic_existing_account_draft(tmp_path: Path) -> dict[str, Any]:
    payload = _synthetic_existing_account_plan(tmp_path)
    payload["schema_version"] = MYBANK_EXISTING_ACCOUNT_DRAFT_SCHEMA
    payload["source"] = {
        "path": str((tmp_path / "synthetic-statement.xlsx").resolve()),
        "account_suffix": "7968",
    }
    return payload


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


def _existing_account_receipt() -> MyBankStatementCutoverReceipt:
    before = replace(
        _counts(imported=False),
        managed_accounts=5,
        managed_account_lifecycles=5,
        account_registry_operations=5,
        managed_account_aliases=5,
        account_business_unit_assignments=5,
    )
    after = replace(
        before,
        evidence_objects=1,
        encrypted_object_identities=1,
        encrypted_blob_versions=1,
        bank_statements=1,
        bank_statement_transactions=2,
        bank_statement_observations=2,
        bank_statement_reviews=1,
        audit_events=8,
    )
    return MyBankStatementCutoverReceipt(
        statement_ref=UUID("84000000-0000-4000-8000-000000000011"),
        evidence_ref=EVIDENCE_REF,
        managed_account_ref=ACCOUNT_REF,
        registry_created=False,
        created=True,
        registry_replay_created=False,
        replay_created=False,
        transaction_count=2,
        review_status="PENDING",
        before_counts=before,
        after_counts=after,
        replay_counts=after,
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
    assert isinstance(loaded.cutover, MyBankStatementCutoverPlan)
    assert loaded.cutover.registry_plan.accounts[0].aliases[0].alias_value.endswith("1357")
    assert loaded.principal is not None
    assert loaded.principal.grants[0].entity_ref == ENTITY_REF
    assert loaded.principal.grants[0].allow_account_registry is True
    assert loaded.safety_proof.restore_report.parent == loaded.safety_proof.backup_directory


def test_existing_account_plan_loads_without_account_registration_payload(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "existing-account-plan.json").resolve()
    _write_private_plan(path, _synthetic_existing_account_plan(tmp_path))

    loaded = load_private_mybank_cutover_plan(path)

    assert isinstance(loaded.cutover, MyBankExistingAccountStatementPlan)
    assert loaded.cutover.entity_ref == ENTITY_REF
    assert loaded.cutover.business_unit_ref == BUSINESS_UNIT_REF
    assert loaded.cutover.managed_account_ref == ACCOUNT_REF
    assert loaded.cutover.account_suffix == "1357"
    assert loaded.cutover.expected_transaction_count == 2
    assert loaded.cutover.evidence_mode is MyBankEvidenceMode.CREATE_NEW
    assert loaded.cutover.owner_kind is EntityType.COMPANY
    assert loaded.principal is None
    payload = _synthetic_existing_account_plan(tmp_path)
    assert set(payload["account"]) == {"managed_account_ref"}


def test_private_draft_is_finalized_from_verified_source_without_guessing_metadata(
    tmp_path: Path,
) -> None:
    source_path = (tmp_path / "synthetic-statement.xlsx").resolve()
    source = _write_synthetic_mybank_xlsx(source_path)
    draft_path = (tmp_path / "cutover-draft.json").resolve()
    plan_path = (tmp_path / "cutover-plan.json").resolve()
    _write_private_plan(draft_path, _synthetic_draft(tmp_path))

    finalized = finalize_private_mybank_cutover_plan(draft_path, plan_path)

    assert finalized == plan_path
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == MYBANK_CUTOVER_PLAN_SCHEMA
    assert payload["source"] == {
        "path": str(source_path),
        "sha256": __import__("hashlib").sha256(source).hexdigest(),
        "size": len(source),
        "account_suffix": "7968",
        "transaction_count": 2,
    }
    loaded = load_private_mybank_cutover_plan(plan_path)
    assert loaded.cutover.owner_kind is EntityType.COMPANY
    assert isinstance(loaded.cutover, MyBankStatementCutoverPlan)
    assert loaded.cutover.registry_plan.owner_entity_ref == ENTITY_REF


def test_existing_account_draft_binds_digest_size_and_nonempty_row_count(
    tmp_path: Path,
) -> None:
    source_path = (tmp_path / "synthetic-statement.xlsx").resolve()
    source = _write_synthetic_mybank_xlsx(source_path)
    draft_path = (tmp_path / "existing-account-draft.json").resolve()
    plan_path = (tmp_path / "existing-account-plan.json").resolve()
    _write_private_plan(draft_path, _synthetic_existing_account_draft(tmp_path))

    finalize_private_mybank_cutover_plan(draft_path, plan_path)

    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == MYBANK_EXISTING_ACCOUNT_PLAN_SCHEMA
    assert payload["source"] == {
        "path": str(source_path),
        "sha256": __import__("hashlib").sha256(source).hexdigest(),
        "size": len(source),
        "account_suffix": "7968",
        "transaction_count": 2,
    }
    assert payload["account"] == {"managed_account_ref": str(ACCOUNT_REF)}
    assert payload["scope"]["evidence_mode"] == "CREATE_NEW"
    assert "principal" not in payload


@pytest.mark.parametrize("mode", (None, "AUTO", "reuse_existing"))
def test_existing_account_plan_requires_explicit_supported_evidence_mode(
    tmp_path: Path,
    mode: str | None,
) -> None:
    payload = _synthetic_existing_account_plan(tmp_path)
    if mode is None:
        del payload["scope"]["evidence_mode"]
    else:
        payload["scope"]["evidence_mode"] = mode
    path = (tmp_path / "invalid-existing-account-plan.json").resolve()
    _write_private_plan(path, payload)

    with pytest.raises(MyBankCutoverCommandError, match="private plan"):
        load_private_mybank_cutover_plan(path)


def test_private_draft_wrong_account_mapping_publishes_no_plan(tmp_path: Path) -> None:
    source_path = (tmp_path / "synthetic-statement.xlsx").resolve()
    _write_synthetic_mybank_xlsx(source_path)
    draft = _synthetic_draft(tmp_path)
    draft["source"]["account_suffix"] = "0000"
    draft_path = (tmp_path / "cutover-draft.json").resolve()
    plan_path = (tmp_path / "cutover-plan.json").resolve()
    _write_private_plan(draft_path, draft)

    with pytest.raises(MyBankCutoverPlanBuildError, match="could not be finalized"):
        finalize_private_mybank_cutover_plan(draft_path, plan_path)

    assert not plan_path.exists()


def test_private_plan_builder_command_prints_no_source_or_alias_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_path = (tmp_path / "synthetic-statement.xlsx").resolve()
    _write_synthetic_mybank_xlsx(source_path)
    draft = _synthetic_draft(tmp_path)
    draft_path = (tmp_path / "cutover-draft.json").resolve()
    plan_path = (tmp_path / "cutover-plan.json").resolve()
    _write_private_plan(draft_path, draft)

    result = run_mybank_cutover_plan_builder(
        {
            "LEDGERBRIDGE_MYBANK_PRIVATE_DRAFT": str(draft_path),
            "LEDGERBRIDGE_MYBANK_PRIVATE_PLAN": str(plan_path),
        }
    )

    assert result == 0
    rendered = capsys.readouterr().out
    assert rendered.strip() == "MYBANK_CUTOVER_PLAN_READY"
    assert str(source_path) not in rendered
    assert draft["account"]["aliases"][0]["alias_value"] not in rendered


def test_existing_account_builder_explicitly_skips_empty_statement(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_path = (tmp_path / "synthetic-statement.xlsx").resolve()
    _write_synthetic_mybank_xlsx(source_path, empty=True)
    draft_path = (tmp_path / "existing-account-draft.json").resolve()
    plan_path = (tmp_path / "existing-account-plan.json").resolve()
    _write_private_plan(draft_path, _synthetic_existing_account_draft(tmp_path))

    result = run_mybank_cutover_plan_builder(
        {
            "LEDGERBRIDGE_MYBANK_PRIVATE_DRAFT": str(draft_path),
            "LEDGERBRIDGE_MYBANK_PRIVATE_PLAN": str(plan_path),
        }
    )

    assert result == 0
    assert not plan_path.exists()
    assert capsys.readouterr().out.strip() == "MYBANK_CUTOVER_EMPTY_STATEMENT_SKIPPED"


def test_preflight_writes_private_bound_receipt_without_disclosing_plan_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path = (tmp_path / "cutover-plan.json").resolve()
    receipt_path = (tmp_path / "preflight-receipt.json").resolve()
    payload = _synthetic_plan(tmp_path)
    _write_private_plan(plan_path, payload)
    calls: list[tuple[str, bool]] = []

    def execute(
        _loaded: object, database_url: str, *, commit: bool
    ) -> MyBankStatementCutoverReceipt:
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
    assert payload["source"]["path"] not in rendered
    assert payload["account"]["aliases"][0]["alias_value"] not in rendered


def test_existing_account_plan_uses_same_rollback_only_command_gate(
    tmp_path: Path,
) -> None:
    plan_path = (tmp_path / "existing-account-plan.json").resolve()
    receipt_path = (tmp_path / "existing-account-preflight.json").resolve()
    _write_private_plan(plan_path, _synthetic_existing_account_plan(tmp_path))
    observed: list[tuple[type[object], bool]] = []

    def execute(
        loaded: object, _database_url: str, *, commit: bool
    ) -> MyBankStatementCutoverReceipt:
        observed.append((type(loaded.cutover), commit))  # type: ignore[attr-defined]
        return _existing_account_receipt()

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
    assert observed == [(MyBankExistingAccountStatementPlan, False)]


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

    def execute(
        _loaded: object, database_url: str, *, commit: bool
    ) -> MyBankStatementCutoverReceipt:
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
