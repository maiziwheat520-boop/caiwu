from __future__ import annotations

import hashlib
import io
import json
import os
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
import yaml

import ledgerbridge.account_registry_intake as intake_module
from ledgerbridge.account_registry_intake import (
    ACCOUNT_REGISTRY_INTAKE_PLAN_SCHEMA,
    AccountRegistryIntakeError,
    AccountRegistryIntakeInventory,
    AccountRegistryIntakeReceipt,
    _ensure_exact_evidence,
    _ensure_initial_lifecycle,
    _require_database_owner_target,
    _require_exact_replay,
    load_private_account_registry_intake,
    run_transactional_account_registry_intake,
)
from ledgerbridge.account_registry_intake_command import (
    AccountRegistryIntakeCommandError,
    run_account_registry_intake_command,
)
from ledgerbridge.models import EntityType

ENTITY_REF = UUID("85000000-0000-4000-8000-000000000001")
BUSINESS_UNIT_REF = UUID("85000000-0000-4000-8000-000000000002")
EVIDENCE_REF = UUID("85000000-0000-4000-8000-000000000003")
ACCOUNT_REF = UUID("85000000-0000-4000-8000-000000000004")
OPERATION_REF = UUID("85000000-0000-4000-8000-000000000005")
ALIAS_REF = UUID("85000000-0000-4000-8000-000000000006")
ASSIGNMENT_REF = UUID("85000000-0000-4000-8000-000000000007")
AUDIT_REF = UUID("85000000-0000-4000-8000-000000000008")


def _plan(tmp_path: Path, *, lifecycle: str = "CLOSED") -> dict[str, object]:
    source_path = (tmp_path / "synthetic-statement.pdf").resolve()
    source = b"synthetic account admission evidence"
    source_path.write_bytes(source)
    return {
        "schema_version": ACCOUNT_REGISTRY_INTAKE_PLAN_SCHEMA,
        "target_revision": "a" * 40,
        "entity": {
            "entity_ref": str(ENTITY_REF),
            "entity_type": "COMPANY",
            "name": "Synthetic Company",
        },
        "business_unit": {
            "business_unit_ref": str(BUSINESS_UNIT_REF),
            "ref": "synthetic-company",
            "label": "Synthetic Company",
        },
        "evidence": {
            "evidence_ref": str(EVIDENCE_REF),
            "source_path": str(source_path),
            "display_name": "synthetic-statement.pdf",
            "declared_media_type": "application/pdf",
            "plaintext_sha256": hashlib.sha256(source).hexdigest(),
            "plaintext_size": len(source),
        },
        "account": {
            "operation_id": str(OPERATION_REF),
            "expected_registry_revision": 0,
            "managed_account_ref": str(ACCOUNT_REF),
            "account_key": "synthetic_bank:company:1234",
            "institution_code": "synthetic_bank",
            "account_suffix": "1234",
            "account_kind": "BANK_CHECKING",
            "initial_lifecycle": lifecycle,
            "aliases": [
                {
                    "alias_ref": str(ALIAS_REF),
                    "alias_kind": "ACCOUNT_NUMBER",
                    "alias_value": "0000 0000 0000 1234",
                }
            ],
            "business_unit_assignment": {
                "assignment_ref": str(ASSIGNMENT_REF),
                "effective_from": "2025-01-01",
                "effective_to": None,
            },
        },
        "audit": {
            "actor_ref": "operator:synthetic-account-intake",
            "reason": "operator-confirmed synthetic account admission",
        },
        "storage": {
            "key_file": str((tmp_path / "key.json").resolve()),
            "artifact_root": str((tmp_path / "artifacts").resolve()),
        },
    }


