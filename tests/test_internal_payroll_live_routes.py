from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.internal_payroll_assertion import (
    PayrollUserAssertionClaims,
    sign_payroll_user_assertion,
)
from ledgerbridge.internal_payroll_routes import (
    InMemoryPayrollAssertionReplayStore,
    get_payroll_assertion_replay_store,
    get_payroll_live_source,
    get_payroll_test_workspace_source,
    router,
)
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.payroll_integration import (
    PayrollIntegrationError,
    PayrollLiveRead,
    PayrollTestWorkspaceResult,
)

ENTITY = UUID("10000000-0000-4000-8000-000000000001")
COMPANY = "company_live_hotel"
WORKLOAD = "workload:payroll-live-route-test"
BFF_KEY = b"b" * 32


class _LiveSource:
    def __init__(self, *, verification_capability: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.verification_capability = verification_capability
        self.command_error: PayrollIntegrationError | None = None

    def _read(self, name: str, **kwargs: object) -> PayrollLiveRead:
        self.calls.append((name, kwargs))
        data: dict[str, object] = {
            "schema_version": f"ledgerbridge.payroll-{name}.v1",
            "projection_revision": "a" * 64,
            "etag": f'"{"a" * 64}"',
        }
        if name == "status":
            allowed_actions = kwargs.get("allowed_actions", ())
            data.update(
                {
                    "live_data_ready": True,
                    "capabilities": {
                        "commands_enabled": bool(kwargs.get("allowed_actions")),
                        "allowed_actions": (
                            list(allowed_actions)
                            if self.verification_capability
                            and isinstance(allowed_actions, (list, tuple))
                            else []
                        ),
                    },
                }
            )
        if name == "verification":
            data["available_evidence"] = [
                {
                    "company_id": COMPANY,
                    "artifact_id": "artifact_live_bank_001",
                    "period": "2026-08",
                    "evidence_type": "BANK_RECEIPT",
                    "status": "READY_FOR_MATCHING",
                    "display_label": "BANK_RECEIPT · 2026-08",
                }
            ]
            data["items"] = []
        return PayrollLiveRead(
            entity_ref=ENTITY,
            company_id=COMPANY,
            payload=MappingProxyType(data),
        )

    def read_status(self, **kwargs: object) -> PayrollLiveRead:
        return self._read("status", **kwargs)

    def read_dashboard(self, **kwargs: object) -> PayrollLiveRead:
        return self._read("dashboard", **kwargs)

    def list_materials(self, **kwargs: object) -> PayrollLiveRead:
        return self._read("materials", **kwargs)

    def list_batches(self, **kwargs: object) -> PayrollLiveRead:
        return self._read("batches", **kwargs)

    def list_verification_results(self, **kwargs: object) -> PayrollLiveRead:
        return self._read("verification", **kwargs)

    def verify_receipts(self, **kwargs: object) -> PayrollLiveRead:
        self.calls.append(("verify_receipts", kwargs))
        if self.command_error is not None:
            raise self.command_error
        receipt = {
            "schema_version": "payroll-ledgerbridge-command-receipt/v1",
            "company_id": COMPANY,
            "resource_id": kwargs["batch_id"],
            "action": "payroll.receipts.verify",
            "idempotency_key": kwargs["idempotency_key"],
            "audit_event_id": "audit_live_001",
            "audit_hash": "c" * 64,
            "occurred_at": "2026-08-30T08:00:00.000Z",
            "replayed": False,
            "audit_closure": {
                "company_id": COMPANY,
                "resource_id": kwargs["batch_id"],
                "action": "payroll.receipts.verify",
                "actor_subject": "user:checker",
                "actor_id": "payroll_checker_001",
                "audit_event_id": "audit_live_001",
                "audit_hash": "c" * 64,
                "occurred_at": "2026-08-30T08:00:00.000Z",
            },
        }
        return PayrollLiveRead(
            entity_ref=ENTITY,
            company_id=COMPANY,
            payload=MappingProxyType(receipt),
        )


class _TestWorkspaceSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.company_id = COMPANY
        self.read_error: PayrollIntegrationError | None = None

    def _result(self, name: str, **kwargs: object) -> PayrollTestWorkspaceResult:
        self.calls.append((name, kwargs))
        return PayrollTestWorkspaceResult(
            entity_ref=ENTITY,
            company_id=self.company_id,
            replayed=False,
            payload=MappingProxyType(
                {
                    "schema_version": "payroll-ledgerbridge-test-projection/v1",
                    "data_scope": "TEST_ONLY",
                    "payable": False,
                    "submission_supported": False,
                }
            ),
        )

    def read_workspace(self, **kwargs: object) -> PayrollTestWorkspaceResult:
        if self.read_error is not None:
            raise self.read_error
        return self._result("read", **kwargs)

    def create_workspace(self, **kwargs: object) -> PayrollTestWorkspaceResult:
        return self._result("create", **kwargs)

    def clear_workspace(self, **kwargs: object) -> PayrollTestWorkspaceResult:
        return self._result("clear", **kwargs)


def _settings(*, commands: bool = True) -> Settings:
    return Settings(
        env="test",
        runtime_role="migrate",
        database_url="postgresql+psycopg://synthetic.invalid/ledgerbridge",
        artifact_root=Path.cwd() / "synthetic-artifacts",
        enable_internal_read_api=True,
        internal_read_policy_generation=1,
        enable_payroll_integration=True,
        payroll_base_url="http://127.0.0.1:4318",
        payroll_company_mapping={COMPANY: ENTITY},
        payroll_bff_user_assertion_key=SecretStr(BFF_KEY.decode()),
        payroll_bff_user_assertion_issuer="web-test",
        payroll_bff_user_assertion_audience="core-test",
        payroll_provider_workload_assertion_key=SecretStr("w" * 32),
        payroll_provider_user_assertion_key=SecretStr("u" * 32),
        payroll_provider_assertion_issuer="LedgerBridge",
        payroll_provider_assertion_audience="PayrollVerification",
        payroll_provider_service_subject=WORKLOAD,
        enable_payroll_commands=commands,
        enable_payroll_test_workspaces=True,
        payroll_provider_trusted_command_contract=(
            "payroll-trusted-command/v1" if commands else "disabled"
        ),
        payroll_command_allowlist=(frozenset({"VERIFY_RECEIPTS"}) if commands else frozenset()),
        payroll_role_bindings=(
            {COMPANY: {"user:checker": frozenset({"checker"})}} if commands else {}
        ),
    )


def _principal() -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref=WORKLOAD,
        san_uri="spiffe://ledgerbridge.test/payroll-live-route-test",
        policy_generation=1,
        capabilities=frozenset({Capability.PAYROLL_LIVE_READ, Capability.PAYROLL_COMMAND}),
        grants=(EntityGrant(entity_ref=ENTITY, business_unit_refs=frozenset({"unit-a"})),),
    )


