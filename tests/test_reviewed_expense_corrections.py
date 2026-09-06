from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import Connection

from scripts import correct_reviewed_company_expenses as command
from scripts.correct_reviewed_company_expenses import Correction, apply_correction, read_plan


def _plan() -> Correction:
    return Correction(
        transaction_ref=UUID(int=1),
        managed_account_ref=UUID(int=2),
        occurred_on=date(2026, 8, 2),
        amount_minor=-1200,
        expected_revision=1,
        expected_category="OPERATING_FEE",
        expected_item="SUPPLIES",
        category="BOTTLED_WATER",
        item="WATER",
        operation_id=UUID(int=3),
        actor_ref="reviewer:synthetic",
        reason="Confirmed source evidence",
    )


def _connection(plan: Correction, *, replay: bool = False) -> MagicMock:
    connection = MagicMock(spec=Connection)
    connection.execute.return_value.scalar_one.return_value = "ledgerbridge_owner"
    current = {
        "status": "CONFIRMED",
        "revision": 1,
        "category_code": "OPERATING_FEE",
        "reporting_item_code": "SUPPLIES",
        "reporting_item_revision": 1,
        "operation_id": plan.operation_id if replay else UUID(int=4),
    }
    fact = {
        "managed_account_ref": plan.managed_account_ref,
        "occurred_on": plan.occurred_on,
        "amount_minor": plan.amount_minor,
    }
    previous = (
        {
            "command_sha256": hashlib.sha256(plan.model_dump_json().encode()).digest(),
            "transaction_ref": plan.transaction_ref,
            "revision": 2,
        }
        if replay
        else None
    )
    connection.execute.return_value.mappings.return_value.one_or_none.side_effect = [
        fact,
        previous,
        current,
        {"revision": 1, "status": "ACTIVE"},
    ]
    return connection


def test_correction_appends_audited_revision_without_rewriting_fact() -> None:
    plan = _plan()
    connection = _connection(plan)
    assert apply_correction(connection, plan)
    sql = [str(call.args[0]) for call in connection.execute.call_args_list]
    assert any("append_audit_event" in query for query in sql)
    assert len([query for query in sql if "INSERT INTO" in query]) == 1
    assert not any(word in query.upper() for query in sql for word in ("UPDATE PUBLIC.", "DELETE "))
    insertion = connection.execute.call_args.args[1]
    assert insertion["category"] == "BOTTLED_WATER"
    assert insertion["revision"] == 2
    payload = json.loads(insertion["payload"])
    assert payload["previous_category_code"] == "OPERATING_FEE"
    assert payload["source_binding"]["amount_minor"] == "-1200"


def test_replay_is_no_op_without_new_audit_or_classification() -> None:
    plan = _plan()
    connection = _connection(plan, replay=True)
    assert not apply_correction(connection, plan)
    assert not any(
        "append_audit_event" in str(call.args[0]) or "INSERT" in str(call.args[0])
        for call in connection.execute.call_args_list
    )


def test_runtime_role_is_rejected_before_reading_financial_facts() -> None:
    connection = _connection(_plan())
    connection.execute.return_value.scalar_one.return_value = "ledgerbridge_reader"
    with pytest.raises(ValueError, match="OWNER_ROLE_REQUIRED"):
        apply_correction(connection, _plan())
    assert connection.execute.call_count == 1


@pytest.mark.parametrize(
    "change",
    [
        {"amount_minor": 1200},
        {"amount_minor": -12.3},
        {"category": "INCOME"},
        {"category": "OPERATING_FEE", "item": "SUPPLIES"},
        {"reason": " "},
    ],
)
def test_unsafe_plan_is_rejected(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Correction.model_validate({**_plan().model_dump(), **change})


def test_plan_hash_and_unique_identities_are_required(tmp_path: Path) -> None:
    plan = tmp_path / "synthetic.json"
    raw = json.dumps([_plan().model_dump(mode="json")]).encode()
    plan.write_bytes(raw)
    assert read_plan(plan, hashlib.sha256(raw).hexdigest()) == [_plan()]
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        read_plan(plan, "0" * 64)
    raw = json.dumps([_plan().model_dump(mode="json")] * 2).encode()
    plan.write_bytes(raw)
    with pytest.raises(ValueError, match="DUPLICATE_PLAN"):
        read_plan(plan, hashlib.sha256(raw).hexdigest())


def test_cli_does_not_expose_private_validation_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "invalid.json"
    raw = json.dumps(
        [{**_plan().model_dump(mode="json"), "amount_minor": "PRIVATE_SENTINEL"}]
    ).encode()
    path.write_bytes(raw)
    monkeypatch.setattr(
        "sys.argv", ["correct", "--plan", str(path), "--sha256", hashlib.sha256(raw).hexdigest()]
    )
    assert command.main() == 2
    output = capsys.readouterr()
    assert output.out == "CORRECTION_FAILED\n"
    assert output.err == ""
