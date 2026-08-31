from copy import deepcopy
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


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["materials"][0].update(routing_status="REVIEW_REQUIRED"),
        lambda p: p["materials"][0].update(company_id="other_company"),
        lambda p: p.update(payable=True),
        lambda p: p["routing_counts"].update(auto_test=2),
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
