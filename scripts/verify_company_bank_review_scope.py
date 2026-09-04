from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


COMPANY_REPORT_PRINCIPAL = "workload:ledgerbridge-company-reports"
COMPANY_REVIEW_PRINCIPAL = "workload:ledgerbridge-company-bank-review"
EXPECTED_REPORT_CAPABILITIES = {"company-report:read"}
EXPECTED_REVIEW_CAPABILITIES = {
    "bank-statement-review:read",
    "bank-statement-review:decide",
}


def _principal(policy: dict[str, Any], principal_ref: str) -> dict[str, Any] | None:
    matches = [
        identity.get("principal")
        for identity in policy.get("identities", [])
        if isinstance(identity, dict)
        and isinstance(identity.get("principal"), dict)
        and identity["principal"].get("principal_ref") == principal_ref
    ]
    return matches[0] if len(matches) == 1 else None


def _entity_refs(principal: dict[str, Any]) -> set[str]:
    return {
        str(grant["entity_ref"])
        for grant in principal.get("grants", [])
        if isinstance(grant, dict) and isinstance(grant.get("entity_ref"), str)
    }


def validate_scope(
    statements: object,
    policy: object,
    *,
    expected_statement_count: int = 8,
    expected_company_count: int = 7,
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(statements, list):
        return {"ok": False, "errors": ["statement mapping must be a list"]}
    if not isinstance(policy, dict):
        return {"ok": False, "errors": ["policy must be an object"]}

    valid_statements = [
        item
        for item in statements
        if isinstance(item, dict)
        and all(
            isinstance(item.get(field), str) and bool(item[field].strip())
            for field in ("statement_ref", "entity_ref", "company_name")
        )
    ]
    if len(valid_statements) != len(statements):
        errors.append("statement mapping contains an invalid item")

    statement_refs = {str(item["statement_ref"]) for item in valid_statements}
    configured_entities = {str(item["entity_ref"]) for item in valid_statements}
    company_by_entity = {
        str(item["entity_ref"]): str(item["company_name"])
        for item in valid_statements
    }
    if len(valid_statements) != expected_statement_count:
        errors.append(
            f"configured statement count is {len(valid_statements)}, expected {expected_statement_count}"
        )
    if len(statement_refs) != len(valid_statements):
        errors.append("statement references must be unique")
    if len(configured_entities) != expected_company_count:
        errors.append(
            f"configured company count is {len(configured_entities)}, expected {expected_company_count}"
        )

    report = _principal(policy, COMPANY_REPORT_PRINCIPAL)
    review = _principal(policy, COMPANY_REVIEW_PRINCIPAL)
    if report is None:
        errors.append("company report principal must appear exactly once")
    if review is None:
        errors.append("company bank review principal must appear exactly once")

    root_generation = policy.get("policy_generation")
    if type(root_generation) is not int or root_generation < 1:
        errors.append("policy generation must be a positive integer")

    for label, principal, expected_capabilities in (
        ("company report", report, EXPECTED_REPORT_CAPABILITIES),
        ("company bank review", review, EXPECTED_REVIEW_CAPABILITIES),
    ):
        if principal is None:
            continue
        capabilities = {
            str(item) for item in principal.get("capabilities", []) if isinstance(item, str)
        }
        if capabilities != expected_capabilities:
            errors.append(f"{label} capabilities are not the exact approved set")
        if principal.get("policy_generation") != root_generation:
            errors.append(f"{label} policy generation does not match the root policy")

        granted_entities = _entity_refs(principal)
        missing = configured_entities - granted_entities
        extra = granted_entities - configured_entities
        if missing:
            names = sorted(company_by_entity[item] for item in missing)
            errors.append(f"{label} is missing configured companies: {', '.join(names)}")
        if extra:
            errors.append(f"{label} has {len(extra)} unconfigured company grant(s)")

    return {
        "ok": not errors,
        "statement_count": len(valid_statements),
        "company_count": len(configured_entities),
        "policy_generation": root_generation,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that company statement configuration and production workload scopes agree."
    )
    parser.add_argument("--statements", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--expected-statements", type=int, default=8)
    parser.add_argument("--expected-companies", type=int, default=7)
    args = parser.parse_args()

    result = validate_scope(
        json.loads(args.statements.read_text(encoding="utf-8")),
        json.loads(args.policy.read_text(encoding="utf-8")),
        expected_statement_count=args.expected_statements,
        expected_company_count=args.expected_companies,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
