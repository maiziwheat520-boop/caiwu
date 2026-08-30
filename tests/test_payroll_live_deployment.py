from pathlib import Path


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


def test_payroll_live_example_uses_vm103_non_secret_identity_context() -> None:
    root = Path(__file__).resolve().parents[1]
    example = (root / ".env.example").read_text(encoding="utf-8")
    entity_ref = "a131ef1b-e250-5a6d-82ff-cab68f767997"

    assert f'LEDGERBRIDGE_PAYROLL_COMPANY_MAPPING={{"{entity_ref}":"{entity_ref}"}}' in example
    assert "LEDGERBRIDGE_PAYROLL_PROVIDER_SERVICE_SUBJECT=workload:ledgerbridge-core" in example
    assert "LEDGERBRIDGE_ENABLE_PAYROLL_COMMANDS=false" in example
    assert "LEDGERBRIDGE_PAYROLL_ROLE_BINDINGS={}" in example
