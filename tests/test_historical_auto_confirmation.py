from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from ledgerbridge.controlled_import import (
    ImportBusinessUnit,
    ImportCandidate,
    ImportCategory,
    ImportEntity,
    PreparedEvidence,
    PreparedManifest,
    import_prepared_manifest,
)
from ledgerbridge.historical_auto_confirmation import (
    HistoricalAutoConfirmationSettings,
    confirm_existing_test_historical_candidates,
    confirm_test_historical_candidates,
)


class _UnexpectedConnection:
    def execute(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("disabled historical auto-confirmation must not query the database")


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Rows:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def one(self) -> dict[str, Any]:
        assert len(self._rows) == 1
        return self._rows[0]

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class _HistoricalConnection:
    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        self._candidates = candidates
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, statement: object, params: dict[str, object]) -> _Rows:
        sql = str(statement)
        self.calls.append((sql, params))
        if "apply_candidate_decision" in sql:
            return _Rows([{"receipt": {"replayed": False}}])
        return _Rows(self._candidates)


class _Transaction:
    def __init__(self, connection: _HistoricalConnection) -> None:
        self._connection = connection
        self.exited_cleanly = False

    def __enter__(self) -> _HistoricalConnection:
        return self._connection

    def __exit__(self, exc_type: object, _exc: object, _traceback: object) -> None:
        self.exited_cleanly = exc_type is None


class _Engine:
    def __init__(self, connection: _HistoricalConnection) -> None:
        self.transaction = _Transaction(connection)

    def begin(self) -> _Transaction:
        return self.transaction


class _ControlledImportReplayConnection(_HistoricalConnection):
    def __init__(
        self,
        candidates: list[dict[str, Any]],
        receipt: dict[str, object],
    ) -> None:
        super().__init__(candidates)
        self._receipt = receipt

    def execute(self, statement: object, params: dict[str, object]) -> _Rows:
        sql = str(statement)
        if "internal_import.controlled_batch_receipt" in sql:
            self.calls.append((sql, params))
            return _Rows([self._receipt])
        return super().execute(statement, params)


class _StatefulHistoricalConnection(_HistoricalConnection):
    def execute(self, statement: object, params: dict[str, object]) -> _Rows:
        result = super().execute(statement, params)
        if "apply_candidate_decision" in str(statement):
            for candidate in self._candidates:
                if candidate["candidate_id"] == params["candidate_id"]:
                    candidate["status"] = "CONFIRMED"
                    candidate["revision"] = int(candidate["revision"]) + 1
        return result


def test_historical_auto_confirmation_is_disabled_by_default() -> None:
    settings = HistoricalAutoConfirmationSettings()

    assert settings.enabled is False
    assert settings.cutoff_month == "2026-08"


def test_historical_auto_confirmation_reads_its_operator_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEDGERBRIDGE_TEST_HISTORICAL_AUTO_IMPORT_ENABLED", "true")
    monkeypatch.setenv("LEDGERBRIDGE_TEST_HISTORICAL_AUTO_IMPORT_CUTOFF_MONTH", "2026-07")

    settings = HistoricalAutoConfirmationSettings()

    assert settings.enabled is True
    assert settings.cutoff_month == "2026-07"


def test_historical_auto_confirmation_rejects_a_cutoff_after_august_2026() -> None:
    with pytest.raises(ValidationError, match="must not include 2026-09 or later"):
        HistoricalAutoConfirmationSettings(
            enabled=True,
            cutoff_month="2026-09",
        )


def test_disabled_historical_auto_confirmation_is_a_database_noop() -> None:
    result = confirm_test_historical_candidates(
        _UnexpectedConnection(),  # type: ignore[arg-type]
        HistoricalAutoConfirmationSettings(enabled=False, cutoff_month="2026-08"),
    )

    assert result.enabled is False
    assert result.cutoff_month == "2026-08"
    assert result.selected_count == 0
    assert result.confirmed_count == 0


