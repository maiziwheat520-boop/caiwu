from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest

from ledgerbridge.config import Settings
from ledgerbridge.internal_payroll_assertion import (
    HmacPayrollProviderAssertionSigner,
    PayrollUserAssertionClaims,
    PayrollUserAssertionError,
    sign_payroll_user_assertion,
    verify_payroll_user_assertion,
)
from ledgerbridge.internal_read_contract import EntityGrant, WorkloadPrincipal

ENTITY = UUID("30000000-0000-4000-8000-000000000001")
OPERATION = UUID("30000000-0000-4000-8000-000000000002")
JTI = UUID("30000000-0000-4000-8000-000000000003")
KEY = b"bff-payroll-user-assertion-key-32-bytes-minimum"
NOW = datetime(2026, 8, 30, 8, 0, tzinfo=UTC)


def _principal() -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:ledgerbridge-web",
        san_uri="spiffe://ledgerbridge.test/web",
        policy_generation=9,
        capabilities=frozenset(),
        grants=(EntityGrant(entity_ref=ENTITY, business_unit_refs=frozenset({"hotel"})),),
    )


def _body() -> bytes:
    return (
        b'{"contract_version":"ledgerbridge.payroll-batch-command.v1",'
        b'"expected_version":4,"explicitly_confirmed":true}'
    )


def _claims() -> PayrollUserAssertionClaims:
    import hashlib

    return PayrollUserAssertionClaims(
        issuer="LedgerBridge-Web",
        audience="LedgerBridge-Core",
        subject="user_payroll_checker_001",
        session_ref="session_payroll_001",
        authentication_generation=3,
        method="POST",
        canonical_path=("/internal/v1/payroll/batches/batch_alpha/verify-receipts"),
        body_sha256=hashlib.sha256(_body()).hexdigest(),
        entity_ref=ENTITY,
        action="payroll.batch.verify-receipts",
        resource_ref="batch_alpha",
        expected_revision=4,
        operation_id=OPERATION,
        workload_principal="workload:ledgerbridge-web",
        policy_generation=9,
        issued_at=int(NOW.timestamp()) - 10,
        expires_at=int(NOW.timestamp()) + 20,
        jti=JTI,
    )


def _decode_envelope(token: str) -> dict[str, object]:
    _, encoded, _ = token.split(".")
    padding = "=" * (-len(encoded) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(encoded + padding))
    assert isinstance(decoded, dict)
    return cast(dict[str, object], decoded)


def test_bff_assertion_is_request_bound_and_reissued_as_two_provider_assertions() -> None:
    claims = _claims()
    token = sign_payroll_user_assertion(claims, KEY)

    verified = verify_payroll_user_assertion(
        token,
        key=KEY,
        issuer="LedgerBridge-Web",
        audience="LedgerBridge-Core",
        method="POST",
        canonical_path=claims.canonical_path,
        body=_body(),
        entity_ref=ENTITY,
        action="payroll.batch.verify-receipts",
        resource_ref="batch_alpha",
        expected_revision=4,
        operation_id=OPERATION,
        workload_principal=_principal(),
        now=NOW,
    )
    signer = HmacPayrollProviderAssertionSigner(
        workload_key=b"provider-workload-key-32-bytes-minimum",
        user_key=b"provider-user-key-32-bytes-minimum-value",
        issuer="LedgerBridge",
        audience="PayrollVerification",
        service_subject="ledgerbridge-payroll-provider",
        clock=lambda: NOW,
        nonce_factory=iter(
            (
                UUID("30000000-0000-4000-8000-000000000004"),
                UUID("30000000-0000-4000-8000-000000000005"),
            )
        ).__next__,
    )

    headers = signer.headers(
        user=verified,
        company_id="company_hotel_001",
        provider_action="payroll.receipts.verify",
        method="POST",
        path="/api/v1/batches/batch_alpha/verify-receipts",
        body=_body(),
    )

    assert set(headers) == {
        "X-LedgerBridge-Workload-Assertion",
        "X-LedgerBridge-User-Assertion",
    }
    workload = _decode_envelope(headers["X-LedgerBridge-Workload-Assertion"])
    user = _decode_envelope(headers["X-LedgerBridge-User-Assertion"])
    assert workload["version"] == "ledgerbridge.payroll-workload-assertion.v1"
    assert workload["subject"] == "ledgerbridge-payroll-provider"
    assert workload["entity"] == str(ENTITY)
    assert workload["company"] == "company_hotel_001"
    assert workload["action"] == "payroll.receipts.verify"
    assert user["version"] == "ledgerbridge.payroll-user-assertion.v1"
    assert user["subject"] == claims.subject
    assert user["session"] == claims.session_ref
    assert user["entity"] == str(ENTITY)
    assert user["company"] == "company_hotel_001"
    assert user["action"] == "payroll.receipts.verify"
    assert workload["body_sha256"] == user["body_sha256"] == claims.body_sha256


def test_production_live_payroll_requires_private_origin_and_two_deployment_secrets() -> None:
    common: dict[str, Any] = {
        "env": "production",
        "runtime_role": "api",
        "api_database_url": "postgresql+psycopg://ledgerbridge_api@db/app",
        "artifact_root": "C:/synthetic/artifacts",
        "enable_internal_read_api": True,
        "internal_read_backend": "database",
        "reader_database_url": "postgresql+psycopg://reader.invalid/ledgerbridge",
        "internal_read_cursor_key": "c" * 32,
        "internal_read_policy_generation": 1,
        "internal_read_operational_gate": "r1-production-v1",
        "internal_read_transport": "unix-mtls-proxy",
        "internal_read_mtls_policy_path": "C:/synthetic/mtls-policy.json",
        "enable_internal_read_persistent_audit": True,
        "enable_internal_read_persistent_receipt": True,
        "internal_read_evidence_key_file": "C:/synthetic/evidence.key",
        "enable_payroll_integration": True,
        "payroll_company_mapping": {"company_hotel_001": ENTITY},
        "payroll_bff_user_assertion_key": "b" * 32,
        "payroll_bff_user_assertion_issuer": "LedgerBridge-Web",
        "payroll_bff_user_assertion_audience": "LedgerBridge-Core",
    }
    with pytest.raises(ValueError, match="private Docker service origin"):
        Settings(**common, payroll_base_url="http://127.0.0.1:4318")
    with pytest.raises(ValueError, match="two provider assertion keys"):
        Settings(**common, payroll_base_url="http://payroll-verification:4318")


def test_provider_signer_rejects_shared_workload_and_user_key() -> None:
    with pytest.raises(PayrollUserAssertionError, match="independent"):
        HmacPayrollProviderAssertionSigner(
            workload_key=b"s" * 32,
            user_key=b"s" * 32,
            issuer="LedgerBridge",
            audience="PayrollVerification",
            service_subject="ledgerbridge-payroll-provider",
        )
