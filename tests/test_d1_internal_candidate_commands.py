from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import SecretStr

from ledgerbridge.candidate_contract import (
    CandidateProjection,
    CandidateStatus,
    IngestChannel,
    ReviewSummary,
    SourceProjection,
    create_candidate_aggregate,
)
from ledgerbridge.config import Settings, get_settings
from ledgerbridge.internal_candidate_command import SyntheticInternalReviewService
from ledgerbridge.internal_candidate_command_routes import (
    get_candidate_command_service,
    router,
)
from ledgerbridge.internal_command_assertion import (
    UserAssertionClaims,
    sign_user_assertion,
)
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import (
    AccountingDimensions,
    Capability,
    EntityGrant,
    WorkloadPrincipal,
)
from ledgerbridge.internal_read_routes import (
    get_synthetic_internal_read_service,
)
from ledgerbridge.internal_read_routes import router as read_router

KEY = b"synthetic-d1-user-assertion-key-0001"
ISSUER = "ledgerbridge-web-test"
AUDIENCE = "ledgerbridge-core-test"
POLICY_GENERATION = 21
ENTITY = UUID("10000000-0000-4000-8000-000000000001")
PENDING = UUID("30000000-0000-4000-8000-000000000003")
INCOMPLETE = UUID("30000000-0000-4000-8000-000000000001")
CONFLICTED = UUID("30000000-0000-4000-8000-000000000002")


def _principal() -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="ledgerbridge-web-test",
        san_uri="spiffe://ledgerbridge.test/web/review",
        policy_generation=POLICY_GENERATION,
        capabilities=frozenset(
            {
                Capability.CANDIDATE_READ,
                Capability.CANDIDATE_DECIDE,
                Capability.EVIDENCE_READ,
            }
        ),
        grants=(
            EntityGrant(
                entity_ref=ENTITY,
                business_unit_refs=frozenset({"unit-demo-a", "unit-reviewed"}),
                allow_unassigned_candidates=True,
            ),
        ),
    )


def _settings() -> Settings:
    return Settings(
        env="test",
        database_url="postgresql+psycopg://ledgerbridge_owner:test@localhost/test",
        artifact_root=Path("C:/ledgerbridge-test-artifacts"),
        enable_internal_read_api=True,
        enable_internal_candidate_command_api=True,
        internal_read_policy_generation=POLICY_GENERATION,
        internal_command_assertion_key=SecretStr(KEY.decode()),
        internal_command_assertion_issuer=ISSUER,
        internal_command_assertion_audience=AUDIENCE,
    )


def _app(
    service: SyntheticInternalReviewService | None = None,
) -> tuple[FastAPI, SyntheticInternalReviewService]:
    app = FastAPI()
    service = service or SyntheticInternalReviewService()
    app.include_router(read_router)
    app.include_router(router)
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_internal_read_principal] = _principal
    app.dependency_overrides[get_candidate_command_service] = lambda: service
    app.dependency_overrides[get_synthetic_internal_read_service] = lambda: service
    return app, service


def _body(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _assertion(
    *,
    body: bytes,
    candidate_ref: UUID,
    operation_id: UUID,
    expected_revision: int,
    subject: str = "owner-passkey-1",
    issued_at: int | None = None,
    expires_at: int | None = None,
    path: str | None = None,
) -> str:
    now = int(datetime.now(UTC).timestamp())
    issued = now if issued_at is None else issued_at
    expires = issued + 45 if expires_at is None else expires_at
    claims = UserAssertionClaims(
        issuer=ISSUER,
        audience=AUDIENCE,
        subject=subject,
        authentication_generation=3,
        canonical_path=(path or f"/internal/v1/candidates/{candidate_ref}/decisions"),
        body_sha256=hashlib.sha256(body).hexdigest(),
        resource_ref=candidate_ref,
        expected_revision=expected_revision,
        operation_id=operation_id,
        workload_principal=_principal().principal_ref,
        policy_generation=POLICY_GENERATION,
        issued_at=issued,
        expires_at=expires,
        jti=uuid4(),
    )
    return sign_user_assertion(claims, KEY)


def _post(
    client: TestClient,
    candidate_ref: UUID,
    request: dict[str, object],
    *,
    operation_id: UUID | None = None,
    assertion: str | None = None,
) -> tuple[UUID, Response]:
    operation = operation_id or uuid4()
    body = _body(request)
    revision = request["expected_revision"]
    assert type(revision) is int
    token = assertion or _assertion(
        body=body,
        candidate_ref=candidate_ref,
        operation_id=operation,
        expected_revision=revision,
    )
    response = client.post(
        f"/internal/v1/candidates/{candidate_ref}/decisions",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": str(operation),
            "X-LedgerBridge-User-Assertion": token,
        },
    )
    return operation, response