def _client(
    *,
    commands: bool = True,
    verification_capability: bool = True,
) -> tuple[TestClient, _LiveSource]:
    app = FastAPI()
    app.include_router(router)
    source = _LiveSource(verification_capability=verification_capability)
    replay_store = InMemoryPayrollAssertionReplayStore()
    settings = _settings(commands=commands)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_internal_read_principal] = _principal
    app.dependency_overrides[get_payroll_live_source] = lambda: source
    app.dependency_overrides[get_payroll_assertion_replay_store] = lambda: replay_store
    return TestClient(app), source


def _test_workspace_client() -> tuple[TestClient, _TestWorkspaceSource]:
    app = FastAPI()
    app.include_router(router)
    source = _TestWorkspaceSource()
    app.dependency_overrides[get_settings] = lambda: _settings()
    app.dependency_overrides[get_internal_read_principal] = _principal
    app.dependency_overrides[get_payroll_test_workspace_source] = lambda: source
    app.dependency_overrides[get_payroll_assertion_replay_store] = lambda: (
        InMemoryPayrollAssertionReplayStore()
    )
    return TestClient(app), source


def _assertion(
    *,
    path: str,
    action: str,
    resource_ref: str,
    body: bytes = b"",
    operation_id: UUID | None = None,
    expected_revision: int | None = None,
) -> str:
    now = datetime.now(UTC)
    return sign_payroll_user_assertion(
        PayrollUserAssertionClaims(
            issuer="web-test",
            audience="core-test",
            subject="user:checker",
            session_ref="session:real-browser-session",
            authentication_generation=1,
            method="POST" if operation_id else "GET",
            canonical_path=path,
            body_sha256=hashlib.sha256(body).hexdigest(),
            entity_ref=ENTITY,
            action=action,  # type: ignore[arg-type]
            resource_ref=resource_ref,
            expected_revision=expected_revision,
            operation_id=operation_id,
            workload_principal=WORKLOAD,
            policy_generation=1,
            issued_at=int(now.timestamp()),
            expires_at=int((now + timedelta(seconds=30)).timestamp()),
            jti=uuid4(),
        ),
        BFF_KEY,
    )