def _write_private(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def _receipt(loaded: Any) -> AccountRegistryIntakeReceipt:
    plan = loaded.plan
    return AccountRegistryIntakeReceipt(
        plan_sha256=loaded.plan_sha256,
        operation_id=plan.account.operation_id,
        owner_entity_ref=plan.entity.entity_ref,
        business_unit_ref=plan.business_unit.business_unit_ref,
        evidence_ref=plan.evidence.evidence_ref,
        managed_account_ref=plan.account.managed_account_ref,
        registry_revision=plan.account.expected_registry_revision + 1,
        lifecycle_revision=2,
        lifecycle_status="CLOSED",
        entity_created=True,
        business_unit_created=True,
        evidence_created=True,
        registry_created=True,
    )


def test_private_plan_binds_one_exact_owner_evidence_account(tmp_path: Path) -> None:
    plan_path = (tmp_path / "account-intake.json").resolve()
    payload = _plan(tmp_path)
    _write_private(plan_path, payload)

    loaded = load_private_account_registry_intake(plan_path)

    assert loaded.plan.entity.entity_type is EntityType.COMPANY
    assert loaded.registry_plan.owner_entity_ref == ENTITY_REF
    assert loaded.registry_plan.accounts[0].admission_evidence_ref == EVIDENCE_REF
    assert loaded.registry_plan.accounts[0].institution_code == "synthetic_bank"
    assert loaded.registry_plan.business_unit_assignments[0].business_unit_id == BUSINESS_UNIT_REF
    assert loaded.principal.principal_ref == "workload:account-registry-intake"
    assert loaded.principal.grants[0].allow_account_registry is True
    assert (
        loaded.plan_sha256
        == hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
    )


def test_private_plan_rejects_relative_source_path(tmp_path: Path) -> None:
    payload = _plan(tmp_path)
    payload["evidence"]["source_path"] = "relative.pdf"  # type: ignore[index]
    plan_path = (tmp_path / "account-intake.json").resolve()
    _write_private(plan_path, payload)

    with pytest.raises(AccountRegistryIntakeError, match="unavailable or invalid"):
        load_private_account_registry_intake(plan_path)


def test_private_plan_preserves_nonzero_owner_registry_revision(tmp_path: Path) -> None:
    payload = _plan(tmp_path)
    payload["account"]["expected_registry_revision"] = 4  # type: ignore[index]
    plan_path = (tmp_path / "account-intake.json").resolve()
    _write_private(plan_path, payload)

    loaded = load_private_account_registry_intake(plan_path)

    assert loaded.registry_plan.expected_registry_revision == 4


class _Result:
    def __init__(self, *, rows: list[dict[str, object]] | None = None, scalar: object = None):
        self._rows = rows or []
        self._scalar = scalar

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict[str, object]]:
        return self._rows

    def one(self) -> dict[str, object]:
        if len(self._rows) != 1:
            raise RuntimeError("expected one row")
        return self._rows[0]

    def scalar_one(self) -> object:
        return self._scalar


class _LifecycleSession:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, statement: object, parameters: dict[str, object]) -> _Result:
        rendered = str(statement)
        self.calls.append((rendered, parameters))
        if "SELECT revision, status" in rendered:
            return _Result(rows=self.rows)
        if "append_audit_event" in rendered:
            return _Result(scalar=AUDIT_REF)
        return _Result()


def test_closed_initial_lifecycle_uses_audit_bound_append_only_revision(tmp_path: Path) -> None:
    plan_path = (tmp_path / "account-intake.json").resolve()
    _write_private(plan_path, _plan(tmp_path))
    loaded = load_private_account_registry_intake(plan_path)
    session = _LifecycleSession([{"revision": 1, "status": "ACTIVE"}])

    revision, status = _ensure_initial_lifecycle(  # type: ignore[arg-type]
        session,
        loaded.plan,
        registry_created=True,
    )

    assert (revision, status) == (2, "CLOSED")
    audit_call = next(call for call in session.calls if "append_audit_event" in call[0])
    assert audit_call[1]["action"] == "managed_account.lifecycle"
    assert audit_call[1]["rule_version"] == "ledgerbridge.bank-statement.v1"
    assert any("INSERT INTO public.managed_account_lifecycle" in call[0] for call in session.calls)
    assert not any("UPDATE public.managed_account_lifecycle" in call[0] for call in session.calls)


