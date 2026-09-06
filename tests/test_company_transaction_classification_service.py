"""What the classification service does when the database seam misbehaves.

Every classification the service serves or writes crosses one SECURITY DEFINER
seam, read at a single immutable audit horizon. The service therefore treats
what comes back over that seam as a claim to be checked, not as its own state:
a missing horizon, a projection that does not validate, a scope the principal
does not hold, or a command the database rejected each has to end as a named
refusal. A scripted session stands in for the database so each of those
shapes can be stated exactly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from ledgerbridge.company_transaction_classification import (
    CashflowRole,
    CompanyTransactionCategory,
    CompanyTransactionCategorySummary,
    CompanyTransactionClassificationReviewRequest,
    DatabaseCompanyTransactionClassificationService,
)
from ledgerbridge.internal_candidate_command import (
    CandidateCommandIdempotencyConflict,
    CandidateCommandRejected,
    CandidateCommandUnavailable,
)
from ledgerbridge.internal_read_contract import (
    AuthorizationDenied,
    Capability,
    EntityGrant,
    ResourceNotVisible,
    WorkloadPrincipal,
)

ENTITY = UUID("7c000000-0000-4000-8000-000000000001")
OTHER_ENTITY = UUID("7c000000-0000-4000-8000-000000000002")
TRANSACTION = UUID("7c000000-0000-4000-8000-000000000003")
OPERATION = UUID("7c000000-0000-4000-8000-000000000004")
ASSERTION = UUID("7c000000-0000-4000-8000-000000000005")
HORIZON_HASH = bytes.fromhex("cd" * 32)
CAPABILITIES = frozenset(
    {
        Capability.BANK_STATEMENT_REVIEW_READ,
        Capability.BANK_STATEMENT_REVIEW_DECIDE,
        Capability.COMPANY_REPORT_READ,
    }
)


def _principal(
    *,
    capabilities: frozenset[Capability] | None = None,
    grants: tuple[EntityGrant, ...] | None = None,
) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:company-classification-test",
        san_uri="spiffe://ledgerbridge.test/company-classification",
        policy_generation=1,
        capabilities=CAPABILITIES if capabilities is None else capabilities,
        grants=(
            (EntityGrant(entity_ref=ENTITY, allow_account_registry=True),)
            if grants is None
            else grants
        ),
    )


def _horizon(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {"sequence": 42, "hash": HORIZON_HASH}
    row.update(overrides)
    return row


def _classification(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "transaction_ref": str(TRANSACTION),
        "entity_ref": str(ENTITY),
        "occurred_at": datetime(2026, 3, 1, 9, 0, tzinfo=UTC),
        "amount_minor": -12_500,
        "currency": "CNY",
        "counterparty_name": "合成供应商",
        "transaction_name": "布草洗涤",
        "status": "PENDING",
        "category_code": None,
        "cashflow_role": None,
        "revision": 1,
        "source": "AUTO_RULE",
        "rule_version": "2026.03",
    }
    item.update(overrides)
    return item


def _summary(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "entity_ref": str(ENTITY),
        "from_date": date(2026, 3, 1),
        "to_date_exclusive": date(2026, 4, 1),
        "confirmed_count": 1,
        "pending_count": 0,
        "confirmed_gross_minor": 12_500,
        "categories": [],
    }
    value.update(overrides)
    return value


def _receipt(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "transaction_ref": str(TRANSACTION),
        "status": "CONFIRMED",
        "category_code": "LINEN_LAUNDRY",
        "reporting_item_code": None,
        "reporting_item_revision": None,
        "revision": 2,
        "created": True,
    }
    value.update(overrides)
    return value


class _Result:
    def __init__(self, rows: Sequence[Mapping[str, object]] | None, scalar: object) -> None:
        self._rows = rows
        self._scalar = scalar

    def mappings(self) -> _Result:
        return self

    def first(self) -> Mapping[str, object] | None:
        assert self._rows is not None
        return self._rows[0] if self._rows else None

    def __iter__(self) -> Any:
        assert self._rows is not None
        return iter(self._rows)

    def scalar_one(self) -> object:
        if isinstance(self._scalar, Exception):
            raise self._scalar
        return self._scalar


class _ScriptedSession:
    """Answers each call by matching the SQL the service issues."""

    def __init__(
        self,
        *,
        horizon: Mapping[str, object] | None = None,
        items: Sequence[Mapping[str, object]] | None = None,
        summary: object = None,
        receipt: object = None,
        raises: Exception | None = None,
    ) -> None:
        self._horizon = horizon
        self._items = items
        self._summary = summary
        self._receipt = receipt
        self._raises = raises
        self.committed = False
        self.calls: list[Mapping[str, object]] = []

    def __enter__(self) -> _ScriptedSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def commit(self) -> None:
        self.committed = True

    def execute(self, statement: object, params: Mapping[str, object] | None = None) -> _Result:
        sql = str(statement)
        self.calls.append(dict(params or {}))
        if "current_audit_horizon" in sql:
            return _Result([self._horizon] if self._horizon is not None else [], None)
        if self._raises is not None:
            raise self._raises
        if "list_company_transaction_classifications_as_of" in sql:
            return _Result(list(self._items or ()), None)
        if "get_company_transaction_classification_summary_v2_as_of" in sql:
            return _Result(None, self._summary)
        if "review_company_transaction_classification_v2" in sql:
            return _Result(None, self._receipt)
        raise AssertionError(f"the service issued an unexpected query: {sql}")


def _service(session: _ScriptedSession) -> DatabaseCompanyTransactionClassificationService:
    def factory() -> Session:
        return cast(Session, session)

    return DatabaseCompanyTransactionClassificationService(factory, factory)


def _request(**overrides: object) -> CompanyTransactionClassificationReviewRequest:
    values: dict[str, Any] = {
        "entity_ref": ENTITY,
        "expected_revision": 1,
        "category_code": CompanyTransactionCategory.LINEN_LAUNDRY,
        "reason": "matched the laundry contract",
    }
    values.update(overrides)
    return CompanyTransactionClassificationReviewRequest(**values)


def _review(session: _ScriptedSession, **overrides: object) -> object:
    return _service(session).review(
        _principal(),
        transaction_ref=TRANSACTION,
        operation_id=OPERATION,
        assertion_jti=ASSERTION,
        actor_ref="human:reviewer",
        command=_request(**overrides),
    )


def _dbapi_error(sqlstate: str | None) -> DBAPIError:
    class _Orig(Exception):
        def __init__(self) -> None:
            self.sqlstate = sqlstate

    return DBAPIError("SELECT 1", {}, _Orig())


# --- the projections the service refuses to believe -------------------------


def test_a_pending_classification_cannot_claim_a_cashflow_role() -> None:
    """PENDING means nobody has decided yet, so it cannot carry a decision."""

    session = _ScriptedSession(
        horizon=_horizon(),
        items=[{"item": _classification(cashflow_role="OPERATING_EXPENSE")}],
    )
    with pytest.raises(CandidateCommandUnavailable, match="reader is unavailable"):
        _service(session).list_current(_principal())


def test_a_summary_category_must_pair_its_reporting_item_code_and_label() -> None:
    with pytest.raises(ValidationError, match="code and label must be supplied together"):
        CompanyTransactionCategorySummary(
            category_code=CompanyTransactionCategory.OPERATING_FEE,
            reporting_item_code="linen",
            reporting_item_label=None,
            cashflow_role=CashflowRole.OPERATING_EXPENSE,
            transaction_count=1,
            inflow_minor=0,
            outflow_minor=12_500,
            net_minor=-12_500,
            gross_minor=12_500,
            transaction_share_ppm=1_000_000,
            gross_share_ppm=1_000_000,
        )


# --- the scope the principal actually holds ---------------------------------


def test_a_principal_without_the_capability_is_denied() -> None:
    session = _ScriptedSession(horizon=_horizon())
    with pytest.raises(AuthorizationDenied):
        _service(session).list_current(_principal(capabilities=frozenset()))


def test_a_principal_with_no_entity_at_all_sees_nothing() -> None:
    session = _ScriptedSession(horizon=_horizon())
    with pytest.raises(ResourceNotVisible, match="scope is not visible"):
        _service(session).list_current(_principal(grants=()))


def test_a_review_of_a_transaction_outside_the_granted_scope_is_refused() -> None:
    session = _ScriptedSession(horizon=_horizon(), receipt=_receipt())
    with pytest.raises(ResourceNotVisible, match="outside the authorized scope"):
        _review(session, entity_ref=OTHER_ENTITY)
    assert not session.committed


# --- the audit horizon ------------------------------------------------------


def test_a_missing_audit_horizon_is_refused() -> None:
    session = _ScriptedSession(horizon=None)
    with pytest.raises(CandidateCommandUnavailable, match="audit horizon is unavailable"):
        _service(session).list_current(_principal())


@pytest.mark.parametrize(
    "overrides",
    [{"sequence": 0}, {"sequence": -1}, {"hash": bytes.fromhex("cd" * 31)}, {"hash": b""}],
)
def test_an_audit_horizon_that_is_not_a_real_position_is_refused(
    overrides: dict[str, object],
) -> None:
    """A horizon is what makes two reads agree; a broken one cannot be repaired."""

    session = _ScriptedSession(horizon=_horizon(**overrides))
    with pytest.raises(CandidateCommandUnavailable, match="audit horizon is invalid"):
        _service(session).list_current(_principal())


# --- listing ----------------------------------------------------------------


def test_current_classifications_are_read_at_one_horizon_newest_first() -> None:
    older = _classification(occurred_at=datetime(2026, 3, 1, 9, 0, tzinfo=UTC))
    newer = _classification(
        transaction_ref=str(OPERATION), occurred_at=datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
    )
    session = _ScriptedSession(horizon=_horizon(), items=[{"item": older}, {"item": newer}])

    page = _service(session).list_current(_principal())

    assert [item.occurred_at.day for item in page.items] == [2, 1]
    assert all(call.get("horizon_hash", HORIZON_HASH) == HORIZON_HASH for call in session.calls)


def test_a_reader_that_fails_mid_page_is_reported_as_unavailable() -> None:
    session = _ScriptedSession(
        horizon=_horizon(), raises=OperationalError("SELECT 1", {}, Exception("no connection"))
    )
    with pytest.raises(CandidateCommandUnavailable, match="reader is unavailable"):
        _service(session).list_current(_principal())


def test_a_row_that_does_not_carry_an_item_is_refused() -> None:
    session = _ScriptedSession(horizon=_horizon(), items=[{"other": {}}])
    with pytest.raises(CandidateCommandUnavailable, match="reader is unavailable"):
        _service(session).list_current(_principal())


# --- summaries --------------------------------------------------------------


def test_a_summary_range_that_does_not_move_forward_is_refused() -> None:
    session = _ScriptedSession(horizon=_horizon())
    with pytest.raises(ValueError, match="from_date must precede to_date_exclusive"):
        _service(session).summaries(
            _principal(), from_date=date(2026, 4, 1), to_date_exclusive=date(2026, 4, 1)
        )


def test_a_summary_is_returned_for_every_entity_in_scope() -> None:
    session = _ScriptedSession(horizon=_horizon(), summary=_summary())

    page = _service(session).summaries(
        _principal(), from_date=date(2026, 3, 1), to_date_exclusive=date(2026, 4, 1)
    )

    assert len(page.items) == 1
    assert page.items[0].confirmed_gross_minor == 12_500


def test_a_summary_that_does_not_validate_is_refused() -> None:
    session = _ScriptedSession(horizon=_horizon(), summary=_summary(confirmed_count=-1))
    with pytest.raises(CandidateCommandUnavailable, match="summary is unavailable"):
        _service(session).summaries(
            _principal(), from_date=date(2026, 3, 1), to_date_exclusive=date(2026, 4, 1)
        )


def test_a_summary_reader_that_fails_is_reported_as_unavailable() -> None:
    session = _ScriptedSession(
        horizon=_horizon(), raises=OperationalError("SELECT 1", {}, Exception("no connection"))
    )
    with pytest.raises(CandidateCommandUnavailable, match="summary is unavailable"):
        _service(session).summaries(
            _principal(), from_date=date(2026, 3, 1), to_date_exclusive=date(2026, 4, 1)
        )


# --- reviewing --------------------------------------------------------------


def test_a_review_that_the_database_accepted_is_committed_once() -> None:
    session = _ScriptedSession(horizon=_horizon(), receipt=_receipt())

    receipt = _review(session)

    assert receipt.revision == 2  # type: ignore[attr-defined]
    assert session.committed


@pytest.mark.parametrize(
    ("sqlstate", "error", "message"),
    [
        ("LB001", CandidateCommandIdempotencyConflict, "idempotency conflict"),
        ("LB002", CandidateCommandRejected, "revision is stale"),
        ("LB003", CandidateCommandRejected, "review was rejected"),
        ("LB004", ResourceNotVisible, "outside the authorized scope"),
        ("58030", CandidateCommandUnavailable, "review is unavailable"),
        (None, CandidateCommandUnavailable, "review is unavailable"),
    ],
)
def test_each_database_rejection_keeps_its_own_meaning(
    sqlstate: str | None, error: type[Exception], message: str
) -> None:
    """A stale revision and an unreachable database must not look alike to a caller."""

    session = _ScriptedSession(horizon=_horizon(), receipt=_dbapi_error(sqlstate))
    with pytest.raises(error, match=message):
        _review(session)
    assert not session.committed


def test_a_review_whose_receipt_does_not_validate_is_refused() -> None:
    session = _ScriptedSession(horizon=_horizon(), receipt=_receipt(revision=1))
    with pytest.raises(CandidateCommandUnavailable, match="review is unavailable"):
        _review(session)
    assert not session.committed


def test_a_review_whose_session_fails_is_reported_as_unavailable() -> None:
    session = _ScriptedSession(
        horizon=_horizon(), raises=OperationalError("SELECT 1", {}, Exception("no connection"))
    )
    with pytest.raises(CandidateCommandUnavailable, match="review is unavailable"):
        _review(session)
    assert not session.committed
