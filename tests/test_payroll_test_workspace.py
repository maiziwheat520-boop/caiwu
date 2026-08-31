from copy import deepcopy
from urllib.error import HTTPError
from uuid import uuid4

import pytest

from ledgerbridge.payroll_integration import (
    HttpPayrollTestWorkspaceSource,
    PayrollIntegrationError,
)


class Transport:
    def __init__(self, response):
        self.response = response

    def get_json(self, *_args, **_kwargs):
        return deepcopy(self.response)

    def post_json(self, *_args, **_kwargs):
        return deepcopy(self.response)


class MissingTransport(Transport):
    status_code = 404

    def get_json(self, *_args, **_kwargs):
        cause = HTTPError("http://payroll/test", self.status_code, "missing", {}, None)
        try:
            raise cause
        except HTTPError as exc:
            raise PayrollIntegrationError("PAYROLL_PROVIDER_REJECTED", "provider rejected") from exc


def projection(company_id="company_demo", batch="batch_demo"):
    revision = "a" * 64
    materials = [
        {
            "company_id": company_id,
            "material_id": "material_old",
            "routing_status": "AUTO_TEST",
            "period": "2026-08",
            "material_type": "bank_statement",
            "payable": False,
            "submission_supported": False,
        },
        {
            "company_id": company_id,
            "material_id": "material_new",
            "routing_status": "REVIEW_REQUIRED",
            "period": "2026-09",
            "material_type": "bank_statement",
            "payable": False,
            "submission_supported": False,
        },
        {
            "company_id": company_id,
            "material_id": "material_unknown",
            "routing_status": "DATE_UNKNOWN",
            "period": None,
            "material_type": None,
            "payable": False,
            "submission_supported": False,
        },
    ]
    return {
        "schema_version": "payroll-ledgerbridge-test-projection/v1",
        "contract_version": "1.0.0",
        "data_scope": "TEST_ONLY",
        "test_batch_id": batch,
        "company_id": company_id,
        "cutoff_date": "2026-08-31",
        "workspace_revision": 1,
        "projection_revision": revision,
        "etag": f'"{revision}"',
        "generated_at": "2026-09-01T00:00:00Z",
        "payment_submission_supported": False,
        "payable": False,
        "submission_supported": False,
        "auto_test_ready": True,
        "routing_counts": {"auto_test": 1, "review_required": 1, "date_unknown": 1},
        "materials": materials,
    }


def source(response):
    entity = uuid4()
    return entity, HttpPayrollTestWorkspaceSource(
        base_url="http://payroll:4318",
        timeout_seconds=2,
        company_mapping={"company_demo": entity},
        enabled=True,
        transport=Transport(response),
    )


def test_read_accepts_cutoff_and_unknown_date_without_blocking():
    entity, adapter = source(projection())
    result = adapter.read_workspace(
        entity_ref=entity, test_batch_id="batch_demo", provider_headers={}
    )
    assert result.payload_copy()["routing_counts"] == {
        "auto_test": 1,
        "review_required": 1,
        "date_unknown": 1,
    }


def test_date_unknown_accepts_a_valid_derived_period_but_keeps_unknown_routing():
    payload = projection()
    payload["materials"][2]["period"] = "2026-08"
    entity, adapter = source(payload)
    result = adapter.read_workspace(
        entity_ref=entity, test_batch_id="batch_demo", provider_headers={}
    )
    assert result.payload_copy()["routing_counts"]["date_unknown"] == 1


def test_test_workspace_get_maps_only_explicit_provider_404_to_stable_missing():
    entity = uuid4()
    adapter = HttpPayrollTestWorkspaceSource(
        base_url="http://payroll:4318",
        timeout_seconds=2,
        company_mapping={"company_demo": entity},
        enabled=True,
        transport=MissingTransport({}),
    )
    with pytest.raises(PayrollIntegrationError) as captured:
        adapter.read_workspace(entity_ref=entity, test_batch_id="batch_demo", provider_headers={})
    assert captured.value.error_code == "PAYROLL_TEST_WORKSPACE_NOT_FOUND"


def test_test_workspace_get_keeps_other_provider_rejections_fail_closed():
    transport = MissingTransport({})
    transport.status_code = 403
    entity = uuid4()
    adapter = HttpPayrollTestWorkspaceSource(
        base_url="http://payroll:4318",
        timeout_seconds=2,
        company_mapping={"company_demo": entity},
        enabled=True,
        transport=transport,
    )
    with pytest.raises(PayrollIntegrationError) as captured:
        adapter.read_workspace(entity_ref=entity, test_batch_id="batch_demo", provider_headers={})
    assert captured.value.error_code == "PAYROLL_PROVIDER_REJECTED"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["materials"][0].update(routing_status="REVIEW_REQUIRED"),
        lambda p: p["materials"][0].update(company_id="other_company"),
        lambda p: p.update(payable=True),
        lambda p: p["routing_counts"].update(auto_test=2),
        lambda p: p.update(auto_test_ready=False),
        lambda p: p["materials"][0].update(routing_status="INVALID"),
        lambda p: p["materials"][0].update(period="2026-09"),
        lambda p: p.update(workspace_revision=0),
    ],
)
def test_projection_fails_closed_on_scope_safety_and_routing(mutate):
    payload = projection()
    mutate(payload)
    entity, adapter = source(payload)
    with pytest.raises(PayrollIntegrationError):
        adapter.read_workspace(entity_ref=entity, test_batch_id="batch_demo", provider_headers={})