def test_enabled_historical_auto_confirmation_confirms_only_complete_pending_history() -> None:
    eligible = UUID("81000000-0000-4000-8000-000000000001")
    entity = UUID("81000000-0000-4000-8000-000000000002")
    business_unit = UUID("81000000-0000-4000-8000-000000000003")
    category = UUID("81000000-0000-4000-8000-000000000010")
    connection = _HistoricalConnection(
        [
            {
                "candidate_id": eligible,
                "entity_id": entity,
                "revision": 3,
                "status": "PENDING",
                "business_unit_id": business_unit,
                "category_id": category,
                "amount_minor": 100,
                "accounting_month": date(2026, 8, 1),
            },
            {
                "candidate_id": UUID("81000000-0000-4000-8000-000000000004"),
                "entity_id": entity,
                "revision": 1,
                "status": "PENDING",
                "business_unit_id": business_unit,
                "category_id": category,
                "amount_minor": 100,
                "accounting_month": date(2026, 9, 1),
            },
            {
                "candidate_id": UUID("81000000-0000-4000-8000-000000000005"),
                "entity_id": entity,
                "revision": 1,
                "status": "INCOMPLETE",
                "business_unit_id": business_unit,
                "category_id": category,
                "amount_minor": 100,
                "accounting_month": None,
            },
            {
                "candidate_id": UUID("81000000-0000-4000-8000-000000000006"),
                "entity_id": entity,
                "revision": 1,
                "status": "CONFLICTED",
                "business_unit_id": business_unit,
                "category_id": category,
                "amount_minor": 100,
                "accounting_month": date(2026, 8, 1),
            },
            {
                "candidate_id": UUID("81000000-0000-4000-8000-000000000007"),
                "entity_id": entity,
                "revision": 1,
                "status": "PENDING",
                "business_unit_id": business_unit,
                "category_id": category,
                "amount_minor": 100,
                "accounting_month": None,
            },
            {
                "candidate_id": UUID("81000000-0000-4000-8000-000000000008"),
                "entity_id": entity,
                "revision": 1,
                "status": "PENDING",
                "business_unit_id": None,
                "category_id": category,
                "amount_minor": 100,
                "accounting_month": date(2026, 8, 1),
            },
            {
                "candidate_id": UUID("81000000-0000-4000-8000-000000000009"),
                "entity_id": entity,
                "revision": 1,
                "status": "PENDING",
                "business_unit_id": business_unit,
                "category_id": None,
                "amount_minor": None,
                "accounting_month": date(2026, 8, 1),
            },
        ]
    )

    result = confirm_test_historical_candidates(
        connection,  # type: ignore[arg-type]
        HistoricalAutoConfirmationSettings(enabled=True, cutoff_month="2026-08"),
        decided_at=datetime(2026, 9, 1, 2, 3, tzinfo=UTC),
    )

    assert result.enabled is True
    assert result.selected_count == 1
    assert result.confirmed_count == 1
    assert len(connection.calls) == 2
    command_sql, command = connection.calls[1]
    assert "internal_command.apply_candidate_decision" in command_sql
    assert command["candidate_id"] == eligible
    assert command["authorized_entity_id"] == entity
    assert command["current_business_unit_id"] == business_unit
    assert command["target_business_unit_id"] == business_unit
    assert command["decision"] == "CONFIRM"
    assert command["expected_revision"] == 3
    assert command["actor_ref"] == "system:test-historical-auto-import"
    assert "test historical auto-import" in str(command["reason"])
    assert command["set_business_unit"] is False
    assert command["set_category"] is False
    assert command["set_amount"] is False
    assert command["set_month"] is False
    assert all(
        "journal" not in sql.lower() and "posted" not in sql.lower() for sql, _ in connection.calls
    )