def test_five_live_reads_require_request_bound_human_assertions() -> None:
    client, source = _client()
    cases = [
        ("status", "payroll.status.read", "payroll-status"),
        ("dashboard", "payroll.dashboard.read", "payroll-dashboard"),
        ("materials", "payroll.materials.list", "payroll-materials"),
        ("batches", "payroll.batches.list", "payroll-batches"),
        ("verification", "payroll.verification.list", "payroll-verification"),
    ]
    revisions = set()
    for view, action, resource in cases:
        path = f"/internal/v1/payroll/{view}"
        response = client.get(
            path,
            headers={
                "X-LedgerBridge-User-Assertion": _assertion(
                    path=path,
                    action=action,
                    resource_ref=resource,
                )
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["contract_version"] == "ledgerbridge.payroll-read.v1"
        assert payload["entity_ref"] == str(ENTITY)
        assert payload["company_id"] == COMPANY
        revisions.add((payload["data"]["projection_revision"], payload["data"]["etag"]))
    assert len(revisions) == 1
    assert len(source.calls) == 5


def test_receipt_verification_translates_only_server_controlled_provider_fields() -> None:
    client, source = _client()
    batch_id = "batch_live_2026_08"
    path = f"/internal/v1/payroll/batches/{batch_id}/verify-receipts"
    operation_id = uuid4()
    body = json.dumps(
        {
            "contract_version": "ledgerbridge.payroll-receipt-verification-command.v1",
            "expected_revision": 4,
            "explicitly_confirmed": True,
            "source_artifact_ids": ["artifact_live_bank_001"],
            "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
        },
        separators=(",", ":"),
    ).encode()
    response = client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": str(operation_id),
            "X-LedgerBridge-User-Assertion": _assertion(
                path=path,
                action="payroll.batch.verify-receipts",
                resource_ref=batch_id,
                body=body,
                operation_id=operation_id,
                expected_revision=4,
            ),
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["contract_version"] == "ledgerbridge.payroll-command-result.v1"
    assert payload["action"] == "payroll.batch.verify-receipts"
    assert payload["resource_ref"] == batch_id
    assert payload["replayed"] is False
    call = next(kwargs for name, kwargs in source.calls if name == "verify_receipts")
    provider_body_raw = call["provider_body"]
    assert isinstance(provider_body_raw, bytes)
    provider_body = json.loads(provider_body_raw)
    assert provider_body == {
        "expected_version": 4,
        "explicit_human_approval": True,
        "idempotency_key": str(operation_id),
        "payment_submission_allowed": False,
        "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
        "source_artifact_ids": ["artifact_live_bank_001"],
    }
    reads: list[Mapping[str, str]] = []
    for name, kwargs in source.calls:
        if name in {"status", "verification"}:
            provider_headers = kwargs["provider_headers"]
            assert isinstance(provider_headers, Mapping)
            reads.append(provider_headers)
    assert len(reads) == 2
    assert reads[0] != reads[1]


def test_verify_refuses_provider_without_advertised_command_capability() -> None:
    client, source = _client(verification_capability=False)
    batch_id = "batch_live_2026_08"
    path = f"/internal/v1/payroll/batches/{batch_id}/verify-receipts"
    operation_id = uuid4()
    body = json.dumps(
        {
            "contract_version": "ledgerbridge.payroll-receipt-verification-command.v1",
            "expected_revision": 4,
            "explicitly_confirmed": True,
            "source_artifact_ids": ["artifact_live_bank_001"],
            "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
        },
        separators=(",", ":"),
    ).encode()
    response = client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": str(operation_id),
            "X-LedgerBridge-User-Assertion": _assertion(
                path=path,
                action="payroll.batch.verify-receipts",
                resource_ref=batch_id,
                body=body,
                operation_id=operation_id,
                expected_revision=4,
            ),
        },
    )
    assert response.status_code == 403
    assert response.json()["code"] == "PAYROLL_PROVIDER_CAPABILITY_REQUIRED"
    assert all(name != "verify_receipts" for name, _ in source.calls)


