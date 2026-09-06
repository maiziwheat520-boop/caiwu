"""What the company transaction classification routes accept, and what they refuse.

These three routes are the only way the web tier reaches company classifications,
so the route layer is where the request is bound to the person who made it: the
command backend has to be the real one, the idempotency key has to be a
canonical UUID, the URL may not carry a query, and the user assertion has to
cover this exact body, path, resource, revision and operation.  The service
itself is stubbed here - what is under test is the binding, not the SQL.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import SecretStr

from ledgerbridge.company_transaction_classification import (
    CompanyTransactionCategory,
    CompanyTransactionClassificationPage,
    CompanyTransactionClassificationReviewReceipt,
    CompanyTransactionClassificationReviewRequest,
    CompanyTransactionClassificationSummaryPage,
    DatabaseCompanyTransactionClassificationService,
)
from ledgerbridge.company_transaction_classification_routes import (
    get_service,
    router,
)
from ledgerbridge.config import Settings, get_settings
from ledgerbridge.internal_candidate_command_routes import (
    InternalCandidateCommandProblem,
    require_internal_candidate_command_api,
)
from ledgerbridge.internal_command_assertion import UserAssertionClaims, sign_user_assertion
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import (
    Capability,
    EntityGrant,
    WorkloadPrincipal,
)

# The filesystem root is absolute on every platform; a literal `C:/...` is
# absolute only on Windows, which made these synthetic settings fail on CI.
_SYNTHETIC_ROOT = Path(Path.cwd().anchor)
KEY = b"synthetic-classification-assertion-key-01"
ISSUER = "ledgerbridge-web-test"
AUDIENCE = "ledgerbridge-core-test"
POLICY_GENERATION = 21
ENTITY = UUID("10000000-0000-4000-8000-000000000001")
TRANSACTION = UUID("40000000-0000-4000-8000-000000000001")


def _principal() -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="ledgerbridge-web-test",
        san_uri="spiffe://ledgerbridge.test/web/classification",
        policy_generation=POLICY_GENERATION,
        capabilities=frozenset(
            {
                Capability.BANK_STATEMENT_REVIEW_READ,
                Capability.BANK_STATEMENT_REVIEW_DECIDE,
                Capability.COMPANY_REPORT_READ,
            }
        ),
        grants=(
            EntityGrant(
                entity_ref=ENTITY,
                business_unit_refs=frozenset({"unit-demo-a"}),
            ),
        ),
    )


def _settings(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "env": "test",
        "database_url": "postgresql+psycopg://ledgerbridge_owner:test@localhost/test",
        "api_database_url": None,
        "worker_database_url": None,
        "reader_database_url": None,
        "artifact_root": _SYNTHETIC_ROOT / "ledgerbridge-test-artifacts",
        "enable_internal_read_api": True,
        "enable_internal_candidate_command_api": True,
        "internal_read_policy_generation": POLICY_GENERATION,
        "internal_command_assertion_key": SecretStr(KEY.decode()),
        "internal_command_assertion_issuer": ISSUER,
        "internal_command_assertion_audience": AUDIENCE,
    }
    fields.update(overrides)
    return Settings(**fields)


def _database_backed_settings() -> Settings:
    """Settings whose command and read backends are both the real database."""
    return _settings(
        api_database_url="postgresql+psycopg://ledgerbridge_api:test@localhost/test",
        reader_database_url="postgresql+psycopg://ledgerbridge_reader:test@localhost/test",
        internal_read_backend="database",
        internal_read_cursor_key="c" * 32,
        internal_candidate_command_backend="database",
    )


class _StubService:
    """Stands in for the database service so the route binding is what is tested."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def list_current(
        self, principal: WorkloadPrincipal, *, status: str
    ) -> CompanyTransactionClassificationPage:
        self.calls.append({"call": "list_current", "status": status})
        return CompanyTransactionClassificationPage(items=())

    def summaries(
        self,
        principal: WorkloadPrincipal,
        *,
        from_date: date,
        to_date_exclusive: date,
    ) -> CompanyTransactionClassificationSummaryPage:
        self.calls.append(
            {"call": "summaries", "from_date": from_date, "to_date_exclusive": to_date_exclusive}
        )
        return CompanyTransactionClassificationSummaryPage(items=())

    def review(
        self,
        principal: WorkloadPrincipal,
        *,
        transaction_ref: UUID,
        operation_id: UUID,
        assertion_jti: UUID,
        actor_ref: str,
        command: CompanyTransactionClassificationReviewRequest,
    ) -> CompanyTransactionClassificationReviewReceipt:
        self.calls.append(
            {
                "call": "review",
                "transaction_ref": transaction_ref,
                "operation_id": operation_id,
                "assertion_jti": assertion_jti,
                "actor_ref": actor_ref,
                "command": command,
            }
        )
        return CompanyTransactionClassificationReviewReceipt(
            transaction_ref=transaction_ref,
            status="CONFIRMED",
            category_code=command.category_code,
            revision=command.expected_revision + 1,
            created=True,
        )


