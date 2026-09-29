from __future__ import annotations

import unittest

from scripts.verify_company_bank_review_scope import validate_scope


def _statements() -> list[dict[str, str]]:
    companies = ["雅阁", "逸豪", "青居客", "薇旭", "星汇", "景怡", "雅朵"]
    return [
        {
            "statement_ref": f"10000000-0000-4000-8000-{index:012d}",
            "entity_ref": f"20000000-0000-4000-8000-{min(index, 7):012d}",
            "company_name": companies[min(index, 7) - 1],
        }
        for index in range(1, 9)
    ]


def _principal(
    principal_ref: str,
    capabilities: list[str],
    entity_refs: list[str],
) -> dict[str, object]:
    return {
        "certificate_serial": "A1",
        "principal": {
            "principal_ref": principal_ref,
            "policy_generation": 10,
            "capabilities": capabilities,
            "grants": [{"entity_ref": entity_ref} for entity_ref in entity_refs],
        },
    }


def _policy(*, review_entities: list[str]) -> dict[str, object]:
    all_entities = sorted({item["entity_ref"] for item in _statements()})
    return {
        "version": "ledgerbridge.mtls-workload-policy.v2",
        "policy_generation": 10,
        "identities": [
            _principal(
                "workload:ledgerbridge-company-reports",
                ["company-report:read"],
                all_entities,
            ),
            _principal(
                "workload:ledgerbridge-company-bank-review",
                ["bank-statement-review:read", "bank-statement-review:decide"],
                review_entities,
            ),
        ],
    }


class CompanyBankReviewScopeTests(unittest.TestCase):
    def test_accepts_exact_seven_company_eight_statement_policy(self) -> None:
        statements = _statements()
        entities = sorted({item["entity_ref"] for item in statements})

        result = validate_scope(statements, _policy(review_entities=entities))

        self.assertEqual(
            result,
            {
                "ok": True,
                "statement_count": 8,
                "company_count": 7,
                "policy_generation": 10,
                "errors": [],
            },
        )

    def test_reports_named_companies_missing_from_review_identity(self) -> None:
        statements = _statements()
        entities = sorted({item["entity_ref"] for item in statements})

        result = validate_scope(statements, _policy(review_entities=entities[:-2]))

        self.assertIs(result["ok"], False)
        self.assertEqual(
            result["errors"],
            ["company bank review is missing configured companies: 景怡, 雅朵"],
        )

    def test_rejects_unapproved_review_capability(self) -> None:
        statements = _statements()
        entities = sorted({item["entity_ref"] for item in statements})
        policy = _policy(review_entities=entities)
        review = policy["identities"][1]["principal"]
        review["capabilities"].append("candidate:read")

        result = validate_scope(statements, policy)

        self.assertIs(result["ok"], False)
        self.assertEqual(
            result["errors"],
            ["company bank review capabilities are not the exact approved set"],
        )


if __name__ == "__main__":
    unittest.main()