def test_provider_version_conflict_remains_http_409_at_the_core_boundary() -> None:
    client, source = _client()
    source.command_error = PayrollIntegrationError(
        "PAYROLL_VERSION_CONFLICT",
        "provider rejected a stale payroll revision",
    )
    batch_id = "batch_live_2026_08"
    path = f"/internal/v1/payroll/batches/{batch_id}/verify-receipts"
    operation_id = uuid4()
    body = json.dumps(
        {
            "contract_version": "ledgerbridge.payroll-receipt-verification-command.v1",
            "expected_revision": 4,
            "explicitly_confirmed": True,
            "source_artifact_ids": ["artifact_live_bank_001"],
            "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
        },
        separators=(",", ":"),
    ).encode()

    response = client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": str(operation_id),
            "X-LedgerBridge-User-Assertion": _assertion(
                path=path,
                action="payroll.batch.verify-receipts",
                resource_ref=batch_id,
                body=body,
                operation_id=operation_id,
                expected_revision=4,
            ),
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "PAYROLL_VERSION_CONFLICT"


def test_command_gate_and_assertion_replay_fail_closed() -> None:
    client, _ = _client(commands=False)
    disabled = client.post("/internal/v1/payroll/batches/batch_live_2026_08/verify-receipts")
    assert disabled.status_code == 404
    assert disabled.json()["code"] == "PAYROLL_COMMAND_DISABLED"
    path = "/internal/v1/payroll/status"
    token = _assertion(
        path=path,
        action="payroll.status.read",
        resource_ref="payroll-status",
    )
    assert client.get(path, headers={"X-LedgerBridge-User-Assertion": token}).status_code == 200
    replay = client.get(path, headers={"X-LedgerBridge-User-Assertion": token})
    assert replay.status_code == 401
    assert replay.json()["code"] == "PAYROLL_USER_ASSERTION_INVALID"


# TEST_ONLY workspace routes use the same request-bound assertion contract.
def test_test_workspace_read_create_and_clear_are_request_bound() -> None:
    client, source = _test_workspace_client()
    batch_id = "batch_test_2026_08"
    read_path = f"/internal/v1/payroll/test-workspaces/{batch_id}"
    read = client.get(
        read_path,
        headers={
            "X-LedgerBridge-User-Assertion": _assertion(
                path=read_path, action="payroll.test_workspace.read", resource_ref=batch_id
            )
        },
    )
    assert read.status_code == 200, read.text
    assert read.json()["contract_version"] == "ledgerbridge.payroll-test-workspace-read.v1"

    operation_id = uuid4()
    create_path = "/internal/v1/payroll/test-workspaces"
    create_body = json.dumps(
        {
            "schema_version": "payroll-test-workspace-create-request/v1",
            "test_batch_id": batch_id,
            "expected_store_revision": 0,
            "cutoff_date": "2026-08-31",
            "idempotency_key": str(operation_id),
        },
        separators=(",", ":"),
    ).encode()
    create = client.post(
        create_path,
        content=create_body,
        headers={
            "Content-Type": "application/json",
            "X-LedgerBridge-User-Assertion": _assertion(
                path=create_path,
                action="payroll.test_workspace.create",
                resource_ref=batch_id,
                body=create_body,
                operation_id=operation_id,
                expected_revision=0,
            ),
        },
    )
    assert create.status_code == 200, create.text

    operation_id = uuid4()
    clear_path = f"{read_path}/clear"
    clear_body = json.dumps(
        {
            "schema_version": "payroll-test-workspace-clear-request/v1",
            "expected_workspace_revision": 1,
            "idempotency_key": str(operation_id),
            "explicitly_confirmed": True,
        },
        separators=(",", ":"),
    ).encode()
    clear = client.post(
        clear_path,
        content=clear_body,
        headers={
            "Content-Type": "application/json",
            "X-LedgerBridge-User-Assertion": _assertion(
                path=clear_path,
                action="payroll.test_workspace.clear",
                resource_ref=batch_id,
                body=clear_body,
                operation_id=operation_id,
                expected_revision=1,
            ),
        },
    )
    assert clear.status_code == 200, clear.text
    assert [name for name, _ in source.calls] == ["read", "create", "clear"]