def test_lifecycle_replay_requires_exact_initial_history(tmp_path: Path) -> None:
    plan_path = (tmp_path / "account-intake.json").resolve()
    _write_private(plan_path, _plan(tmp_path))
    loaded = load_private_account_registry_intake(plan_path)
    exact = _LifecycleSession(
        [
            {"revision": 1, "status": "ACTIVE"},
            {"revision": 2, "status": "CLOSED"},
        ]
    )

    assert _ensure_initial_lifecycle(  # type: ignore[arg-type]
        exact,
        loaded.plan,
        registry_created=False,
    ) == (2, "CLOSED")
    assert len(exact.calls) == 1

    conflicting = _LifecycleSession([{"revision": 1, "status": "ACTIVE"}])
    with pytest.raises(AccountRegistryIntakeError, match="replay conflicts"):
        _ensure_initial_lifecycle(  # type: ignore[arg-type]
            conflicting,
            loaded.plan,
            registry_created=False,
        )

    later_change = _LifecycleSession(
        [
            {"revision": 1, "status": "ACTIVE"},
            {"revision": 2, "status": "CLOSED"},
            {"revision": 3, "status": "INACTIVE"},
        ]
    )
    assert _ensure_initial_lifecycle(  # type: ignore[arg-type]
        later_change,
        loaded.plan,
        registry_created=False,
    ) == (2, "CLOSED")


def test_database_owner_target_is_one_fixed_assertion() -> None:
    exact = _Result(
        rows=[
            {
                "current_user": "ledgerbridge",
                "session_user": "ledgerbridge",
                "database_name": "ledgerbridge",
                "schema_revision": "20260904_0043",
                "transaction_read_only": "off",
            }
        ]
    )

    class Session:
        def __init__(self, result: _Result) -> None:
            self.result = result
            self.calls = 0

        def execute(self, _statement: object) -> _Result:
            self.calls += 1
            return self.result

    accepted = Session(exact)
    _require_database_owner_target(accepted)  # type: ignore[arg-type]
    assert accepted.calls == 1

    rejected = Session(
        _Result(
            rows=[
                {
                    "current_user": "ledgerbridge_api",
                    "session_user": "ledgerbridge",
                    "database_name": "ledgerbridge",
                    "schema_revision": "20260904_0043",
                    "transaction_read_only": "off",
                }
            ]
        )
    )
    with pytest.raises(AccountRegistryIntakeError, match="database owner target"):
        _require_database_owner_target(rejected)  # type: ignore[arg-type]


def test_exact_replay_requires_four_false_flags_and_zero_delta(tmp_path: Path) -> None:
    plan_path = (tmp_path / "account-intake.json").resolve()
    _write_private(plan_path, _plan(tmp_path))
    loaded = load_private_account_registry_intake(plan_path)
    first = _receipt(loaded)
    replay = replace(
        first,
        entity_created=False,
        business_unit_created=False,
        evidence_created=False,
        registry_created=False,
    )
    inventory = AccountRegistryIntakeInventory(1, 1, 1, 1, 1, 1, 2, 1, 1, 1)

    _require_exact_replay(
        first,
        replay,
        before_inventory=inventory,
        after_inventory=inventory,
        audit_before=8,
        audit_after=8,
    )

    with pytest.raises(AccountRegistryIntakeError, match="changed target inventory"):
        _require_exact_replay(
            first,
            replay,
            before_inventory=inventory,
            after_inventory=replace(inventory, managed_account_aliases=2),
            audit_before=8,
            audit_after=8,
        )