def _group_service() -> tuple[SyntheticInternalReviewService, tuple[UUID, UUID]]:
    service = SyntheticInternalReviewService()
    candidate_refs = (PENDING, CONFLICTED)
    for index, candidate_ref in enumerate(candidate_refs, start=1):
        original = next(
            item for item in service._fixture.candidates if item.candidate_ref == candidate_ref
        )
        payload = original.model_dump(mode="python")
        payload.update(
            {
                "revision": 1,
                "status": CandidateStatus.PENDING,
                "business_unit_ref": "unit-demo-a",
                "business_unit_label": "Demo unit A",
                "category_code": "SUPPLIES",
                "category_label": "Synthetic supplies",
                "amount_minor": index,
                "accounting_month": "2026-05",
                "summary": (
                    f"支付宝 | 2026-05-0{index} | 收入 | 基金收益 | "
                    "中欧基金管理有限公司 | 余额宝 | 交易成功"
                ),
                "counterparty_ref": None,
                "counterparty_class": None,
                "confidence_basis_points": 9_900,
                "source": SourceProjection(
                    ingest_channel=IngestChannel.CONTROLLED_UPLOAD,
                    source_system="alipay_export",
                    source_event_ref=original.source.source_event_ref,
                    display_label="支付宝交易",
                ),
                "blockers": (),
                "review_risks": (),
                "review_summary": ReviewSummary(event_count=0, current_revision=1),
                "updated_at": original.created_at,
                "supersedes_candidate_ref": None,
                "superseded_by_candidate_ref": None,
            }
        )
        projection = CandidateProjection.model_validate(payload)
        service._aggregates[candidate_ref] = create_candidate_aggregate(projection)
    return service, candidate_refs


def _post_group(
    client: TestClient,
    group_ref: str,
    request: dict[str, object],
    *,
    operation_id: UUID | None = None,
) -> tuple[UUID, Response]:
    operation = operation_id or uuid4()
    body = _body(request)
    members = request["members"]
    assert isinstance(members, list)
    source_ref = UUID(str(request["source_candidate_ref"]))
    source_member = next(item for item in members if item["candidate_ref"] == str(source_ref))
    revision = source_member["expected_revision"]
    assert type(revision) is int
    path = f"/internal/v1/candidate-classification-groups/{group_ref}/decisions"
    assertion = _assertion(
        body=body,
        candidate_ref=source_ref,
        operation_id=operation,
        expected_revision=revision,
        path=path,
    )
    response = client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": str(operation),
            "X-LedgerBridge-User-Assertion": assertion,
        },
    )
    return operation, response


def test_confirm_is_actor_bound_append_only_and_idempotent() -> None:
    app, _ = _app()
    request = {
        "decision": "CONFIRM",
        "expected_revision": 1,
        "reason": "synthetic operator verification",
    }
    with TestClient(app) as client:
        operation, first = _post(client, PENDING, request)
        assert first.status_code == 200
        receipt = first.json()
        assert receipt["replayed"] is False
        assert receipt["candidate"]["status"] == "CONFIRMED"
        assert receipt["candidate"]["revision"] == 2
        assert receipt["events"][0]["actor_ref"] == "owner-passkey-1"

        body = _body(request)
        replay_assertion = _assertion(
            body=body,
            candidate_ref=PENDING,
            operation_id=operation,
            expected_revision=1,
        )
        _, replay = _post(
            client,
            PENDING,
            request,
            operation_id=operation,
            assertion=replay_assertion,
        )
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True
        assert replay.json()["events"] == receipt["events"]

        events = client.get(f"/internal/v1/candidate-events?candidate_ref={PENDING}")
        assert events.status_code == 200
        assert len(events.json()["items"]) == 1
        refreshed = client.get(f"/internal/v1/candidates/{PENDING}")
        assert refreshed.json()["status"] == "CONFIRMED"


def test_classification_group_batch_is_explicit_atomic_audited_and_idempotent() -> None:
    service, candidate_refs = _group_service()
    app, _ = _app(service)
    with TestClient(app) as client:
        groups = client.get("/internal/v1/candidate-classification-groups")
        assert groups.status_code == 200
        group = next(item for item in groups.json()["items"] if item["batch_member_count"] == 2)
        assert group["conditions"]["counterparty_basis"] == "EXACT_PLATFORM_SUMMARY_V1"
        request = {
            "source_candidate_ref": str(candidate_refs[0]),
            "accounting_month": "2026-05",
            "target": {"business_unit_ref": "unit-reviewed", "category_code": "TRAVEL"},
            "members": [
                {"candidate_ref": str(candidate_ref), "expected_revision": 1}
                for candidate_ref in candidate_refs
            ],
            "reason": "reviewed the exact Yu'e Bao group",
            "acknowledged_risk_codes": [],
        }
        operation, first = _post_group(client, group["group_ref"], request)
        assert first.status_code == 200
        receipt = first.json()
        assert receipt["replayed"] is False
        assert len(receipt["results"]) == 2
        assert {item["status"] for item in receipt["results"]} == {"APPLIED"}
        assert {item["candidate"]["status"] for item in receipt["results"]} == {"CONFIRMED"}
        assert {item["candidate"]["category_code"] for item in receipt["results"]} == {"TRAVEL"}
        assert len({item["operation_id"] for item in receipt["results"]}) == 2

        _, replay = _post_group(client, group["group_ref"], request, operation_id=operation)
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True
        assert {item["status"] for item in replay.json()["results"]} == {"REPLAYED"}
        for candidate_ref in candidate_refs:
            events = client.get(
                f"/internal/v1/candidate-events?candidate_ref={candidate_ref}"
            ).json()["items"]
            assert len(events) == 1


