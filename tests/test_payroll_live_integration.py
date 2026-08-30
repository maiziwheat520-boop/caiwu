from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from uuid import UUID

import pytest

from ledgerbridge.payroll_integration import HttpPayrollLiveSource, PayrollIntegrationError

ENTITY = UUID("30000000-0000-4000-8000-000000000001")
COMPANY = "company_hotel_001"
REVISION = "a" * 64
PROVIDER_HEADERS = {
    "X-LedgerBridge-Workload-Assertion": "synthetic-workload-assertion",
    "X-LedgerBridge-User-Assertion": "synthetic-provider-assertion",
}


class _Transport:
    def __init__(self, responses: Mapping[str, Mapping[str, object]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, bytes | None]] = []

    def get_json(self, url: str, **kwargs: object) -> Mapping[str, object]:
        assert kwargs["headers"] == PROVIDER_HEADERS
        self.calls.append(("GET", url, None))
        return self.responses[url]

    def post_json(self, url: str, **kwargs: object) -> Mapping[str, object]:
        assert kwargs["headers"] == PROVIDER_HEADERS
        body = kwargs["body"]
        assert isinstance(body, bytes)
        self.calls.append(("POST", url, body))
        return self.responses[url]


def _projection(*, company_id: str = COMPANY, ready: bool = True) -> dict[str, object]:
    return {
        "contract_version": "1.0.0",
        "schema_version": "payroll-ledgerbridge-live-projection/v1",
        "company_id": company_id,
        "projection_revision": REVISION,
        "etag": f'"{REVISION}"',
        "generated_at": "2026-08-30T08:00:00.000Z",
        "live_data_ready": ready,
        "payment_submission_supported": False,
        "payable": False,
        "submission_supported": False,
        "server_capabilities": {
            "projection_read": True,
            "material_review_command": True,
            "batch_command": True,
            "verification_command": True,
            "payment_submission": False,
        },
        "unassigned_material_count": 0,
        "available_evidence": [
            {
                "company_id": company_id,
                "artifact_id": "artifact_live_bank_001",
                "period": "2026-08",
                "evidence_type": "BANK_RECEIPT",
                "status": "READY_FOR_MATCHING",
                "display_label": "BANK_RECEIPT · 2026-08",
            }
        ],
        "materials": [
            {
                "company_id": company_id,
                "material_id": "material_live_payroll_001",
                "material_type": "PAYROLL_SHEET",
                "period": "2026-08",
                "status": "REVIEWED" if ready else "NEEDS_REVIEW",
                "review_revision": 1,
                "payable": False,
                "submission_supported": False,
            }
        ],
        "batches": [
            {
                "company_id": company_id,
                "batch_id": "batch_live_2026_08",
                "pay_period": "2026-08",
                "revision": 4,
                "status": "APPROVED",
                "payable": False,
                "submission_supported": False,
                "payment_submission_supported": False,
                "lines": [
                    {
                        "company_id": company_id,
                        "employee_id": "employee_live_001",
                        "employee_display": "员*工",
                        "account_id": "account_live_001",
                        "account_display": "****1234",
                        "net_pay_minor": 524000,
                    }
                ],
                "audit_closure": {
                    "audit_event_id": "audit_live_001",
                    "audit_hash": "b" * 64,
                },
            }
        ],
        "verifications": [
            {
                "company_id": company_id,
                "verification_id": "verification_live_001",
                "batch_id": "batch_live_2026_08",
                "status": "MATCHED",
                "source_artifact_ids": ["artifact_live_bank_001"],
                "results": [
                    {
                        "company_id": company_id,
                        "employee_id": "employee_live_001",
                        "employee_display": "员*工",
                        "account_id": "account_live_001",
                        "account_display": "****1234",
                        "status": "MATCHED",
                    }
                ],
                "payable": False,
                "submission_supported": False,
                "payment_submission_supported": False,
            }
        ],
        "resources": [
            {
                "company_id": company_id,
                "employee_id": "employee_live_001",
                "employee_display": "员*工",
                "account_id": "account_live_001",
                "account_display": "****1234",
            }
        ],
    }


