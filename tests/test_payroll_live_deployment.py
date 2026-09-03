from pathlib import Path

import yaml


def test_payroll_live_compose_reuses_core_network_and_defaults_commands_closed() -> None:
    root = Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.payroll-live.yml").read_text(encoding="utf-8")

    assert "http://payroll-verification:4318" in compose
    assert "payroll-provider-link" not in compose
    assert "networks:" not in compose
    assert "LEDGERBRIDGE_ENABLE_PAYROLL_COMMANDS:-false" in compose
    assert "LEDGERBRIDGE_PAYROLL_COMMAND_ALLOWLIST:-[]" in compose
    assert "LEDGERBRIDGE_PAYROLL_PROVIDER_WORKLOAD_ASSERTION_KEY" in compose
    assert "LEDGERBRIDGE_PAYROLL_PROVIDER_USER_ASSERTION_KEY" in compose
    assert "LEDGERBRIDGE_PAYROLL_BFF_USER_ASSERTION_KEY" in compose
    assert "PAYROLL_PROVIDER_WORKLOAD_ASSERTION_KEY: replace" not in compose
    assert "PAYROLL_PROVIDER_USER_ASSERTION_KEY: replace" not in compose


def test_core_review_stack_cannot_omit_payroll_live_environment() -> None:
    root = Path(__file__).resolve().parents[1]
    core_review = yaml.safe_load(
        (root / "docker-compose.core-review.yml").read_text(encoding="utf-8")
    )
    payroll_live = yaml.safe_load(
        (root / "docker-compose.payroll-live.yml").read_text(encoding="utf-8")
    )

    core_environment = core_review["services"]["internal-reader"]["environment"]
    payroll_environment = payroll_live["services"]["internal-reader"]["environment"]

    assert {key: core_environment.get(key) for key in payroll_environment} == payroll_environment
    assert core_environment["LEDGERBRIDGE_ENABLE_PAYROLL_INTEGRATION"] == "true"
    assert core_environment["LEDGERBRIDGE_ENABLE_PAYROLL_COMMANDS"] == (
        "${LEDGERBRIDGE_ENABLE_PAYROLL_COMMANDS:-false}"
    )


def test_payroll_live_example_uses_synthetic_non_secret_identity_context() -> None:
    root = Path(__file__).resolve().parents[1]
    example = (root / ".env.example").read_text(encoding="utf-8")
    entity_ref = "00000000-0000-4000-8000-000000000001"
    company_id = "00000000-0000-4000-8000-000000000002"

    assert f'LEDGERBRIDGE_PAYROLL_COMPANY_MAPPING={{"{entity_ref}":"{company_id}"}}' in example
    assert "LEDGERBRIDGE_PAYROLL_PROVIDER_SERVICE_SUBJECT=workload:ledgerbridge-core" in example
    assert "LEDGERBRIDGE_ENABLE_PAYROLL_COMMANDS=false" in example
    assert "LEDGERBRIDGE_PAYROLL_ROLE_BINDINGS={}" in example