def test_classification_group_stale_member_rolls_back_every_member() -> None:
    service, candidate_refs = _group_service()
    app, _ = _app(service)
    with TestClient(app) as client:
        group = next(
            item
            for item in client.get("/internal/v1/candidate-classification-groups").json()["items"]
            if item["batch_member_count"] == 2
        )
        request = {
            "source_candidate_ref": str(candidate_refs[0]),
            "accounting_month": "2026-05",
            "target": {"business_unit_ref": "unit-reviewed", "category_code": "TRAVEL"},
            "members": [
                {"candidate_ref": str(candidate_refs[0]), "expected_revision": 1},
                {"candidate_ref": str(candidate_refs[1]), "expected_revision": 2},
            ],
            "reason": "stale atomic batch",
            "acknowledged_risk_codes": [],
        }
        _, response = _post_group(client, group["group_ref"], request)
        assert response.status_code == 409
        for candidate_ref in candidate_refs:
            candidate = client.get(f"/internal/v1/candidates/{candidate_ref}").json()
            assert candidate["status"] == "PENDING"
            assert candidate["revision"] == 1
            assert (
                client.get(f"/internal/v1/candidate-events?candidate_ref={candidate_ref}").json()[
                    "items"
                ]
                == []
            )


def test_complete_and_conflict_decisions_preserve_frozen_state_graph() -> None:
    app, _ = _app()
    with TestClient(app) as client:
        _, completed = _post(
            client,
            INCOMPLETE,
            {
                "decision": "CORRECT_AND_CONFIRM",
                "expected_revision": 1,
                "reason": "assign the reviewed unit",
                "corrections": {"business_unit": "unit-demo-a"},
            },
        )
        assert completed.status_code == 200
        assert completed.json()["candidate"]["status"] == "CONFIRMED"
        assert completed.json()["candidate"]["revision"] == 3
        assert [item["action"] for item in completed.json()["events"]] == [
            "COMPLETE_FIELDS",
            "CONFIRM",
        ]

        _, resolved = _post(
            client,
            CONFLICTED,
            {
                "decision": "RESOLVE_CONFLICT",
                "expected_revision": 1,
                "reason": "reviewed the synthetic duplicate",
                "corrections": {"amount_minor": -12_346},
                "conflict_resolution": "use the attachment-backed record",
            },
        )
        assert resolved.status_code == 200
        assert resolved.json()["candidate"]["status"] == "CONFIRMED"
        assert resolved.json()["candidate"]["amount_minor"] == -12_346
        assert {change["field"] for change in resolved.json()["events"][0]["changes"]} == {
            "amount_minor",
            "status",
        }
        assert [item["action"] for item in resolved.json()["events"]] == [
            "RESOLVE_CONFLICT",
            "CONFIRM",
        ]


def test_pending_candidate_correction_updates_posting_fields_and_preserves_evidence() -> None:
    app, _ = _app()
    with TestClient(app) as client:
        initial = client.get(f"/internal/v1/candidates/{PENDING}").json()
        _, corrected_response = _post(
            client,
            PENDING,
            {
                "decision": "CORRECT_AND_CONFIRM",
                "expected_revision": 1,
                "reason": "reviewed all posting fields against the evidence",
                "corrections": {
                    "business_unit": "unit-reviewed",
                    "category": "TRAVEL",
                    "amount_minor": -1_999,
                    "accounting_month": "2026-09",
                },
            },
        )

        assert corrected_response.status_code == 200
        receipt = corrected_response.json()
        corrected = receipt["candidate"]
        assert corrected["status"] == "CONFIRMED"
        assert corrected["revision"] == 2
        assert (
            corrected["business_unit_ref"],
            corrected["business_unit_label"],
            corrected["category_code"],
            corrected["category_label"],
            corrected["amount_minor"],
            corrected["accounting_month"],
        ) == (
            "unit-reviewed",
            "Reviewed unit",
            "TRAVEL",
            "Reviewed travel",
            -1_999,
            "2026-09",
        )
        assert [event["action"] for event in receipt["events"]] == ["CORRECT_AND_CONFIRM"]
        event = receipt["events"][0]
        assert event["prior_projection"] == initial
        assert event["result_projection"] == corrected
        assert corrected["source"] == initial["source"]
        assert corrected["evidence"] == initial["evidence"]