def _source(transport: _Transport) -> HttpPayrollLiveSource:
    return HttpPayrollLiveSource(
        base_url="https://payroll.internal",
        timeout_seconds=2.5,
        company_mapping={COMPANY: ENTITY},
        enabled=True,
        transport=transport,
    )


def _projection_transport(projection: Mapping[str, object]) -> _Transport:
    return _Transport(
        {"https://payroll.internal/api/v1/ledgerbridge-projections/current": projection}
    )


def test_five_views_share_one_exact_provider_revision_and_safe_projection() -> None:
    source = _source(_projection_transport(_projection()))
    reads = [
        source.read_status(
            entity_ref=ENTITY,
            provider_headers=PROVIDER_HEADERS,
            allowed_actions=("VERIFY_RECEIPTS",),
        ),
        source.read_dashboard(entity_ref=ENTITY, provider_headers=PROVIDER_HEADERS),
        source.list_materials(entity_ref=ENTITY, provider_headers=PROVIDER_HEADERS),
        source.list_batches(entity_ref=ENTITY, provider_headers=PROVIDER_HEADERS),
        source.list_verification_results(
            entity_ref=ENTITY,
            provider_headers=PROVIDER_HEADERS,
        ),
    ]
    assert {
        (read.payload_copy()["projection_revision"], read.payload_copy()["etag"]) for read in reads
    } == {(REVISION, f'"{REVISION}"')}
    assert reads[0].payload_copy()["capabilities"] == {
        "commands_enabled": True,
        "allowed_actions": ["VERIFY_RECEIPTS"],
    }
    dashboard_summary = reads[1].payload_copy()["dashboard"]
    assert isinstance(dashboard_summary, Mapping)
    assert dashboard_summary["net_pay_minor"] == 524000


def test_not_ready_status_and_dashboard_return_only_safe_setup_summary() -> None:
    source = _source(_projection_transport(_projection(ready=False)))
    status = source.read_status(
        entity_ref=ENTITY,
        provider_headers=PROVIDER_HEADERS,
        allowed_actions=("VERIFY_RECEIPTS",),
    ).payload_copy()
    dashboard = source.read_dashboard(
        entity_ref=ENTITY,
        provider_headers=PROVIDER_HEADERS,
    ).payload_copy()
    assert status["capabilities"] == {"commands_enabled": False, "allowed_actions": []}
    assert dashboard["live_data_ready"] is False
    assert "dashboard" not in dashboard
    setup_summary = dashboard["setup_summary"]
    assert isinstance(setup_summary, Mapping)
    assert setup_summary["blocking_reason_codes"] == [
        "MATERIAL_REVIEW_REQUIRED",
        "LIVE_DATA_NOT_READY",
    ]
    with pytest.raises(PayrollIntegrationError) as captured:
        source.list_materials(entity_ref=ENTITY, provider_headers=PROVIDER_HEADERS)
    assert captured.value.error_code == "PAYROLL_LIVE_DATA_UNAVAILABLE"


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda v: v.update({"unknown_private_field": "x"}), "PAYROLL_PROVIDER_RESPONSE"),
        (lambda v: v.pop("resources"), "PAYROLL_PROVIDER_RESPONSE"),
        (
            lambda v: v["materials"][0].update({"company_id": "company_other"}),
            "PAYROLL_IDENTITY_SCOPE_MISMATCH",
        ),
        (
            lambda v: v["batches"][0]["lines"][0].update({"net_pay_minor": 524000.0}),
            "PAYROLL_PROVIDER_RESPONSE",
        ),
        (
            lambda v: v["available_evidence"][0].update({"artifact_id": "artifact_demo_receipt"}),
            "PAYROLL_DEMO_DATA_NOT_ALLOWED",
        ),
        (lambda v: v.update({"payable": True}), "PAYROLL_PAYMENT_MODE_NOT_ALLOWED"),
        (lambda v: v["server_capabilities"].pop("payment_submission"), "PAYROLL_PROVIDER_RESPONSE"),
        (lambda v: v.update({"projection_revision": 7}), "PAYROLL_PROVIDER_RESPONSE"),
    ],
)
def test_projection_rejects_unsafe_contract_variants(
    mutate: Callable[[dict[str, object]], object],
    expected_code: str,
) -> None:
    projection = copy.deepcopy(_projection())
    mutate(projection)
    source = _source(_projection_transport(projection))
    with pytest.raises(PayrollIntegrationError) as captured:
        source.list_batches(entity_ref=ENTITY, provider_headers=PROVIDER_HEADERS)
    assert captured.value.error_code == expected_code