def test_historical_auto_confirmation_can_be_scoped_to_newly_imported_candidates() -> None:
    imported = UUID("82000000-0000-4000-8000-000000000001")
    existing = UUID("82000000-0000-4000-8000-000000000002")
    entity = UUID("82000000-0000-4000-8000-000000000003")
    business_unit = UUID("82000000-0000-4000-8000-000000000004")
    category = UUID("82000000-0000-4000-8000-000000000005")
    connection = _HistoricalConnection(
        [
            {
                "candidate_id": imported,
                "entity_id": entity,
                "revision": 1,
                "status": "PENDING",
                "business_unit_id": business_unit,
                "category_id": category,
                "amount_minor": 100,
                "accounting_month": date(2026, 8, 1),
            },
            {
                "candidate_id": existing,
                "entity_id": entity,
                "revision": 1,
                "status": "PENDING",
                "business_unit_id": business_unit,
                "category_id": category,
                "amount_minor": 100,
                "accounting_month": date(2026, 8, 1),
            },
        ]
    )

    result = confirm_test_historical_candidates(
        connection,  # type: ignore[arg-type]
        HistoricalAutoConfirmationSettings(enabled=True, cutoff_month="2026-08"),
        candidate_refs=(imported,),
        decided_at=datetime(2026, 9, 1, 2, 3, tzinfo=UTC),
    )

    assert result.selected_count == 1
    assert result.confirmed_count == 1
    assert connection.calls[0][1]["candidate_ids"] == [imported]
    assert [call[1]["candidate_id"] for call in connection.calls[1:]] == [imported]


def test_existing_candidate_one_shot_owns_one_atomic_transaction() -> None:
    candidate = UUID("83000000-0000-4000-8000-000000000001")
    connection = _HistoricalConnection(
        [
            {
                "candidate_id": candidate,
                "entity_id": UUID("83000000-0000-4000-8000-000000000002"),
                "revision": 1,
                "status": "PENDING",
                "business_unit_id": UUID("83000000-0000-4000-8000-000000000003"),
                "category_id": UUID("83000000-0000-4000-8000-000000000004"),
                "amount_minor": 100,
                "accounting_month": date(2026, 8, 1),
            }
        ]
    )
    engine = _Engine(connection)

    result = confirm_existing_test_historical_candidates(
        engine,  # type: ignore[arg-type]
        HistoricalAutoConfirmationSettings(enabled=True, cutoff_month="2026-08"),
        decided_at=datetime(2026, 9, 1, 2, 3, tzinfo=UTC),
    )

    assert result.confirmed_count == 1
    assert engine.transaction.exited_cleanly is True


def test_existing_candidate_one_shot_is_idempotent_after_confirmation() -> None:
    candidate = UUID("83100000-0000-4000-8000-000000000001")
    connection = _StatefulHistoricalConnection(
        [
            {
                "candidate_id": candidate,
                "entity_id": UUID("83100000-0000-4000-8000-000000000002"),
                "revision": 1,
                "status": "PENDING",
                "business_unit_id": UUID("83100000-0000-4000-8000-000000000003"),
                "category_id": UUID("83100000-0000-4000-8000-000000000004"),
                "amount_minor": 100,
                "accounting_month": date(2026, 8, 1),
            }
        ]
    )
    engine = _Engine(connection)
    settings = HistoricalAutoConfirmationSettings(enabled=True, cutoff_month="2026-08")

    first = confirm_existing_test_historical_candidates(engine, settings)  # type: ignore[arg-type]
    second = confirm_existing_test_historical_candidates(engine, settings)  # type: ignore[arg-type]

    assert first.confirmed_count == 1
    assert second.confirmed_count == 0
    assert sum("apply_candidate_decision" in sql for sql, _ in connection.calls) == 1