def test_test_workspace_assertions_reject_wrong_action_path_and_resource() -> None:
    client, source = _test_workspace_client()
    batch_id = "batch_test_2026_08"
    path = f"/internal/v1/payroll/test-workspaces/{batch_id}"
    invalid = [
        _assertion(path=path, action="payroll.dashboard.read", resource_ref=batch_id),
        _assertion(
            path="/internal/v1/payroll/test-workspaces/wrong_batch",
            action="payroll.test_workspace.read",
            resource_ref=batch_id,
        ),
        _assertion(path=path, action="payroll.test_workspace.read", resource_ref="wrong_batch"),
    ]
    for assertion in invalid:
        response = client.get(path, headers={"X-LedgerBridge-User-Assertion": assertion})
        assert response.status_code == 401
    assert source.calls == []


def test_test_workspace_rejects_provider_company_scope_mismatch() -> None:
    client, source = _test_workspace_client()
    source.company_id = "company_other"
    batch_id = "batch_test_2026_08"
    path = f"/internal/v1/payroll/test-workspaces/{batch_id}"
    response = client.get(
        path,
        headers={
            "X-LedgerBridge-User-Assertion": _assertion(
                path=path, action="payroll.test_workspace.read", resource_ref=batch_id
            )
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "PAYROLL_IDENTITY_SCOPE_MISMATCH"


def test_test_workspace_missing_is_preserved_as_core_404_for_autocreate() -> None:
    client, source = _test_workspace_client()
    source.read_error = PayrollIntegrationError(
        "PAYROLL_TEST_WORKSPACE_NOT_FOUND", "workspace missing"
    )
    batch_id = "batch_test_2026_08"
    path = f"/internal/v1/payroll/test-workspaces/{batch_id}"
    response = client.get(
        path,
        headers={
            "X-LedgerBridge-User-Assertion": _assertion(
                path=path,
                action="payroll.test_workspace.read",
                resource_ref=batch_id,
            )
        },
    )
    assert response.status_code == 404
    assert response.json()["code"] == "PAYROLL_TEST_WORKSPACE_NOT_FOUND"


def test_test_workspace_commands_reject_wrong_action_path_and_resource() -> None:
    client, source = _test_workspace_client()
    batch_id = "batch_test_2026_08"
    create_path = "/internal/v1/payroll/test-workspaces"
    operation_id = uuid4()
    create_body = json.dumps(
        {
            "schema_version": "payroll-test-workspace-create-request/v1",
            "test_batch_id": batch_id,
            "expected_store_revision": 0,
            "cutoff_date": "2026-08-31",
            "idempotency_key": str(operation_id),
        },
        separators=(",", ":"),
    ).encode()
    bad_create = _assertion(
        path=create_path,
        action="payroll.test_workspace.clear",
        resource_ref=batch_id,
        body=create_body,
        operation_id=operation_id,
        expected_revision=0,
    )
    response = client.post(
        create_path,
        content=create_body,
        headers={
            "Content-Type": "application/json",
            "X-LedgerBridge-User-Assertion": bad_create,
        },
    )
    assert response.status_code == 401

    operation_id = uuid4()
    clear_path = f"/internal/v1/payroll/test-workspaces/{batch_id}/clear"
    clear_body = json.dumps(
        {
            "schema_version": "payroll-test-workspace-clear-request/v1",
            "expected_workspace_revision": 1,
            "idempotency_key": str(operation_id),
            "explicitly_confirmed": True,
        },
        separators=(",", ":"),
    ).encode()
    bad_clear = _assertion(
        path=f"/internal/v1/payroll/test-workspaces/{batch_id}/wrong",
        action="payroll.test_workspace.clear",
        resource_ref="wrong_batch",
        body=clear_body,
        operation_id=operation_id,
        expected_revision=1,
    )
    response = client.post(
        clear_path,
        content=clear_body,
        headers={
            "Content-Type": "application/json",
            "X-LedgerBridge-User-Assertion": bad_clear,
        },
    )
    assert response.status_code == 401
    assert source.calls == []