def test_create_unwraps_only_valid_test_projection():
    payload = {
        "schema_version": "payroll-test-workspace-create-result/v1",
        "replayed": False,
        "projection": projection(),
    }
    entity, adapter = source(payload)
    result = adapter.create_workspace(
        entity_ref=entity, test_batch_id="batch_demo", provider_headers={}, body=b"{}"
    )
    assert result.replayed is False


def test_clear_receipt_remains_non_payable():
    payload = {
        "schema_version": "payroll-test-workspace-clear-receipt/v1",
        "data_scope": "TEST_ONLY",
        "test_batch_id": "batch_demo",
        "company_id": "company_demo",
        "cleared_workspace_revision": 2,
        "cleared_material_count": 3,
        "cleared_at": "2026-09-01T00:00:00Z",
        "payment_submission_supported": False,
        "payable": False,
        "submission_supported": False,
        "replayed": False,
    }
    entity, adapter = source(payload)
    result = adapter.clear_workspace(
        entity_ref=entity, test_batch_id="batch_demo", provider_headers={}, body=b"{}"
    )
    assert result.payload_copy()["payable"] is False


def test_test_workspace_organize_returns_only_versioned_non_payable_material_receipt():
    payload = {
        "schema_version": "payroll-test-material-organize-result/v1",
        "data_scope": "TEST_ONLY",
        "test_batch_id": "batch_demo",
        "company_id": "company_demo",
        "workspace_revision": 2,
        "projection_revision": "b" * 64,
        "material": {
            "company_id": "company_demo",
            "material_id": "material_unknown",
            "routing_status": "AUTO_TEST",
            "period": "2026-08",
            "material_type": "PAYROLL_SHEET",
            "payable": False,
            "submission_supported": False,
        },
        "payment_submission_supported": False,
        "payable": False,
        "submission_supported": False,
        "replayed": False,
    }
    entity, adapter = source(payload)
    result = adapter.organize_material(
        entity_ref=entity,
        test_batch_id="batch_demo",
        material_id="material_unknown",
        expected_workspace_revision=1,
        expected_period="2026-08",
        expected_material_type="PAYROLL_SHEET",
        provider_headers={},
        body=b"{}",
    )
    assert result.payload_copy()["material"]["period"] == "2026-08"
    assert result.payload_copy()["payable"] is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(workspace_revision=99),
        lambda payload: payload["material"].update(period="2026-01"),
        lambda payload: payload["material"].update(material_type="SUPPORTING_SCAN"),
    ],
)
def test_test_workspace_organize_receipt_is_bound_to_the_requested_change(mutate):
    payload = {
        "schema_version": "payroll-test-material-organize-result/v1",
        "data_scope": "TEST_ONLY",
        "test_batch_id": "batch_demo",
        "company_id": "company_demo",
        "workspace_revision": 2,
        "projection_revision": "b" * 64,
        "material": {
            "company_id": "company_demo",
            "material_id": "material_unknown",
            "routing_status": "AUTO_TEST",
            "period": "2026-08",
            "material_type": "PAYROLL_SHEET",
            "payable": False,
            "submission_supported": False,
        },
        "payment_submission_supported": False,
        "payable": False,
        "submission_supported": False,
        "replayed": False,
    }
    mutate(payload)
    entity, adapter = source(payload)
    with pytest.raises(PayrollIntegrationError):
        adapter.organize_material(
            entity_ref=entity,
            test_batch_id="batch_demo",
            material_id="material_unknown",
            expected_workspace_revision=1,
            expected_period="2026-08",
            expected_material_type="PAYROLL_SHEET",
            provider_headers={},
            body=b"{}",
        )


