from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

import ledgerbridge.internal_read_audit as audit_module
from ledgerbridge.config import Settings
from ledgerbridge.internal_read_audit import (
    AuditSinkUnavailable,
    DatabaseInternalReadAuditSink,
    DatabaseInternalReadReceiptSink,
    EvidenceReadAuditEvent,
    EvidenceReadReceipt,
    UnavailableInternalReadAuditSink,
    get_internal_read_audit_sink,
)


class _Session(AbstractContextManager["_Session"]):
    def __init__(self) -> None:
        self.committed = False
        self.params: dict[str, object] = {}
        self.statement: object | None = None

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        self.committed = True

    def execute(self, _statement: object, params: dict[str, object]) -> _Result:
        self.statement = _statement
        self.params = params
        return _Result()


class _Result:
    def scalar_one(self) -> UUID:
        return UUID("40000000-0000-4000-8000-000000000002")


def _event() -> EvidenceReadAuditEvent:
    return EvidenceReadAuditEvent(
        principal_ref="workload:r1-audit-test",
        principal_san_uri="spiffe://ledgerbridge.test/r1-audit-test",
        policy_generation=11,
        evidence_ref=UUID("20000000-0000-4000-8000-000000000001"),
        entity_ref=UUID("10000000-0000-4000-8000-000000000001"),
        business_unit_ref="unit-demo-a",
        byte_size=16,
        sha256="a" * 64,
    )


def _receipt() -> EvidenceReadReceipt:
    return EvidenceReadReceipt(
        principal_ref="workload:r1-audit-test",
        principal_san_uri="spiffe://ledgerbridge.test/r1-audit-test",
        policy_generation="policy-11",
        evidence_ref=UUID("20000000-0000-4000-8000-000000000001"),
        entity_ref=UUID("10000000-0000-4000-8000-000000000001"),
        business_unit_id=UUID("11000000-0000-4000-8000-000000000001"),
        blob_ref=UUID("30000000-0000-4000-8000-000000000001"),
        byte_size=16,
        sha256="a" * 64,
    )


def test_database_sink_appends_allowlisted_payload_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    captured: dict[str, Any] = {}

    def append(_session: object, **kwargs: object) -> UUID:
        captured.update(kwargs)
        return UUID("40000000-0000-4000-8000-000000000001")

    monkeypatch.setattr(audit_module, "append_audit_event", append)
    factory = cast(Callable[[], Session], lambda: session)
    DatabaseInternalReadAuditSink(factory).append(_event())

    assert session.committed is True
    assert captured["actor"] == "workload:r1-audit-test"
    assert captured["action"] == "internal.read.evidence.content"
    assert captured["reason"] == "internal evidence content read"
    assert captured["rule_version"] == "ledgerbridge.internal-read-audit.v1"
    assert captured["payload"] == {
        "event_type": "EVIDENCE_CONTENT_READ",
        "principal_san_uri": "spiffe://ledgerbridge.test/r1-audit-test",
        "policy_generation": 11,
        "evidence_ref": "20000000-0000-4000-8000-000000000001",
        "entity_ref": "10000000-0000-4000-8000-000000000001",
        "business_unit_ref": "unit-demo-a",
        "byte_size": 16,
        "sha256": "a" * 64,
        "outcome": "SUCCEEDED",
    }


def test_database_sink_maps_database_failure_to_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> UUID:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(audit_module, "append_audit_event", fail)
    with pytest.raises(AuditSinkUnavailable, match="append failed"):
        factory = cast(Callable[[], Session], _Session)
        DatabaseInternalReadAuditSink(factory).append(_event())


def test_database_receipt_sink_calls_allowlisted_function_and_commits() -> None:
    session = _Session()
    factory = cast(Callable[[], Session], lambda: session)
    DatabaseInternalReadReceiptSink(factory).append(_receipt())

    assert session.committed is True
    assert session.params["principal_ref"] == "workload:r1-audit-test"
    assert session.params["policy_generation"] == "policy-11"
    assert ":policy_generation" in str(session.statement)
    assert ":key_generation" not in str(session.statement)
    assert session.params["blob_ref"] == UUID("30000000-0000-4000-8000-000000000001")
    assert session.params["sha256"] == bytes.fromhex("aa" * 32)


def test_database_receipt_sink_maps_failure_to_fail_closed() -> None:
    class FailingSession(_Session):
        def execute(self, _statement: object, _params: dict[str, object]) -> _Result:
            raise RuntimeError("database unavailable")

    with pytest.raises(AuditSinkUnavailable, match="receipt append failed"):
        DatabaseInternalReadReceiptSink(cast(Callable[[], Session], FailingSession)).append(
            _receipt()
        )


def test_audit_dependency_stays_unavailable_without_explicit_test_gate(tmp_path: Path) -> None:
    disabled = Settings(
        env="test",
        runtime_role="migrate",
        database_url="sqlite+pysqlite:///:memory:",
        artifact_root=tmp_path.resolve(),
    )
    assert isinstance(get_internal_read_audit_sink(disabled), UnavailableInternalReadAuditSink)

    enabled = disabled.model_copy(
        update={
            "enable_internal_read_api": True,
            "enable_internal_read_persistent_audit": True,
            "internal_read_policy_generation": 11,
        }
    )
    assert isinstance(get_internal_read_audit_sink(enabled), DatabaseInternalReadAuditSink)

    with pytest.raises(ValueError, match="production internal read persistent audit"):
        Settings(
            env="production",
            runtime_role="api",
            api_database_url="postgresql://ledgerbridge_api@db/app",
            artifact_root=tmp_path.resolve(),
            enable_internal_read_persistent_audit=True,
        )
