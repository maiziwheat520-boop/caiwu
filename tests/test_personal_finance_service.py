"""The personal statement reader must fail closed on every malformed projection.

The service reads one entity's statement through SECURITY DEFINER functions at a
single immutable audit horizon.  Everything it trusts comes back over that seam,
so a projection that is short, wide, out of scope, or simply wrong has to end as
a refusal rather than as a statement the caller believes.  A scripted session
stands in for the database so each of those shapes can be stated exactly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ledgerbridge.internal_read_contract import (
    AuthorizationDenied,
    Capability,
    EntityGrant,
    ResourceNotVisible,
    WorkloadPrincipal,
)
from ledgerbridge.internal_read_service import InternalReadBackendUnavailable
from ledgerbridge.personal_finance_service import DatabasePersonalFinanceService

ENTITY = UUID("73000000-0000-4000-8000-000000000001")
STATEMENT = UUID("73000000-0000-4000-8000-000000000002")
ACCOUNT = UUID("73000000-0000-4000-8000-000000000003")
HORIZON_HASH = bytes.fromhex("ab" * 32)


def _principal(
    *,
    capabilities: frozenset[Capability] | None = None,
    entity_ref: UUID = ENTITY,
) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:personal-finance-test",
        san_uri="spiffe://ledgerbridge.test/personal-finance",
        policy_generation=1,
        capabilities=capabilities
        if capabilities is not None
        else frozenset({Capability.LEDGER_READ}),
        grants=(EntityGrant(entity_ref=entity_ref, allow_account_registry=True),),
    )


def _horizon(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {"sequence": 42, "hash": HORIZON_HASH}
    row.update(overrides)
    return row


def _summary(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "statement_ref": STATEMENT,
        "managed_account_ref": ACCOUNT,
        "period_start": date(2026, 1, 1),
        "period_end": date(2026, 1, 31),
        "transaction_count": 2,
        "review_status": "CONFIRMED",
        "review_revision": 1,
    }
    row.update(overrides)
    return row


def _registry(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "owner_kind": "PERSON",
        "accounts": [
            {
                "managed_account_ref": str(ACCOUNT),
                "institution_code": "mybank",
                "account_suffix": "7968",
            }
        ],
    }
    row.update(overrides)
    return row


def _transaction(row_number: int, amount_minor: int) -> dict[str, object]:
    return {
        "source_row_number": row_number,
        "occurred_at": datetime(2026, 1, row_number, 9, 0, tzinfo=UTC),
        "amount_minor": amount_minor,
        "balance_minor": 500_000 + amount_minor,
        "currency": "CNY",
        "counterparty_name": "合成商户",
        "counterparty_account_masked": "****1111",
        "counterparty_institution": "合成银行",
        "transaction_name": "转入" if amount_minor > 0 else "消费",
    }


class _Result:
    def __init__(self, rows: Sequence[Mapping[str, object]] | None, scalar: object) -> None:
        self._rows = rows
        self._scalar = scalar

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> Mapping[str, object] | None:
        assert self._rows is not None
        return self._rows[0] if self._rows else None

    def all(self) -> list[Mapping[str, object]]:
        assert self._rows is not None
        return list(self._rows)

    def scalar_one_or_none(self) -> object:
        return self._scalar


class _ScriptedSession:
    """Answers each internal_read call by matching the SQL the service issues."""

    def __init__(
        self,
        *,
        horizon: Mapping[str, object] | None,
        summary: Mapping[str, object] | None,
        registry: object,
        pages: Sequence[Sequence[Mapping[str, object]]],
        raises: Exception | None = None,
    ) -> None:
        self._horizon = horizon
        self._summary = summary
        self._registry = registry
        self._pages = list(pages)
        self._raises = raises
        self.transaction_calls: list[Mapping[str, object]] = []

    def __enter__(self) -> _ScriptedSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: object, params: Mapping[str, object] | None = None) -> _Result:
        sql = str(statement)
        if self._raises is not None and "list_bank_statement_transactions" in sql:
            raise self._raises
        if "current_audit_horizon" in sql:
            return _Result([self._horizon] if self._horizon is not None else [], None)
        if "get_bank_statement_summary" in sql:
            return _Result([self._summary] if self._summary is not None else [], None)
        if "get_account_registry_projection" in sql:
            return _Result(None, self._registry)
        if "list_bank_statement_transactions" in sql:
            self.transaction_calls.append(dict(params or {}))
            return _Result(self._pages.pop(0) if self._pages else [], None)
        raise AssertionError(f"the service issued an unexpected query: {sql}")


def _service(session: _ScriptedSession) -> DatabasePersonalFinanceService:
    def factory() -> Session:
        return cast(Session, session)

    return DatabasePersonalFinanceService(factory)


def _statement(session: _ScriptedSession, **kwargs: object) -> object:
    return _service(session).statement(
        _principal(),
        statement_ref=STATEMENT,
        entity_ref=ENTITY,
        **kwargs,  # type: ignore[arg-type]
    )


def _healthy(**overrides: object) -> _ScriptedSession:
    defaults: dict[str, Any] = {
        "horizon": _horizon(),
        "summary": _summary(),
        "registry": _registry(),
        "pages": [[_transaction(1, 125_34), _transaction(2, -20_00)]],
    }
    defaults.update(overrides)
    return _ScriptedSession(**defaults)


# --- the happy path --------------------------------------------------------


def test_a_complete_statement_is_returned_at_one_horizon() -> None:
    session = _healthy()
    page = _statement(session)

    assert page.contract_version == "ledgerbridge.personal-finance.v1"  # type: ignore[attr-defined]
    assert page.snapshot_revision == HORIZON_HASH.hex()  # type: ignore[attr-defined]
    assert page.statement.institution_code == "mybank"  # type: ignore[attr-defined]
    assert page.statement.account_suffix == "7968"  # type: ignore[attr-defined]
    assert len(page.items) == 2  # type: ignore[attr-defined]
    # Every page must be read as of the horizon taken on the first call.
    assert {call["horizon_sequence"] for call in session.transaction_calls} == {42}
    assert {call["horizon_hash"] for call in session.transaction_calls} == {HORIZON_HASH}


def test_the_summary_is_the_signed_arithmetic_of_the_rows() -> None:
    page = _statement(_healthy())
    assert page.summary.cash_inflow_minor == 125_34  # type: ignore[attr-defined]
    assert page.summary.cash_outflow_minor == 20_00  # type: ignore[attr-defined]
    assert page.summary.net_cash_flow_minor == 125_34 - 20_00  # type: ignore[attr-defined]


def test_transactions_are_paged_until_the_summary_count_is_met() -> None:
    session = _healthy(
        summary=_summary(transaction_count=3),
        pages=[[_transaction(1, 100), _transaction(2, 200)], [_transaction(3, 300)]],
    )
    page = _statement(session)
    assert [item.source_row_number for item in page.items] == [1, 2, 3]  # type: ignore[attr-defined]
    assert [call["after_row"] for call in session.transaction_calls] == [0, 2]


def test_a_projection_that_runs_out_of_rows_early_is_never_served_short() -> None:
    # The walk stops on an empty page, but the response contract then refuses
    # the page rather than handing back a statement missing two thirds of it.
    session = _healthy(summary=_summary(transaction_count=3), pages=[[_transaction(1, 100)], []])
    with pytest.raises(InternalReadBackendUnavailable, match="projection is unavailable"):
        _statement(session)


# --- authorization ---------------------------------------------------------


def test_a_principal_without_the_capability_is_refused() -> None:
    with pytest.raises(AuthorizationDenied):
        _service(_healthy()).statement(
            _principal(capabilities=frozenset({Capability.SYSTEM_READ})),
            statement_ref=STATEMENT,
            entity_ref=ENTITY,
        )


def test_a_principal_without_a_grant_for_the_entity_cannot_see_it() -> None:
    session = _healthy()
    with pytest.raises(ResourceNotVisible):
        _service(session).statement(
            _principal(entity_ref=uuid4()),
            statement_ref=STATEMENT,
            entity_ref=ENTITY,
        )
    # The refusal must happen before the database is consulted at all.
    assert session.transaction_calls == []


def test_a_statement_the_projection_does_not_return_is_not_found() -> None:
    with pytest.raises(ResourceNotVisible):
        _statement(_healthy(summary=None))


def test_a_registry_for_another_owner_kind_is_not_visible() -> None:
    with pytest.raises(ResourceNotVisible):
        _statement(_healthy(registry=_registry(owner_kind="COMPANY")))


def test_the_owner_kind_the_caller_asks_for_is_the_one_enforced() -> None:
    # The default is PERSON; asking for COMPANY against a PERSON projection
    # must refuse rather than fall back to whatever the row says.
    with pytest.raises(ResourceNotVisible):
        _statement(_healthy(), owner_kind="COMPANY")


# --- the audit horizon -----------------------------------------------------


def test_a_missing_audit_horizon_makes_the_backend_unavailable() -> None:
    with pytest.raises(InternalReadBackendUnavailable, match="horizon is unavailable"):
        _statement(_healthy(horizon=None))


@pytest.mark.parametrize(
    "overrides",
    [
        {"sequence": 0},
        {"sequence": -1},
        {"sequence": True},
        {"sequence": "42"},
        {"sequence": None},
        {"hash": b"short"},
        {"hash": HORIZON_HASH.hex()},
        {"hash": None},
    ],
)
def test_a_malformed_audit_horizon_is_never_trusted(overrides: dict[str, object]) -> None:
    with pytest.raises(InternalReadBackendUnavailable, match="horizon is malformed"):
        _statement(_healthy(horizon=_horizon(**overrides)))


# --- the summary row -------------------------------------------------------


@pytest.mark.parametrize("field", ["statement_ref", "managed_account_ref"])
def test_a_summary_without_a_usable_identity_is_unavailable(field: str) -> None:
    with pytest.raises(InternalReadBackendUnavailable, match="projection is unavailable"):
        _statement(_healthy(summary=_summary(**{field: "not-a-uuid"})))


@pytest.mark.parametrize("count", [0, -1, True, "2", None])
def test_a_summary_transaction_count_that_is_not_a_positive_integer_is_unavailable(
    count: object,
) -> None:
    with pytest.raises(InternalReadBackendUnavailable, match="projection is unavailable"):
        _statement(_healthy(summary=_summary(transaction_count=count)))


def test_a_statement_larger_than_the_bounded_response_is_refused() -> None:
    with pytest.raises(InternalReadBackendUnavailable, match="bounded response limit"):
        _statement(_healthy(summary=_summary(transaction_count=10_001)))


def test_a_summary_that_fails_the_response_contract_is_unavailable() -> None:
    with pytest.raises(InternalReadBackendUnavailable, match="projection is unavailable"):
        _statement(_healthy(summary=_summary(review_status="ARCHIVED")))


def test_a_statement_ref_string_is_accepted_where_a_uuid_is_expected() -> None:
    page = _statement(_healthy(summary=_summary(statement_ref=str(STATEMENT))))
    assert page.statement.statement_ref == STATEMENT  # type: ignore[attr-defined]


# --- the account registry projection ---------------------------------------


@pytest.mark.parametrize("registry", [None, "not-a-mapping", 42])
def test_a_registry_projection_that_is_not_a_mapping_is_not_visible(registry: object) -> None:
    with pytest.raises(ResourceNotVisible):
        _statement(_healthy(registry=registry))


def test_a_registry_without_an_account_list_is_malformed() -> None:
    with pytest.raises(InternalReadBackendUnavailable, match="registry is malformed"):
        _statement(_healthy(registry=_registry(accounts={"managed_account_ref": str(ACCOUNT)})))


@pytest.mark.parametrize(
    "accounts",
    [
        [],
        [{"managed_account_ref": str(uuid4()), "institution_code": "x", "account_suffix": "1234"}],
        [
            {
                "managed_account_ref": str(ACCOUNT),
                "institution_code": "a",
                "account_suffix": "1234",
            },
            {
                "managed_account_ref": str(ACCOUNT),
                "institution_code": "b",
                "account_suffix": "5678",
            },
        ],
        ["not-a-mapping"],
        [{"managed_account_ref": 42, "institution_code": "x", "account_suffix": "1234"}],
    ],
)
def test_an_account_that_does_not_resolve_to_exactly_one_row_is_unavailable(
    accounts: list[object],
) -> None:
    with pytest.raises(InternalReadBackendUnavailable, match="account is unavailable"):
        _statement(_healthy(registry=_registry(accounts=accounts)))


@pytest.mark.parametrize(
    "account",
    [
        {"managed_account_ref": str(ACCOUNT), "institution_code": 1, "account_suffix": "7968"},
        {"managed_account_ref": str(ACCOUNT), "institution_code": "mybank", "account_suffix": 7968},
    ],
)
def test_an_account_identity_that_is_not_text_is_malformed(account: dict[str, object]) -> None:
    with pytest.raises(InternalReadBackendUnavailable, match="account is malformed"):
        _statement(_healthy(registry=_registry(accounts=[account])))


# --- the transaction pages -------------------------------------------------


def test_a_page_that_does_not_advance_the_cursor_is_refused() -> None:
    # Without this the reader would loop forever on a stuck projection.
    session = _healthy(
        summary=_summary(transaction_count=4),
        pages=[[_transaction(1, 100), _transaction(2, 200)], [_transaction(2, 300)]],
    )
    with pytest.raises(InternalReadBackendUnavailable, match="did not advance"):
        _statement(session)


def test_a_page_that_overruns_the_summary_count_is_refused() -> None:
    session = _healthy(
        summary=_summary(transaction_count=1),
        pages=[[_transaction(1, 100), _transaction(2, 200)]],
    )
    with pytest.raises(InternalReadBackendUnavailable, match="exceeded summary"):
        _statement(session)


def test_a_transaction_row_that_fails_the_contract_is_unavailable() -> None:
    naive = _transaction(1, 100)
    naive["occurred_at"] = datetime(2026, 1, 1, 9, 0)
    with pytest.raises(InternalReadBackendUnavailable, match="projection is unavailable"):
        _statement(_healthy(summary=_summary(transaction_count=1), pages=[[naive]]))


def test_only_the_public_transaction_fields_reach_the_response() -> None:
    row = _transaction(1, 100)
    row["counterparty_account"] = "6222020200001111111"
    page = _statement(_healthy(summary=_summary(transaction_count=1), pages=[[row]]))
    rendered = page.items[0].model_dump()  # type: ignore[attr-defined]
    assert "counterparty_account" not in rendered
    assert rendered["counterparty_account_masked"] == "****1111"


def test_a_database_failure_is_reported_as_an_unavailable_backend() -> None:
    failure = OperationalError("SELECT 1", {}, Exception("connection reset"))
    with pytest.raises(InternalReadBackendUnavailable, match="projection is unavailable"):
        _statement(_healthy(raises=failure))


def test_an_unavailable_backend_raised_inside_is_not_reclassified() -> None:
    # The service re-raises its own refusals untouched; only foreign failures
    # are folded into "projection is unavailable".
    original = InternalReadBackendUnavailable("deliberate inner failure")
    with pytest.raises(InternalReadBackendUnavailable, match="deliberate inner failure"):
        _statement(_healthy(raises=original))