def test_pending_candidate_correction_rejects_unknown_stable_refs_and_codes() -> None:
    for corrections in (
        {"business_unit": "unit-unknown"},
        {"category": "category-unknown"},
    ):
        app, _ = _app()
        with TestClient(app) as client:
            _, response = _post(
                client,
                PENDING,
                {
                    "decision": "CORRECT_AND_CONFIRM",
                    "expected_revision": 1,
                    "reason": "reject a value outside the authorized dimension catalog",
                    "corrections": corrections,
                },
            )

        assert response.status_code == 404
        assert response.json()["code"] == "RESOURCE_NOT_FOUND"


class _RetiredCurrentDimensionReviewService(SyntheticInternalReviewService):
    def __init__(self, retired_dimension: str) -> None:
        super().__init__()
        self._retired_dimension = retired_dimension

    def get_accounting_dimensions(
        self,
        principal: WorkloadPrincipal,
        *,
        entity_ref: UUID,
    ) -> AccountingDimensions:
        dimensions = super().get_accounting_dimensions(principal, entity_ref=entity_ref)
        if self._retired_dimension == "business_unit":
            return dimensions.model_copy(
                update={
                    "business_units": tuple(
                        item for item in dimensions.business_units if item.ref != "unit-demo-a"
                    )
                }
            )
        return dimensions.model_copy(
            update={
                "categories": tuple(
                    item for item in dimensions.categories if item.code != "SUPPLIES"
                )
            }
        )


def test_pending_amount_only_correction_rejects_retired_current_dimensions() -> None:
    for retired_dimension in ("business_unit", "category"):
        app, _ = _app(_RetiredCurrentDimensionReviewService(retired_dimension))
        with TestClient(app) as client:
            _, response = _post(
                client,
                PENDING,
                {
                    "decision": "CORRECT_AND_CONFIRM",
                    "expected_revision": 1,
                    "reason": "amount review cannot copy a retired accounting dimension",
                    "corrections": {"amount_minor": -1_998},
                },
            )

        assert response.status_code == 404
        assert response.json()["code"] == "RESOURCE_NOT_FOUND"


def test_assertion_request_binding_revision_and_body_actor_fail_closed() -> None:
    app, _ = _app()
    request = {
        "decision": "CONFIRM",
        "expected_revision": 1,
        "reason": "synthetic operator verification",
    }
    operation = uuid4()
    body = _body(request)
    with TestClient(app) as client:
        wrong_path = _assertion(
            body=body,
            candidate_ref=PENDING,
            operation_id=operation,
            expected_revision=1,
            path=f"/internal/v1/candidates/{INCOMPLETE}/decisions",
        )
        _, denied = _post(
            client,
            PENDING,
            request,
            operation_id=operation,
            assertion=wrong_path,
        )
        assert denied.status_code == 401
        assert denied.json()["code"] == "USER_ASSERTION_INVALID"

        now = int(datetime.now(UTC).timestamp())
        expired = _assertion(
            body=body,
            candidate_ref=PENDING,
            operation_id=operation,
            expected_revision=1,
            issued_at=now - 70,
            expires_at=now - 10,
        )
        _, denied = _post(
            client,
            PENDING,
            request,
            operation_id=operation,
            assertion=expired,
        )
        assert denied.status_code == 401

        actor_in_body = request | {"actor": "forged-owner"}
        _, invalid = _post(client, PENDING, actor_in_body)
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "INVALID_COMMAND"

        _, accepted = _post(client, PENDING, request)
        assert accepted.status_code == 200
        _, stale = _post(client, PENDING, request)
        assert stale.status_code == 409
        assert stale.json()["code"] == "STALE_REVISION"


def test_idempotency_key_cannot_change_command_or_actor() -> None:
    app, _ = _app()
    first_request = {
        "decision": "CONFIRM",
        "expected_revision": 1,
        "reason": "first command",
    }
    operation = uuid4()
    with TestClient(app) as client:
        _, first = _post(client, PENDING, first_request, operation_id=operation)
        assert first.status_code == 200

        changed = first_request | {"reason": "different command"}
        _, conflict = _post(client, PENDING, changed, operation_id=operation)
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"

        body = _body(first_request)
        other_actor = _assertion(
            body=body,
            candidate_ref=PENDING,
            operation_id=operation,
            expected_revision=1,
            subject="different-owner",
        )
        _, conflict = _post(
            client,
            PENDING,
            first_request,
            operation_id=operation,
            assertion=other_actor,
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