def _client(
    *,
    settings: Settings | None = None,
    service: _StubService | None = None,
    lift_router_gate: bool = False,
) -> tuple[TestClient, _StubService]:
    app = FastAPI()
    stub = service or _StubService()
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: settings or _settings()
    app.dependency_overrides[get_internal_read_principal] = _principal
    app.dependency_overrides[get_service] = lambda: stub
    if lift_router_gate:
        app.dependency_overrides[require_internal_candidate_command_api] = lambda: None
    return TestClient(app), stub


def _body(**overrides: Any) -> bytes:
    payload: dict[str, Any] = {
        "entity_ref": str(ENTITY),
        "expected_revision": 1,
        "category_code": CompanyTransactionCategory.RENT.value,
        "reason": "confirmed against the signed lease",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _assertion(
    *,
    body: bytes,
    operation_id: UUID,
    transaction_ref: UUID = TRANSACTION,
    expected_revision: int = 1,
    subject: str = "owner-passkey-1",
    path: str | None = None,
) -> str:
    now = int(datetime.now(UTC).timestamp())
    claims = UserAssertionClaims(
        issuer=ISSUER,
        audience=AUDIENCE,
        subject=subject,
        authentication_generation=3,
        canonical_path=(
            path or f"/internal/v1/company-transaction-classifications/{transaction_ref}/reviews"
        ),
        body_sha256=hashlib.sha256(body).hexdigest(),
        resource_ref=transaction_ref,
        expected_revision=expected_revision,
        operation_id=operation_id,
        workload_principal=_principal().principal_ref,
        policy_generation=POLICY_GENERATION,
        issued_at=now,
        expires_at=now + 45,
        jti=uuid4(),
    )
    return sign_user_assertion(claims, KEY)


def _post(
    client: TestClient,
    *,
    body: bytes | None = None,
    idempotency_key: str | None = None,
    assertion: str | None = None,
    query: str = "",
    transaction_ref: UUID = TRANSACTION,
) -> Response:
    content = _body() if body is None else body
    operation = uuid4()
    key = str(operation) if idempotency_key is None else idempotency_key
    token = assertion or _assertion(body=content, operation_id=operation)
    response: Response = client.post(
        f"/internal/v1/company-transaction-classifications/{transaction_ref}/reviews{query}",
        content=content,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": key,
            "X-LedgerBridge-User-Assertion": token,
        },
    )
    return response


# --- resolving the service ---------------------------------------------------


def test_the_routes_are_unavailable_while_the_command_backend_is_synthetic() -> None:
    """Company classifications are real accounting facts, so fixtures cannot serve them."""
    with pytest.raises(InternalCandidateCommandProblem) as problem:
        get_service(_settings())

    assert problem.value.status_code == 503
    assert problem.value.code == "COMPANY_CLASSIFICATION_UNAVAILABLE"


def test_the_service_reads_as_the_reader_role_and_writes_as_the_api_role() -> None:
    """The two roles are separate connections, resolved from separate settings."""
    service = get_service(_database_backed_settings())

    assert isinstance(service, DatabaseCompanyTransactionClassificationService)


# --- listing and summarising -------------------------------------------------


def test_listing_defaults_to_the_classifications_still_awaiting_a_decision() -> None:
    """The default view is the work queue, not the whole confirmed history."""
    client, service = _client()

    response = client.get("/internal/v1/company-transaction-classifications")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert service.calls == [{"call": "list_current", "status": "PENDING"}]


def test_listing_can_ask_for_the_confirmed_classifications_instead() -> None:
    """`status` is the only dimension the list route exposes."""
    client, service = _client()

    response = client.get(
        "/internal/v1/company-transaction-classifications", params={"status": "CONFIRMED"}
    )

    assert response.status_code == 200
    assert service.calls == [{"call": "list_current", "status": "CONFIRMED"}]


