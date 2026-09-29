from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
)
from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.internal_read_routes import require_internal_read_api
from ledgerbridge.payroll_workbench import (
    PayrollWorkbenchIssue,
    PayrollWorkbenchLine,
    PayrollWorkbenchView,
)
from ledgerbridge.payroll_workbench_routes import (
    get_payroll_workbench_service,
    require_payroll_workbench,
    require_payroll_workbench_read,
    router,
)

ENTITY = UUID("11111111-1111-4111-8111-111111111111")


class _Service:
    def __init__(self, view: PayrollWorkbenchView) -> None:
        self.view = view

    def get(self, *, entity_ref: UUID, pay_period: str) -> PayrollWorkbenchView:
        assert entity_ref == ENTITY
        assert pay_period == "2026-08"
        return self.view


def _principal(entity_ref: UUID = ENTITY) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="web",
        san_uri="spiffe://ledgerbridge.test/web",
        policy_generation=1,
        capabilities=frozenset({Capability.PAYROLL_LIVE_READ}),
        grants=(
            EntityGrant(
                entity_ref=entity_ref,
                business_unit_refs=frozenset({"hotel"}),
            ),
        ),
    )


def _view() -> PayrollWorkbenchView:
    line = PayrollWorkbenchLine(
        line_ref=uuid4(),
        employee_ref=uuid4(),
        employee_name="测试员工",
        employee_type="REGULAR",
        location="星汇",
        job_group="前台",
        attendance_days="31",
        payment_channel="MYBANK",
        payee_name="测试收款人",
        account_masked="****1234",
        memo="8月工资 现金已另发1000",
        net_amount_minor=600_000,
        cash_amount_minor=100_000,
        supplemental_amount_minor=15_000,
        bank_amount_minor=500_000,
    )
    return PayrollWorkbenchView(
        entity_ref=ENTITY,
        batch_ref=uuid4(),
        batch_version_ref=uuid4(),
        pay_period="2026-08",
        reconciliation_month="2026-08",
        revision=1,
        status="LOCKED",
        rules_version="rules-2026-08-v1",
        content_sha256="ab" * 32,
        line_count=1,
        net_amount_minor=600_000,
        cash_amount_minor=100_000,
        supplemental_amount_minor=15_000,
        bank_amount_minor=500_000,
        lines=(line,),
        issues=(
            PayrollWorkbenchIssue(
                issue_code="REVIEWED", message="已复核", line_ref=line.line_ref, resolved=True
            ),
        ),
    )


def _client(principal: WorkloadPrincipal) -> TestClient:
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[require_internal_read_api] = lambda: None
    application.dependency_overrides[require_payroll_workbench] = lambda: None
    application.dependency_overrides[require_payroll_workbench_read] = lambda: principal
    application.dependency_overrides[get_payroll_workbench_service] = lambda: _Service(_view())
    application.dependency_overrides[get_payroll_assertion_replay_store] = lambda: (
        InMemoryPayrollAssertionReplayStore()
    )
    application.dependency_overrides[get_settings] = lambda: Settings(
        env="test",
        runtime_role="migrate",
        database_url="postgresql+psycopg://synthetic.invalid/ledgerbridge",
        artifact_root=Path.cwd() / "synthetic-artifacts",
        enable_internal_read_api=True,
        internal_read_policy_generation=1,
        payroll_bff_user_assertion_key=SecretStr("b" * 32),
        payroll_bff_user_assertion_issuer="web-test",
        payroll_bff_user_assertion_audience="core-test",
    )
    return TestClient(application)


def _headers(entity_ref: UUID = ENTITY, period: str = "2026-08") -> dict[str, str]:
    now = datetime.now(UTC)
    token = sign_payroll_user_assertion(
        PayrollUserAssertionClaims(
            issuer="web-test",
            audience="core-test",
            subject="user:checker",
            session_ref="browser-session",
            authentication_generation=1,
            method="GET",
            canonical_path=f"/internal/v1/payroll/workbench/{entity_ref}/{period}",
            body_sha256=hashlib.sha256(b"").hexdigest(),
            entity_ref=entity_ref,
            action="payroll.workbench.read",
            resource_ref=period,
            workload_principal="web",
            policy_generation=1,
            issued_at=int(now.timestamp()),
            expires_at=int((now + timedelta(seconds=30)).timestamp()),
            jti=uuid4(),
        ),
        b"b" * 32,
    )
    return {"X-LedgerBridge-User-Assertion": token}


def test_workbench_returns_masked_account_and_database_totals() -> None:
    response = _client(_principal()).get(
        f"/internal/v1/payroll/workbench/{ENTITY}/2026-08", headers=_headers()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["bank_amount_minor"] == 500_000
    assert body["supplemental_amount_minor"] == 15_000
    assert body["lines"][0]["account_masked"] == "****1234"
    assert "account_number" not in body["lines"][0]
    assert response.headers["Cache-Control"] == "no-store"


def test_workbench_hides_ungranted_entity_and_rejects_bad_period() -> None:
    other = uuid4()
    assert (
        _client(_principal(other)).get(
            f"/internal/v1/payroll/workbench/{ENTITY}/2026-08", headers=_headers()
        ).status_code
        == 404
    )
    assert (
        _client(_principal()).get(
            f"/internal/v1/payroll/workbench/{ENTITY}/2026-13", headers=_headers(period="2026-13")
        ).status_code
        == 400
    )


def test_workbench_requires_request_bound_human_assertion() -> None:
    client = _client(_principal())
    path = f"/internal/v1/payroll/workbench/{ENTITY}/2026-08"
    assert client.get(path).status_code == 422
    assert client.get(path, headers=_headers(period="2026-07")).status_code == 401