def test_transaction_runs_two_applies_then_forces_deferred_constraints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = (tmp_path / "account-intake.json").resolve()
    _write_private(plan_path, _plan(tmp_path))
    loaded = load_private_account_registry_intake(plan_path)
    first = _receipt(loaded)
    replay = replace(
        first,
        entity_created=False,
        business_unit_created=False,
        evidence_created=False,
        registry_created=False,
    )
    inventory = AccountRegistryIntakeInventory(1, 1, 1, 1, 1, 1, 2, 1, 1, 1)
    applies = iter(((first, None), (replay, None)))
    audit_counts = iter((8, 8))
    statements: list[str] = []

    class Transaction:
        is_active = True
        rolled_back = False

        def commit(self) -> None:
            self.is_active = False

        def rollback(self) -> None:
            self.is_active = False
            self.rolled_back = True

    transaction = Transaction()

    class Connection:
        def begin(self) -> Transaction:
            return transaction

    connection = Connection()

    class Engine:
        @contextmanager
        def connect(self) -> Any:
            yield connection

    class Session:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def execute(self, statement: object) -> _Result:
            statements.append(str(statement))
            return _Result()

        def close(self) -> None:
            pass

    target_checks: list[bool] = []
    monkeypatch.setattr(intake_module, "Session", Session)
    monkeypatch.setattr(intake_module, "_verify_source_file", lambda *_args: None)
    monkeypatch.setattr(intake_module, "_build_evidence_store", lambda *_args: object())
    monkeypatch.setattr(
        intake_module,
        "_require_database_owner_target",
        lambda *_args: target_checks.append(True),
    )
    monkeypatch.setattr(intake_module, "_apply_once", lambda *_args: next(applies))
    monkeypatch.setattr(intake_module, "_read_target_inventory", lambda *_args: inventory)
    monkeypatch.setattr(
        intake_module,
        "_current_transaction_audit_count",
        lambda *_args: next(audit_counts),
    )

    result = run_transactional_account_registry_intake(  # type: ignore[arg-type]
        Engine(),
        loaded,
        commit=False,
    )

    assert result == first
    assert target_checks == [True]
    assert statements == ["SET CONSTRAINTS ALL IMMEDIATE"]
    assert transaction.rolled_back is True


class _EvidenceSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, statement: object, parameters: dict[str, object]) -> _Result:
        self.calls.append((str(statement), parameters))
        return _Result(rows=[])


class _Publication:
    def __init__(self, source: bytes) -> None:
        self.aborted = False
        self.artifact = SimpleNamespace(
            plaintext_sha256=hashlib.sha256(source).digest(),
            plaintext_size=len(source),
        )

    def abort(self) -> None:
        self.aborted = True


class _EvidenceStore:
    def __init__(self, source: bytes) -> None:
        self.publication = _Publication(source)

    def begin_publication(self, _stream: object) -> _Publication:
        return self.publication

    def envelope_metadata(self, _artifact: object) -> object:
        return object()

    @contextmanager
    def open_verified(self, _artifact: object, *, envelope_metadata: object) -> Any:
        del envelope_metadata
        yield io.BytesIO(b"synthetic account admission evidence")


def test_fresh_evidence_failure_aborts_staged_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = (tmp_path / "account-intake.json").resolve()
    _write_private(plan_path, _plan(tmp_path))
    loaded = load_private_account_registry_intake(plan_path)
    source = loaded.source_path.read_bytes()
    session = _EvidenceSession()
    store = _EvidenceStore(source)

    def reject_insert(*_args: object, **_kwargs: object) -> None:
        raise AccountRegistryIntakeError("synthetic persistence rejection")

    monkeypatch.setattr(intake_module, "_insert_fresh_evidence", reject_insert)

    with pytest.raises(AccountRegistryIntakeError, match="synthetic persistence rejection"):
        _ensure_exact_evidence(  # type: ignore[arg-type]
            session,
            store,
            loaded,
        )

    assert store.publication.aborted is True
    evidence_query = next(call for call in session.calls if "evidence_object" in call[0])
    assert set(evidence_query[1]) == {"evidence"}