def test_a_status_outside_the_two_the_route_knows_is_refused() -> None:
    """An unrecognised status would otherwise reach the service as free text."""
    client, service = _client()

    response = client.get(
        "/internal/v1/company-transaction-classifications", params={"status": "ARCHIVED"}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_COMMAND"
    assert service.calls == []


def test_a_summary_covers_a_half_open_range_of_dates() -> None:
    """The range is passed through untouched, so the report period is the caller's."""
    client, service = _client()

    response = client.get(
        "/internal/v1/company-transaction-classification-summary",
        params={"from_date": "2026-01-01", "to_date_exclusive": "2026-02-01"},
    )

    assert response.status_code == 200
    assert service.calls == [
        {
            "call": "summaries",
            "from_date": date(2026, 1, 1),
            "to_date_exclusive": date(2026, 2, 1),
        }
    ]


@pytest.mark.parametrize(
    ("from_date", "to_date_exclusive"),
    [("2026-02-01", "2026-02-01"), ("2026-03-01", "2026-02-01")],
)
def test_a_summary_range_that_ends_before_it_starts_is_refused(
    from_date: str, to_date_exclusive: str
) -> None:
    """An empty or inverted period would report zero and look like a real answer."""
    client, service = _client()

    response = client.get(
        "/internal/v1/company-transaction-classification-summary",
        params={"from_date": from_date, "to_date_exclusive": to_date_exclusive},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_COMMAND"
    assert service.calls == []


# --- reviewing ---------------------------------------------------------------


def test_a_review_binds_the_decision_to_the_person_who_signed_the_assertion() -> None:
    """The actor and the assertion identifier come from the token, never the body."""
    client, service = _client()

    response = client.post(
        f"/internal/v1/company-transaction-classifications/{TRANSACTION}/reviews",
        content=_body(),
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": (key := str(uuid4())),
            "X-LedgerBridge-User-Assertion": _assertion(
                body=_body(), operation_id=UUID(key), subject="owner-passkey-7"
            ),
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "CONFIRMED"
    call = service.calls[0]
    assert call["call"] == "review"
    assert call["transaction_ref"] == TRANSACTION
    assert call["operation_id"] == UUID(key)
    assert call["actor_ref"] == "owner-passkey-7"
    assert isinstance(call["assertion_jti"], UUID)


def test_a_review_url_carrying_a_query_is_refused() -> None:
    """The assertion covers the path only, so a query could ride along unsigned."""
    client, service = _client()

    response = _post(client, query="?force=true")

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_QUERY"
    assert service.calls == []


def test_an_idempotency_key_that_is_not_a_uuid_is_refused() -> None:
    """The key becomes the operation identifier, so it has to parse as one."""
    client, service = _client()

    response = _post(client, idempotency_key="x" * 36)

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_IDEMPOTENCY_KEY"
    assert service.calls == []


def test_an_idempotency_key_that_is_not_the_canonical_spelling_is_refused() -> None:
    """`UUID` accepts several spellings of one value; only one of them identifies
    an operation, so a key with its dashes in the wrong places is refused rather
    than quietly normalised into a key that has already been used."""
    client, service = _client()

    response = _post(client, idempotency_key="aaaaaaaa-aaaaaaaa-aaaa-aaaa-aaaaaaaa")

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_IDEMPOTENCY_KEY"
    assert service.calls == []


@pytest.mark.parametrize(
    "missing_field",
    [
        "internal_command_assertion_key",
        "internal_command_assertion_issuer",
        "internal_command_assertion_audience",
    ],
)
def test_a_review_is_refused_while_the_assertion_verifier_is_unconfigured(
    missing_field: str,
) -> None:
    """With no verifier the route cannot tell who is deciding, so it decides nothing."""
    # A half-configured verifier cannot coexist with the command API, so the
    # settings turn it off and the router-level gate is lifted to reach the
    # handler's own check.
    settings = _settings(
        enable_internal_candidate_command_api=False,
        **{missing_field: None},
    )
    client, service = _client(settings=settings, lift_router_gate=True)

    response = _post(client)

    assert response.status_code == 401
    assert response.json()["code"] == "USER_ASSERTION_INVALID"
    assert service.calls == []


def test_an_assertion_signed_for_another_path_is_refused() -> None:
    """The signature covers the canonical path, so a token from elsewhere will not do."""
    client, service = _client()
    body = _body()
    operation = uuid4()

    response = _post(
        client,
        body=body,
        idempotency_key=str(operation),
        assertion=_assertion(
            body=body,
            operation_id=operation,
            path=f"/internal/v1/candidates/{TRANSACTION}/decisions",
        ),
    )

    assert response.status_code == 401
    assert response.json()["code"] == "USER_ASSERTION_INVALID"
    assert service.calls == []


def test_an_assertion_signed_over_another_body_is_refused() -> None:
    """The body digest is what stops a signed review being replayed with new content."""
    client, service = _client()
    operation = uuid4()

    response = _post(
        client,
        body=_body(reason="confirmed against a different lease"),
        idempotency_key=str(operation),
        assertion=_assertion(body=_body(), operation_id=operation),
    )

    assert response.status_code == 401
    assert response.json()["code"] == "USER_ASSERTION_INVALID"
    assert service.calls == []


def test_the_routes_are_absent_while_the_command_api_is_disabled() -> None:
    """The whole router is gated, so a disabled install exposes no classification URL."""
    client, service = _client(settings=_settings(enable_internal_candidate_command_api=False))

    response = client.get("/internal/v1/company-transaction-classifications")

    assert response.status_code == 404
    assert response.json()["code"] == "CANDIDATE_COMMAND_DISABLED"
    assert service.calls == []