def _receipt(*, replayed: bool = False) -> dict[str, object]:
    return {
        "schema_version": "payroll-ledgerbridge-command-receipt/v1",
        "company_id": COMPANY,
        "resource_id": "batch_live_2026_08",
        "action": "payroll.receipts.verify",
        "audit_event_id": "audit_verify_001",
        "audit_hash": "c" * 64,
        "occurred_at": "2026-08-30T08:00:00.000Z",
        "idempotency_key": "10000000-0000-4000-8000-000000000099",
        "replayed": replayed,
        "audit_closure": {
            "company_id": COMPANY,
            "resource_id": "batch_live_2026_08",
            "action": "payroll.receipts.verify",
            "actor_subject": "user_checker_001",
            "actor_id": "payroll_checker_001",
            "audit_event_id": "audit_verify_001",
            "audit_hash": "c" * 64,
            "occurred_at": "2026-08-30T08:00:00.000Z",
        },
    }


def test_verify_receipt_requires_truthful_replay_and_complete_audit_closure() -> None:
    url = "https://payroll.internal/api/v1/batches/batch_live_2026_08/verify-receipts"
    body = json.dumps({"expected_version": 4}, separators=(",", ":")).encode()
    source = _source(_Transport({url: _receipt(replayed=True)}))
    result = source.verify_receipts(
        entity_ref=ENTITY,
        batch_id="batch_live_2026_08",
        provider_body=body,
        provider_headers=PROVIDER_HEADERS,
        idempotency_key="10000000-0000-4000-8000-000000000099",
    )
    assert result.payload_copy()["replayed"] is True

    missing_audit = _receipt()
    missing_audit.pop("audit_closure")
    source = _source(_Transport({url: missing_audit}))
    with pytest.raises(PayrollIntegrationError) as captured:
        source.verify_receipts(
            entity_ref=ENTITY,
            batch_id="batch_live_2026_08",
            provider_body=body,
            provider_headers=PROVIDER_HEADERS,
            idempotency_key="10000000-0000-4000-8000-000000000099",
        )
    assert captured.value.error_code == "PAYROLL_PROVIDER_RESPONSE"


def test_live_source_maps_timeout_and_unreachable_provider_failures() -> None:
    class _Broken(_Transport):
        def get_json(self, *args: object, **kwargs: object) -> Mapping[str, object]:
            raise TimeoutError

    with pytest.raises(PayrollIntegrationError) as captured:
        _source(_Broken({})).read_status(
            entity_ref=ENTITY,
            provider_headers=PROVIDER_HEADERS,
        )
    assert captured.value.error_code == "PAYROLL_PROVIDER_TIMEOUT"

    class _Unavailable(_Transport):
        def get_json(self, *args: object, **kwargs: object) -> Mapping[str, object]:
            raise OSError

    with pytest.raises(PayrollIntegrationError) as captured:
        _source(_Unavailable({})).read_status(
            entity_ref=ENTITY,
            provider_headers=PROVIDER_HEADERS,
        )
    assert captured.value.error_code == "PAYROLL_PROVIDER_UNAVAILABLE"
