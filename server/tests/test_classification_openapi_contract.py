from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from server.tests.test_core_backend import (
    CANDIDATE_ID,
    CLASSIFICATION_GROUP_REF,
    SECOND_CANDIDATE_ID,
    ClassificationCoreClient,
    build_state,
)
from server.tests.test_payroll_openapi_contract import _component, _validate


OPENAPI_PATH = Path(__file__).resolve().parents[2] / "contracts" / "openapi.yaml"


class ClassificationOpenApiContractTests(unittest.TestCase):
    def test_openapi_describes_the_group_scope_and_atomic_batch_receipt(self) -> None:
        document = OPENAPI_PATH.read_text(encoding="utf-8")
        self.assertIn("/api/v1/candidate-classification-groups:", document)
        self.assertIn(
            "/api/v1/candidate-classification-groups/{group_ref}/decisions:",
            document,
        )
        state = build_state(ClassificationCoreClient())
        groups = state.candidate_classification_groups()
        _validate(document, _component(document, "ClassificationGroupPage"), groups)

        status, receipt = state.apply_candidate_classification_batch(
            CLASSIFICATION_GROUP_REF,
            str(uuid.uuid4()),
            {
                "source_candidate_ref": CANDIDATE_ID,
                "accounting_month": "2026-08",
                "target": {
                    "business_unit_ref": "unit-demo-a",
                    "category_code": "SETTLEMENT",
                },
                "members": [
                    {"candidate_ref": CANDIDATE_ID, "expected_revision": 1},
                    {
                        "candidate_ref": SECOND_CANDIDATE_ID,
                        "expected_revision": 1,
                    },
                ],
                "reason": "逐笔核对相似交易后整组确认",
                "acknowledged_risk_codes": ["TRANSFER_REVIEW_REQUIRED"],
            },
        )
        self.assertEqual(status, 200, receipt)
        _validate(
            document,
            _component(document, "ClassificationBatchReceipt"),
            receipt,
        )


if __name__ == "__main__":
    unittest.main()
