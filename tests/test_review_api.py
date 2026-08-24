from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.main import (
    app,
    get_authenticated_principal,
    get_review_service,
    require_review_api,
)
from ledgerbridge.models import ReviewItem, ReviewItemKind
from ledgerbridge.review_service import ReviewConflict, ReviewNotFound, ReviewService


class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one(self) -> Any:
        return self.value


class _Scalars:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def all(self) -> list[Any]:
        return self.values


class _FakeSession:
    def __init__(self, values: list[Any] | None = None) -> None:
        self.values = list(values or [])
        self.added: list[Any] = []
        self.review_id = uuid4()

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _FakeSession:
        return self

    def execute(self, _statement: object, _params: object | None = None) -> _Result:
        return _Result(uuid4())

    def scalar(self, _statement: object) -> Any:
        return self.values.pop(0) if self.values else None

    def scalars(self, _statement: object) -> _Scalars:
        return _Scalars(self.values)

    def get(self, _model: object, _item_id: UUID) -> Any:
        return self.values[0] if self.values else None

    def add(self, value: Any) -> None:
        if isinstance(value, ReviewItem) and value.id is None:
            value.id = self.review_id
        self.added.append(value)

    def flush(self) -> None:
        return None


class _FakeSessions:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    def __call__(self) -> _FakeSession:
        return self.session


def _open_item(kind: str = ReviewItemKind.DEDUP.value) -> ReviewItem:
    return ReviewItem(
        id=uuid4(),
        kind=kind,
        status="OPEN",
        summary="needs review",
        payload={"source": "test"},
        audit_event_id=uuid4(),
        created_at=datetime.now(UTC),
    )


def test_review_service_create_list_and_decide_dedup() -> None:
    item = _open_item()
    session = _FakeSession([item])
    service = ReviewService(_FakeSessions(session))  # type: ignore[arg-type]

    created_id = service.create_review_item(
        kind=ReviewItemKind.DEDUP,
        summary="duplicate candidate",
        payload={"record": "row-1"},
        actor="worker",
        reason="dedup candidate",
    )
    assert isinstance(created_id, UUID)
    assert service.list_items() == (item,)
    assert service.list_items(status="OPEN", kind="DEDUP") == (item,)
    assert service.get(item.id) is item
    with pytest.raises(ValueError, match="limit"):
        service.list_items(limit=0)
    decided = service.decide(
        item.id,
        actor="operator",
        decision="RESOLVED",
        reason="confirmed duplicate",
    )
    assert decided.status == "RESOLVED"
    assert decided.decision_actor == "operator"

    with pytest.raises(ReviewNotFound):
        ReviewService(_FakeSessions(_FakeSession())).get(uuid4())  # type: ignore[arg-type]
    with pytest.raises(ReviewNotFound):
        ReviewService(_FakeSessions(_FakeSession())).decide(  # type: ignore[arg-type]
            uuid4(), actor="operator", decision="RESOLVED", reason="missing"
        )
    terminal = _open_item()
    terminal.status = "RESOLVED"
    with pytest.raises(ReviewConflict, match="terminal"):
        ReviewService(_FakeSessions(_FakeSession([terminal]))).decide(  # type: ignore[arg-type]
            terminal.id, actor="operator", decision="RESOLVED", reason="again"
        )
    with pytest.raises(ValueError, match="unsupported"):
        service.decide(
            item.id,
            actor="operator",
            decision=cast(Any, "UNKNOWN"),
            reason="bad",
        )

    with pytest.raises(ValueError, match="payload"):
        service.create_review_item(
            kind=ReviewItemKind.DEDUP,
            summary="bad payload",
            payload=cast(Any, []),
            actor="worker",
            reason="bad",
        )
    with pytest.raises(ValueError, match="summary"):
        service.create_review_item(
            kind=ReviewItemKind.DEDUP,
            summary="",
            payload={},
            actor="worker",
            reason="bad",
        )

    suspense_create_session = _FakeSession()
    suspense_create = ReviewService(_FakeSessions(suspense_create_session))  # type: ignore[arg-type]
    suspense_id = suspense_create.create_suspense(
        summary="unknown counterpart",
        payload={"record": "row-2"},
        amount_minor=123,
        reason="needs account assignment",
        suspense_reason="UNKNOWN_COUNTERPARTY",
        suspense_account_id=uuid4(),
        actor="worker",
    )
    assert isinstance(suspense_id, UUID)
    assert len(suspense_create_session.added) == 2
    with pytest.raises(ValueError, match="non-zero"):
        suspense_create.create_suspense(
            summary="invalid",
            payload={},
            amount_minor=0,
            reason="bad",
            suspense_reason="BALANCE_GAP",
            suspense_account_id=uuid4(),
            actor="worker",
        )