def test_controlled_import_replay_routes_its_candidates_through_the_shared_seam(
    tmp_path: Path,
) -> None:
    batch_ref = UUID("84000000-0000-4000-8000-000000000001")
    candidate_ref = UUID("84000000-0000-4000-8000-000000000002")
    entity_ref = UUID("84000000-0000-4000-8000-000000000003")
    business_unit_ref = UUID("84000000-0000-4000-8000-000000000004")
    evidence_ref = UUID("84000000-0000-4000-8000-000000000005")
    prepared = PreparedManifest(
        schema_version="ledgerbridge.controlled-review-prepared.v1",
        source_manifest_sha256="a" * 64,
        batch_ref=batch_ref,
        generated_at=datetime(2026, 8, 31, tzinfo=UTC),
        source_description="synthetic controlled replay",
        entity=ImportEntity(entity_ref=entity_ref, name="Synthetic entity"),
        business_unit=ImportBusinessUnit(
            business_unit_ref=business_unit_ref,
            ref="synthetic-unit",
            label="Synthetic unit",
        ),
        categories=(
            ImportCategory(
                category_ref=UUID("84000000-0000-4000-8000-000000000006"),
                code="SYNTHETIC",
                label="Synthetic",
            ),
        ),
        evidence=(
            PreparedEvidence(
                evidence_ref=evidence_ref,
                display_name="synthetic.bin",
                declared_media_type="application/octet-stream",
                plaintext_sha256="b" * 64,
                plaintext_size=1,
                object_ref="c" * 64,
                ciphertext_sha256="d" * 64,
                ciphertext_size=2,
                storage_key="sha256/dd/dd/" + "d" * 64,
                envelope_schema="ledgerbridge.secretstream.v1",
                algorithm="xchacha20poly1305-secretstream",
                chunk_size=65_536,
                stream_header="e" * 48,
                wrapped_key_generation="synthetic-1",
                wrapped_key_nonce="f" * 48,
                wrapped_key_ciphertext="0" * 96,
                purpose="ledgerbridge-artifact-v2",
            ),
        ),
        candidates=(
            ImportCandidate(
                candidate_ref=candidate_ref,
                operation_id=UUID("84000000-0000-4000-8000-000000000007"),
                ingest_channel="CONTROLLED_UPLOAD",
                source_system="synthetic",
                source_event_ref=UUID("84000000-0000-4000-8000-000000000008"),
                display_label="Synthetic import",
                category_code="SYNTHETIC",
                amount_minor=123,
                accounting_month="2026-08",
                summary="Synthetic candidate",
                confidence_basis_points=9000,
                evidence_refs=(evidence_ref,),
            ),
        ),
    )
    prepared_path = (tmp_path / "prepared.json").resolve()
    raw = prepared.model_dump_json().encode("utf-8")
    prepared_path.write_bytes(raw)
    connection = _ControlledImportReplayConnection(
        [
            {
                "candidate_id": candidate_ref,
                "entity_id": entity_ref,
                "revision": 1,
                "status": "PENDING",
                "business_unit_id": business_unit_ref,
                "category_id": UUID("84000000-0000-4000-8000-000000000006"),
                "amount_minor": 123,
                "accounting_month": date(2026, 8, 1),
            }
        ],
        {
            "source_manifest_sha256": bytes.fromhex(prepared.source_manifest_sha256),
            "prepared_manifest_sha256": hashlib.sha256(raw).digest(),
            "evidence_count": 1,
            "candidate_count": 1,
            "audit_horizon_sequence": 10,
            "audit_horizon_hash": bytes.fromhex("1" * 64),
        },
    )

    result = import_prepared_manifest(
        _Engine(connection),  # type: ignore[arg-type]
        prepared_path,
        historical_auto_confirmation=HistoricalAutoConfirmationSettings(
            enabled=True,
            cutoff_month="2026-08",
        ),
    )

    assert result.replayed is True
    assert result.historical_auto_confirmed_count == 1
    assert [call[1]["candidate_id"] for call in connection.calls if "candidate_id" in call[1]] == [
        candidate_ref
    ]