def test_command_requires_production_rollback_preflight_then_explicit_execution_gate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "DEPLOYED_REVISION").write_text("a" * 40 + "\n", encoding="ascii")
    monkeypatch.chdir(tmp_path)
    plan_path = (tmp_path / "account-intake.json").resolve()
    receipt_path = (tmp_path / "account-intake-preflight.json").resolve()
    _write_private(plan_path, _plan(tmp_path))
    rollback_preflight = {
        "LEDGERBRIDGE_ENV": "production",
        "LEDGERBRIDGE_ACCOUNT_INTAKE_DATABASE_TARGET": "production-rollback-only",
        "LEDGERBRIDGE_ACCOUNT_INTAKE_DATABASE_URL": "postgresql://synthetic-production",
        "LEDGERBRIDGE_ACCOUNT_INTAKE_PRIVATE_PLAN": str(plan_path),
        "LEDGERBRIDGE_ACCOUNT_INTAKE_PREFLIGHT_RECEIPT": str(receipt_path),
    }
    calls: list[tuple[str, bool]] = []

    def execute(loaded: Any, database_url: str, *, commit: bool) -> AccountRegistryIntakeReceipt:
        calls.append((database_url, commit))
        return _receipt(loaded)

    assert (
        run_account_registry_intake_command(
            ["--preflight-only"],
            environ=rollback_preflight,
            executor=execute,
        )
        == 0
    )
    assert calls == [("postgresql://synthetic-production", False)]
    preflight = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert preflight["plan_sha256"]
    assert "ACCOUNT_REGISTRY_INTAKE_PRODUCTION_ROLLBACK_PREFLIGHT_OK" in capsys.readouterr().out

    production = {
        **rollback_preflight,
        "LEDGERBRIDGE_ENV": "production",
        "LEDGERBRIDGE_ACCOUNT_INTAKE_DATABASE_TARGET": "production",
        "LEDGERBRIDGE_ACCOUNT_INTAKE_DATABASE_URL": "postgresql://synthetic-production",
    }
    with pytest.raises(AccountRegistryIntakeCommandError, match="production execution gate"):
        run_account_registry_intake_command(
            ["--execute-production"],
            environ=production,
            executor=execute,
        )
    assert len(calls) == 1

    production["LEDGERBRIDGE_ACCOUNT_INTAKE_PRODUCTION_EXECUTION"] = (
        "execute-reviewed-account-intake-v1"
    )
    assert (
        run_account_registry_intake_command(
            ["--execute-production"],
            environ=production,
            executor=execute,
        )
        == 0
    )
    assert calls[-1] == ("postgresql://synthetic-production", True)
    rendered = capsys.readouterr().out
    assert "ACCOUNT_REGISTRY_INTAKE_PRODUCTION_OK" in rendered
    assert str(ENTITY_REF) not in rendered


def test_production_rejects_receipt_for_changed_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "DEPLOYED_REVISION").write_text("a" * 40 + "\n", encoding="ascii")
    monkeypatch.chdir(tmp_path)
    plan_path = (tmp_path / "account-intake.json").resolve()
    receipt_path = (tmp_path / "account-intake-preflight.json").resolve()
    payload = _plan(tmp_path)
    _write_private(plan_path, payload)
    base = {
        "LEDGERBRIDGE_ENV": "production",
        "LEDGERBRIDGE_ACCOUNT_INTAKE_DATABASE_TARGET": "production-rollback-only",
        "LEDGERBRIDGE_ACCOUNT_INTAKE_DATABASE_URL": "postgresql://synthetic-production",
        "LEDGERBRIDGE_ACCOUNT_INTAKE_PRIVATE_PLAN": str(plan_path),
        "LEDGERBRIDGE_ACCOUNT_INTAKE_PREFLIGHT_RECEIPT": str(receipt_path),
    }
    run_account_registry_intake_command(
        ["--preflight-only"],
        environ=base,
        executor=lambda loaded, *_args, **_kwargs: _receipt(loaded),
    )
    payload["audit"]["reason"] = "a changed reviewed reason"  # type: ignore[index]
    _write_private(plan_path, payload)
    production = {
        **base,
        "LEDGERBRIDGE_ENV": "production",
        "LEDGERBRIDGE_ACCOUNT_INTAKE_DATABASE_TARGET": "production",
        "LEDGERBRIDGE_ACCOUNT_INTAKE_PRODUCTION_EXECUTION": ("execute-reviewed-account-intake-v1"),
    }

    with pytest.raises(AccountRegistryIntakeCommandError, match="preflight receipt"):
        run_account_registry_intake_command(
            ["--execute-production"],
            environ=production,
            executor=lambda loaded, *_args, **_kwargs: _receipt(loaded),
        )


def test_app_image_packages_account_intake_runner() -> None:
    dockerfile = Path("docker/app.Dockerfile").read_text(encoding="utf-8")
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))

    assert (
        "COPY scripts/__init__.py scripts/run_account_registry_intake.py ./scripts/" in dockerfile
    )
    assert "${LEDGERBRIDGE_REVISION}" in dockerfile
    assert "> /app/DEPLOYED_REVISION" in dockerfile
    assert compose["services"]["api"]["build"]["dockerfile"] == "docker/app.Dockerfile"