def test_review_service_reconciliation_and_suspense_branches() -> None:
    group_item = _open_item(ReviewItemKind.RECONCILIATION.value)
    group = type(
        "Group",
        (),
        {
            "review_item_id": group_item.id,
            "status": "PROPOSED",
            "decided_at": None,
            "decision_actor": None,
            "decision_reason": None,
        },
    )()
    session = _FakeSession([group_item, group])
    service = ReviewService(_FakeSessions(session))  # type: ignore[arg-type]
    assert (
        service.decide(
            group_item.id,
            actor="operator",
            decision="REJECTED",
            reason="not a transfer",
        ).status
        == "REJECTED"
    )
    assert group.status == "REJECTED"

    missing_group_item = _open_item(ReviewItemKind.RECONCILIATION.value)
    with pytest.raises(ReviewConflict, match="group is missing"):
        ReviewService(_FakeSessions(_FakeSession([missing_group_item]))).decide(  # type: ignore[arg-type]
            missing_group_item.id,
            actor="operator",
            decision="REJECTED",
            reason="missing child",
        )

    suspense_item = _open_item(ReviewItemKind.SUSPENSE.value)
    suspense = type(
        "Suspense",
        (),
        {
            "review_item_id": suspense_item.id,
            "status": "OPEN",
            "resolved_at": None,
            "resolution_account_id": None,
            "resolution_actor": None,
            "resolution_reason": None,
        },
    )()
    suspense_session = _FakeSession([suspense_item, suspense])
    suspense_service = ReviewService(_FakeSessions(suspense_session))  # type: ignore[arg-type]
    with pytest.raises(ReviewConflict, match="target account"):
        suspense_service.decide(
            suspense_item.id,
            actor="operator",
            decision="RESOLVED",
            reason="assigned",
        )
    suspense_session.values = [suspense_item, suspense]
    resolved = suspense_service.decide(
        suspense_item.id,
        actor="operator",
        decision="RESOLVED",
        reason="assigned",
        resolution_account_id=uuid4(),
    )
    assert resolved.status == "RESOLVED"
    assert suspense.status == "RESOLVED"

    reject_suspense_item = _open_item(ReviewItemKind.SUSPENSE.value)
    reject_suspense_service = ReviewService(
        _FakeSessions(_FakeSession([reject_suspense_item]))  # type: ignore[arg-type]
    )
    with pytest.raises(ReviewConflict, match="require an explicit"):
        reject_suspense_service.decide(
            reject_suspense_item.id,
            actor="operator",
            decision="REJECTED",
            reason="reject",
        )
    missing_suspense_item = _open_item(ReviewItemKind.SUSPENSE.value)
    with pytest.raises(ReviewConflict, match="Suspense item is missing"):
        ReviewService(_FakeSessions(_FakeSession([missing_suspense_item]))).decide(  # type: ignore[arg-type]
            missing_suspense_item.id,
            actor="operator",
            decision="RESOLVED",
            reason="missing child",
            resolution_account_id=uuid4(),
        )


def test_review_api_is_disabled_by_default_and_maps_conflicts() -> None:
    default_settings = Settings(
        database_url="postgresql+psycopg://owner@localhost/db",
        artifact_root=Path.cwd().resolve(),
    )
    with pytest.raises(HTTPException) as disabled:
        require_review_api(default_settings)
    assert disabled.value.status_code == 404

    class _ApiService:
        def list_items(self, **_kwargs: object) -> tuple[ReviewItem, ...]:
            return (_open_item(),)

        def decide(self, *_args: object, **_kwargs: object) -> ReviewItem:
            raise ReviewConflict("already terminal")

    settings = default_settings.model_copy(update={"enable_review_api": True, "env": "test"})
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_authenticated_principal] = lambda: "operator"
    app.dependency_overrides[get_review_service] = _ApiService
    try:
        client = TestClient(app)
        response = client.get("/v1/reviews")
        assert response.status_code == 200
        assert response.json()[0]["kind"] == "DEDUP"
        response = client.post(
            f"/v1/reviews/{uuid4()}/decision",
            json={"decision": "RESOLVED", "reason": "done"},
        )
        assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()