def test_test_workspace_validation_returns_only_test_review_batches():
    payload = {
        "schema_version": "payroll-test-batch-validation-result/v1",
        "data_scope": "TEST_ONLY",
        "test_batch_id": "batch_demo",
        "company_id": "company_demo",
        "workspace_revision": 2,
        "ready_batch_count": 1,
        "blocked_material_count": 0,
        "batches": [
            {
                "batch_id": "batch_demo_2026_08",
                "period": "2026-08",
                "material_count": 2,
                "payroll_sheet_count": 1,
                "supporting_material_count": 1,
                "status": "READY_FOR_TEST_REVIEW",
            }
        ],
        "payment_submission_supported": False,
        "payable": False,
        "submission_supported": False,
        "replayed": False,
    }
    entity, adapter = source(payload)
    result = adapter.validate_batches(
        entity_ref=entity,
        test_batch_id="batch_demo",
        expected_workspace_revision=2,
        provider_headers={},
        body=b"{}",
    )
    assert result.payload_copy()["ready_batch_count"] == 1
    assert result.payload_copy()["submission_supported"] is False


def test_test_workspace_validation_receipt_is_bound_to_the_requested_revision():
    payload = {
        "schema_version": "payroll-test-batch-validation-result/v1",
        "data_scope": "TEST_ONLY",
        "test_batch_id": "batch_demo",
        "company_id": "company_demo",
        "workspace_revision": 99,
        "ready_batch_count": 0,
        "blocked_material_count": 0,
        "batches": [],
        "payment_submission_supported": False,
        "payable": False,
        "submission_supported": False,
        "replayed": False,
    }
    entity, adapter = source(payload)
    with pytest.raises(PayrollIntegrationError):
        adapter.validate_batches(
            entity_ref=entity,
            test_batch_id="batch_demo",
            expected_workspace_revision=2,
            provider_headers={},
            body=b"{}",
        )


def preview_payload():
    return {
        "schema_version": "payroll-test-material-preview/v1",
        "data_scope": "TEST_ONLY",
        "test_batch_id": "batch_demo",
        "company_id": "company_demo",
        "material_id": "material_old",
        "period": "2026-08",
        "status": "READY_FOR_REVIEW",
        "line_count": 1,
        "total_net_pay_cents": 500000,
        "lines": [
            {
                "company_id": "company_demo",
                "employee_id": "emp_preview_001",
                "employee_name": "示例员工甲",
                "account_id": "acct_preview_001",
                "account_masked": "****0138",
                "payment_channel": "MYBANK",
                "base_salary_cents": 500000,
                "allowance_cents": 30000,
                "bonus_cents": 20000,
                "deduction_cents": 5000,
                "social_insurance_cents": 18000,
                "housing_fund_cents": 12000,
                "individual_income_tax_cents": 15000,
                "gross_pay_cents": 550000,
                "net_pay_cents": 500000,
                "notes": "脱敏测试材料",
            }
        ],
        "exceptions": [],
        "payment_submission_supported": False,
        "payable": False,
        "submission_supported": False,
    }


def test_test_workspace_preview_returns_masked_non_payable_lines():
    entity, adapter = source(preview_payload())
    result = adapter.preview_material(
        entity_ref=entity,
        test_batch_id="batch_demo",
        material_id="material_old",
        provider_headers={},
    )
    assert result.payload_copy()["lines"][0]["account_masked"] == "****0138"
    assert result.payload_copy()["total_net_pay_cents"] == 500000
    assert result.payload_copy()["submission_supported"] is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(company_id="company_other"),
        lambda payload: payload["lines"][0].update(account_masked="6222000000000138"),
        lambda payload: payload["lines"][0].update(account_id="acct_6222000000000138"),
        lambda payload: payload["lines"][0].update(net_pay_cents=500000.0),
        lambda payload: payload["lines"][0].update(gross_pay_cents=1),
        lambda payload: payload["lines"][0].update(net_pay_cents=499999),
        lambda payload: payload.update(total_net_pay_cents=1),
        lambda payload: payload.update(payable=True),
    ],
)
def test_test_workspace_preview_fails_closed_on_scope_money_or_payment_drift(mutate):
    payload = preview_payload()
    mutate(payload)
    entity, adapter = source(payload)
    with pytest.raises(PayrollIntegrationError):
        adapter.preview_material(
            entity_ref=entity,
            test_batch_id="batch_demo",
            material_id="material_old",
            provider_headers={},
        )


def test_test_workspace_preview_accepts_an_exact_blocking_net_mismatch_for_review():
    payload = preview_payload()
    payload["status"] = "NEEDS_HUMAN_REVIEW"
    payload["total_net_pay_cents"] = 512000
    payload["lines"][0]["net_pay_cents"] = 512000
    payload["exceptions"] = [
        {
            "code": "NET_PAY_MISMATCH",
            "severity": "BLOCKING",
            "row": 4,
            "calculated_cents": 500000,
            "stated_cents": 512000,
        }
    ]
    entity, adapter = source(payload)
    result = adapter.preview_material(
        entity_ref=entity,
        test_batch_id="batch_demo",
        material_id="material_old",
        provider_headers={},
    )
    assert result.payload_copy()["status"] == "NEEDS_HUMAN_REVIEW"
