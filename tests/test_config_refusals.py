"""Every refusal the settings model makes, stated one at a time.

`Settings` is the only place an operator's environment is checked before
LedgerBridge agrees to start.  Each branch here is a deployment gate: if one
stops firing, a production install that is missing a reader role, an assertion
key, an operational gate or an absolute path would come up believing itself
complete.  The tests below name the refusal rather than the branch, so that a
gate which quietly disappears fails as a missing behaviour and not as a
coverage number.
"""

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from ledgerbridge.config import (
    EvidenceUnlockerRuntimeSettings,
    Settings,
    _is_private_docker_service_origin,
)

ENTITY = UUID("11111111-1111-4111-8111-111111111111")
OTHER_ENTITY = UUID("22222222-2222-4222-8222-222222222222")

API_URL = "postgresql+psycopg://ledgerbridge_api@db/app"
READER_URL = "postgresql+psycopg://ledgerbridge_reader@db/app"


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    """The smallest development install, adjusted by the caller.

    Every field the model would otherwise read from the environment is pinned,
    including the ones a test needs to be absent: CI exports a full set of
    database URLs, and a refusal that only fires on a developer's machine is not
    a refusal that has been tested.
    """
    fields: dict[str, Any] = {
        "env": "development",
        "database_url": "sqlite+pysqlite:///:memory:",
        "api_database_url": None,
        "worker_database_url": None,
        "reader_database_url": None,
        "artifact_root": tmp_path.resolve(),
    }
    fields.update(overrides)
    return Settings(**fields)


def _refuses(tmp_path: Path, match: str, **overrides: Any) -> None:
    with pytest.raises(ValidationError, match=match):
        _settings(tmp_path, **overrides)


def _read_api(**overrides: Any) -> dict[str, Any]:
    """The smallest configuration that turns the internal read API on."""
    fields: dict[str, Any] = {
        "enable_internal_read_api": True,
        "internal_read_policy_generation": 7,
    }
    fields.update(overrides)
    return fields


def _production(**overrides: Any) -> dict[str, Any]:
    """A production install of the API role, before any optional feature."""
    fields: dict[str, Any] = {
        "env": "production",
        "runtime_role": "api",
        "database_url": None,
        "api_database_url": API_URL,
    }
    fields.update(overrides)
    return fields


def _production_read_api(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    """A production install whose internal read API has cleared every R1 gate."""
    return _production(
        reader_database_url=READER_URL,
        enable_internal_read_api=True,
        internal_read_backend="database",
        internal_read_cursor_key="c" * 32,
        internal_read_policy_generation=7,
        internal_read_operational_gate="r1-production-v1",
        internal_read_transport="unix-mtls-proxy",
        internal_read_mtls_policy_path=(tmp_path / "policy.json").resolve(),
        internal_read_evidence_key_file=(tmp_path / "evidence-key.json").resolve(),
        enable_internal_read_persistent_audit=True,
        enable_internal_read_persistent_receipt=True,
        **overrides,
    )


def _payroll(**overrides: Any) -> dict[str, Any]:
    """Payroll integration with the pieces every environment demands."""
    fields: dict[str, Any] = {
        "enable_payroll_integration": True,
        "payroll_base_url": "http://payroll:8080",
        "payroll_company_mapping": {"company_demo_hotel": ENTITY},
        "payroll_bff_user_assertion_key": SecretStr("b" * 32),
        "payroll_bff_user_assertion_issuer": "web-test",
        "payroll_bff_user_assertion_audience": "core-test",
    }
    fields.update(overrides)
    return fields


def _provider_assertions(**overrides: Any) -> dict[str, Any]:
    """The provider-facing assertion identity production payroll insists on."""
    fields: dict[str, Any] = {
        "payroll_provider_workload_assertion_key": SecretStr("w" * 32),
        "payroll_provider_user_assertion_key": SecretStr("u" * 32),
        "payroll_provider_assertion_issuer": "LedgerBridge",
        "payroll_provider_assertion_audience": "PayrollVerification",
        "payroll_provider_service_subject": "payroll-provider",
    }
    fields.update(overrides)
    return fields


def _commands(**overrides: Any) -> dict[str, Any]:
    """Payroll commands with the contract, allowlist and bindings in place."""
    fields: dict[str, Any] = {
        "enable_payroll_commands": True,
        "payroll_provider_trusted_command_contract": "payroll-trusted-command/v1",
        "payroll_command_allowlist": {"VERIFY_RECEIPTS"},
        "payroll_role_bindings": {"company_demo_hotel": {"user-1": {"maker"}}},
    }
    fields.update(overrides)
    return fields


# --- the production internal read API ---------------------------------------


def test_the_production_read_api_refuses_the_synthetic_backend(tmp_path: Path) -> None:
    """Production reads come from the database role, never from fixtures."""
    _refuses(
        tmp_path,
        "requires the database reader backend",
        **_production(
            **_read_api(internal_read_operational_gate="r1-production-v1"),
        ),
    )


def test_the_production_read_api_refuses_a_transport_other_than_the_mtls_proxy(
    tmp_path: Path,
) -> None:
    """Reads must arrive over the Unix-socket mTLS proxy, not over plain HTTP."""
    _refuses(
        tmp_path,
        "requires the Unix-socket mTLS policy",
        **_production(
            **_read_api(
                internal_read_operational_gate="r1-production-v1",
                internal_read_backend="database",
            ),
        ),
    )


def test_the_production_read_api_refuses_the_mtls_proxy_without_a_policy(
    tmp_path: Path,
) -> None:
    """A transport with no policy file authorises nothing, so it is refused."""
    _refuses(
        tmp_path,
        "requires the Unix-socket mTLS policy",
        **_production(
            **_read_api(
                internal_read_operational_gate="r1-production-v1",
                internal_read_backend="database",
                internal_read_transport="unix-mtls-proxy",
            ),
        ),
    )


@pytest.mark.parametrize(
    "missing_sink",
    ["enable_internal_read_persistent_audit", "enable_internal_read_persistent_receipt"],
)
def test_the_production_read_api_refuses_a_missing_persistent_sink(
    tmp_path: Path, missing_sink: str
) -> None:
    """Both sinks are required: an unrecorded read is an unauditable read."""
    sinks: dict[str, Any] = {
        "enable_internal_read_persistent_audit": True,
        "enable_internal_read_persistent_receipt": True,
        missing_sink: False,
    }
    _refuses(
        tmp_path,
        "requires persistent audit and receipt sinks",
        **_production(
            **_read_api(
                internal_read_operational_gate="r1-production-v1",
                internal_read_backend="database",
                internal_read_transport="unix-mtls-proxy",
                internal_read_mtls_policy_path=(tmp_path / "policy.json").resolve(),
                **sinks,
            ),
        ),
    )


def test_the_production_read_api_refuses_to_start_without_the_evidence_key(
    tmp_path: Path,
) -> None:
    """Evidence stays encrypted, so the key file is part of being able to read."""
    _refuses(
        tmp_path,
        "requires the encrypted evidence key file",
        **_production(
            **_read_api(
                internal_read_operational_gate="r1-production-v1",
                internal_read_backend="database",
                internal_read_transport="unix-mtls-proxy",
                internal_read_mtls_policy_path=(tmp_path / "policy.json").resolve(),
                enable_internal_read_persistent_audit=True,
                enable_internal_read_persistent_receipt=True,
            ),
        ),
    )


def test_the_database_read_backend_refuses_to_run_without_a_cursor_key(
    tmp_path: Path,
) -> None:
    """Keyset cursors are signed; without the key they could be forged."""
    _refuses(
        tmp_path,
        "internal_read_cursor_key is required",
        **_read_api(
            internal_read_backend="database",
            reader_database_url=READER_URL,
        ),
    )


def test_a_persistent_read_audit_refuses_to_stand_alone(tmp_path: Path) -> None:
    """An audit sink with no read API to record would record nothing."""
    _refuses(
        tmp_path,
        "persistent internal read audit requires the internal read API",
        enable_internal_read_persistent_audit=True,
    )


def test_a_persistent_read_receipt_refuses_to_stand_alone(tmp_path: Path) -> None:
    """A receipt sink with no read API to receipt would receipt nothing."""
    _refuses(
        tmp_path,
        "persistent internal read receipt requires the internal read API",
        enable_internal_read_persistent_receipt=True,
    )


# --- the candidate command API ----------------------------------------------


def test_production_candidate_commands_refuse_the_synthetic_backend(
    tmp_path: Path,
) -> None:
    """A production decision must be written to the database, not to a fixture."""
    _refuses(
        tmp_path,
        "requires the database command backend",
        **_production(
            enable_internal_candidate_command_api=True,
            internal_candidate_command_operational_gate="d1-production-v1",
        ),
    )


def test_candidate_commands_refuse_to_run_without_the_read_api(tmp_path: Path) -> None:
    """Commands act on what the read API projected; without it there is no target."""
    _refuses(
        tmp_path,
        "candidate command API requires the internal read API",
        enable_internal_candidate_command_api=True,
    )


def test_database_candidate_commands_refuse_synthetic_reads(tmp_path: Path) -> None:
    """Writing to the database while reading fixtures would decide on fiction."""
    _refuses(
        tmp_path,
        "database candidate commands require database reads",
        **_read_api(
            enable_internal_candidate_command_api=True,
            internal_candidate_command_backend="database",
            internal_command_assertion_key=SecretStr("k" * 32),
            internal_command_assertion_issuer="web-test",
            internal_command_assertion_audience="core-test",
        ),
    )


def test_database_candidate_commands_refuse_to_run_without_the_api_role(
    tmp_path: Path,
) -> None:
    """The command path writes as `ledgerbridge_api`; the reader role cannot."""
    _refuses(
        tmp_path,
        "database candidate commands require database reads",
        **_read_api(
            internal_read_backend="database",
            reader_database_url=READER_URL,
            internal_read_cursor_key="c" * 32,
            enable_internal_candidate_command_api=True,
            internal_candidate_command_backend="database",
            internal_command_assertion_key=SecretStr("k" * 32),
            internal_command_assertion_issuer="web-test",
            internal_command_assertion_audience="core-test",
        ),
    )


# --- evidence unlock ---------------------------------------------------------


def _unlock(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "enable_internal_evidence_unlock": True,
        "internal_evidence_unlock_backend": "database",
        "internal_command_assertion_key": SecretStr("k" * 32),
        "internal_command_assertion_issuer": "web-test",
        "internal_command_assertion_audience": "core-test",
    }
    fields.update(overrides)
    return fields


def test_database_evidence_unlock_refuses_synthetic_reads(tmp_path: Path) -> None:
    """Unlocking real evidence against fixture reads would unlock the wrong thing."""
    _refuses(
        tmp_path,
        "database evidence unlock requires database reads",
        **_read_api(**_unlock()),
    )


def test_database_evidence_unlock_refuses_to_run_without_the_api_role(
    tmp_path: Path,
) -> None:
    """The unlock path writes its own audit trail as the API role."""
    _refuses(
        tmp_path,
        "database evidence unlock requires database reads",
        **_read_api(
            **_unlock(
                internal_read_backend="database",
                reader_database_url=READER_URL,
                internal_read_cursor_key="c" * 32,
                internal_read_evidence_key_file=(tmp_path / "evidence-key.json").resolve(),
            ),
        ),
    )


def test_database_evidence_unlock_refuses_to_run_without_the_evidence_key(
    tmp_path: Path,
) -> None:
    """Without the key file the unlocker could not decrypt anything it was handed."""
    _refuses(
        tmp_path,
        "database evidence unlock requires database reads",
        **_read_api(
            **_unlock(
                internal_read_backend="database",
                reader_database_url=READER_URL,
                internal_read_cursor_key="c" * 32,
                api_database_url=API_URL,
            ),
        ),
    )


def test_production_evidence_unlock_refuses_the_synthetic_backend(
    tmp_path: Path,
) -> None:
    """Production unlocks are recorded in the database or they do not happen."""
    _refuses(
        tmp_path,
        "requires the database unlock backend",
        **_production_read_api(
            tmp_path,
            **_unlock(
                internal_evidence_unlock_backend="synthetic",
                internal_evidence_unlock_operational_gate="u1-production-v1",
            ),
        ),
    )


# --- payroll integration -----------------------------------------------------


def test_payroll_integration_refuses_to_run_without_the_read_api(
    tmp_path: Path,
) -> None:
    """Payroll reads publications through the internal read API."""
    _refuses(
        tmp_path,
        "payroll integration requires the internal read API",
        **_payroll(),
    )


def test_payroll_disbursement_sources_refuse_an_unknown_company(
    tmp_path: Path,
) -> None:
    """A source keyed by a company payroll does not know maps money nowhere."""
    _refuses(
        tmp_path,
        "requires a known payroll company",
        **_read_api(
            **_payroll(
                payroll_disbursement_source_entities={"company_unknown": (ENTITY,)},
            ),
        ),
    )


def test_payroll_disbursement_sources_refuse_an_empty_entity_list(
    tmp_path: Path,
) -> None:
    """A company that lists no source entity has no account to disburse from."""
    _refuses(
        tmp_path,
        "requires unique source entities",
        **_read_api(
            **_payroll(
                payroll_disbursement_source_entities={"company_demo_hotel": ()},
            ),
        ),
    )


def test_payroll_disbursement_sources_refuse_a_repeated_entity(
    tmp_path: Path,
) -> None:
    """A repeated source entity would make the same account count twice."""
    _refuses(
        tmp_path,
        "requires unique source entities",
        **_read_api(
            **_payroll(
                payroll_disbursement_source_entities={
                    "company_demo_hotel": (ENTITY, OTHER_ENTITY, ENTITY),
                },
            ),
        ),
    )


@pytest.mark.parametrize(
    "missing_field",
    [
        "payroll_bff_user_assertion_key",
        "payroll_bff_user_assertion_issuer",
        "payroll_bff_user_assertion_audience",
    ],
)
def test_payroll_integration_refuses_an_incomplete_bff_contract(
    tmp_path: Path, missing_field: str
) -> None:
    """The web tier proves the acting user; a half-configured proof proves nothing."""
    _refuses(
        tmp_path,
        "requires the BFF payroll assertion contract",
        **_read_api(**_payroll(**{missing_field: None})),
    )


@pytest.mark.parametrize(
    "missing_field",
    [
        "payroll_provider_assertion_issuer",
        "payroll_provider_assertion_audience",
        "payroll_provider_service_subject",
    ],
)
def test_production_payroll_refuses_an_incomplete_provider_identity(
    tmp_path: Path, missing_field: str
) -> None:
    """The provider checks who is calling; every part of that identity is required."""
    _refuses(
        tmp_path,
        "requires provider assertion identity",
        **_production_read_api(
            tmp_path,
            **_payroll(**_provider_assertions(**{missing_field: None})),
        ),
    )


@pytest.mark.parametrize(
    ("field_name", "wrong_value"),
    [
        ("payroll_provider_assertion_issuer", "SomeoneElse"),
        ("payroll_provider_assertion_audience", "SomeOtherService"),
    ],
)
def test_production_payroll_refuses_an_identity_the_provider_will_not_accept(
    tmp_path: Path, field_name: str, wrong_value: str
) -> None:
    """Issuer and audience are fixed by the provider, not chosen by the operator."""
    _refuses(
        tmp_path,
        "must match the provider",
        **_production_read_api(
            tmp_path,
            **_payroll(**_provider_assertions(**{field_name: wrong_value})),
        ),
    )


def test_production_payroll_refuses_one_key_used_for_both_assertions(
    tmp_path: Path,
) -> None:
    """A shared key lets a workload mint user assertions and impersonate a person."""
    shared = SecretStr("s" * 32)
    _refuses(
        tmp_path,
        "assertion keys must be independent",
        **_production_read_api(
            tmp_path,
            **_payroll(
                **_provider_assertions(
                    payroll_provider_workload_assertion_key=shared,
                    payroll_provider_user_assertion_key=shared,
                ),
            ),
        ),
    )


# --- payroll commands --------------------------------------------------------


def test_payroll_commands_refuse_to_run_without_payroll_integration(
    tmp_path: Path,
) -> None:
    """There is no provider to send a command to until the integration is on."""
    _refuses(
        tmp_path,
        "payroll commands require payroll integration",
        **_commands(),
    )


def test_payroll_commands_refuse_an_unsigned_command_contract(tmp_path: Path) -> None:
    """Commands travel under the trusted contract or they do not travel."""
    _refuses(
        tmp_path,
        "require the trusted provider command contract",
        **_read_api(
            **_payroll(
                **_commands(payroll_provider_trusted_command_contract="disabled"),
            ),
        ),
    )


def test_payroll_commands_refuse_an_empty_allowlist(tmp_path: Path) -> None:
    """An empty allowlist means no command is permitted, so enabling them is a mistake."""
    _refuses(
        tmp_path,
        "require a non-empty command allowlist",
        **_read_api(**_payroll(**_commands(payroll_command_allowlist=set()))),
    )


def test_payroll_commands_refuse_to_run_without_role_bindings(tmp_path: Path) -> None:
    """Maker, checker and approver are decided server-side, never by the caller."""
    _refuses(
        tmp_path,
        "require server-side payroll role bindings",
        **_read_api(**_payroll(**_commands(payroll_role_bindings={}))),
    )


def test_production_payroll_commands_allow_only_receipt_verification(
    tmp_path: Path,
) -> None:
    """The first production command is read-only; approvals stay off until later."""
    _refuses(
        tmp_path,
        "initially allow only VERIFY_RECEIPTS",
        **_production_read_api(
            tmp_path,
            **_payroll(
                **_provider_assertions(
                    **_commands(
                        payroll_command_allowlist={"VERIFY_RECEIPTS", "BATCH_APPROVE"},
                    ),
                ),
            ),
        ),
    )


def test_payroll_commands_refuse_incomplete_provider_assertions(
    tmp_path: Path,
) -> None:
    """Outside production the provider identity is optional until commands are on."""
    _refuses(
        tmp_path,
        "require complete provider assertion settings",
        **_read_api(**_payroll(**_commands())),
    )


def test_an_allowlist_without_commands_is_refused(tmp_path: Path) -> None:
    """A populated allowlist beside disabled commands is a half-applied change."""
    _refuses(
        tmp_path,
        "allowlist must be empty while commands are disabled",
        payroll_command_allowlist={"VERIFY_RECEIPTS"},
    )


def test_payroll_test_workspaces_refuse_to_run_without_the_integration(
    tmp_path: Path,
) -> None:
    """Test workspaces are a payroll feature, not a standalone one."""
    _refuses(
        tmp_path,
        "test workspaces require payroll integration",
        enable_payroll_test_workspaces=True,
    )


# --- paths -------------------------------------------------------------------


@pytest.mark.parametrize(
    "configured_field",
    ["runner_manifest_path", "runner_verification_keys_path"],
)
def test_the_runner_manifest_and_its_keys_must_arrive_together(
    tmp_path: Path, configured_field: str
) -> None:
    """A manifest with no keys cannot be verified; keys with no manifest verify nothing."""
    _refuses(
        tmp_path,
        "must be configured together",
        **{configured_field: (tmp_path / "runner.json").resolve()},
    )


@pytest.mark.parametrize(
    "relative_field",
    ["runner_manifest_path", "runner_verification_keys_path"],
)
def test_runner_paths_refuse_to_be_relative(tmp_path: Path, relative_field: str) -> None:
    """A relative path resolves against whatever directory the process happened to start in."""
    paths: dict[str, Any] = {
        "runner_manifest_path": (tmp_path / "manifest.json").resolve(),
        "runner_verification_keys_path": (tmp_path / "keys.json").resolve(),
    }
    paths[relative_field] = Path("relative") / "runner.json"
    _refuses(tmp_path, f"{relative_field} must be an absolute path", **paths)


@pytest.mark.parametrize(
    "relative_field",
    [
        "internal_read_mtls_policy_path",
        "internal_read_evidence_key_file",
        "internal_evidence_unlock_socket_path",
    ],
)
def test_security_material_paths_refuse_to_be_relative(tmp_path: Path, relative_field: str) -> None:
    """Policy, key and socket paths decide who may read what, so they are pinned absolutely."""
    _refuses(
        tmp_path,
        f"{relative_field} must be an absolute path",
        **{relative_field: Path("relative") / "material"},
    )


def test_a_production_runner_manifest_must_declare_its_generation(
    tmp_path: Path,
) -> None:
    """Without a generation an old manifest could be replayed as the current one."""
    _refuses(
        tmp_path,
        "runner manifest generation must be explicit",
        **_production(
            runner_manifest_path=(tmp_path / "manifest.json").resolve(),
            runner_verification_keys_path=(tmp_path / "keys.json").resolve(),
        ),
    )


# --- authentication and runtime roles ----------------------------------------


@pytest.mark.parametrize("provider", ["trusted_gateway", "test"])
def test_enabled_authentication_requires_a_policy_generation(tmp_path: Path, provider: str) -> None:
    """The generation is what lets a revoked policy be recognised as revoked."""
    _refuses(
        tmp_path,
        "auth_policy_generation is required",
        auth_provider=provider,
    )


def test_the_api_role_refuses_to_run_without_its_own_database_url(
    tmp_path: Path,
) -> None:
    """The API runs as `ledgerbridge_api`; falling back to the owner URL would over-grant."""
    _refuses(
        tmp_path,
        "api_database_url is required for the api runtime role",
        runtime_role="api",
    )


def test_a_production_runtime_database_url_that_cannot_be_parsed_is_refused(
    tmp_path: Path,
) -> None:
    """An unparsable URL hides which role it would connect as, so it is refused."""
    _refuses(
        tmp_path,
        "production runtime database URL must be valid",
        **_production(api_database_url="postgresql+psycopg://ledgerbridge_api@db:role/app"),
    )


def test_a_worker_url_is_required_before_the_worker_can_resolve_one(
    tmp_path: Path,
) -> None:
    """The worker asks for its URL at startup and must be told there is none."""
    settings = _settings(
        tmp_path,
        database_url=None,
        runtime_role="api",
        api_database_url=API_URL,
    )

    with pytest.raises(ValueError, match="a worker database URL is required"):
        settings.resolved_worker_database_url()


def test_a_reader_url_is_required_before_the_reader_can_resolve_one(
    tmp_path: Path,
) -> None:
    """The reader role has no fallback: an absent reader URL is an error, not the owner URL."""
    settings = _settings(tmp_path)

    with pytest.raises(ValueError, match="a reader database URL is required"):
        settings.resolved_reader_database_url()


def test_an_absent_origin_is_not_a_private_docker_service(tmp_path: Path) -> None:
    """The origin check answers `False` for nothing at all rather than raising."""
    assert _is_private_docker_service_origin(None) is False


# --- the unlocker runtime process --------------------------------------------


def _unlocker(tmp_path: Path, **overrides: Any) -> EvidenceUnlockerRuntimeSettings:
    fields: dict[str, Any] = {
        "env": "development",
        "artifact_root": tmp_path.resolve(),
        "internal_read_evidence_key_file": (tmp_path / "evidence-key.json").resolve(),
        "internal_evidence_unlock_socket_path": (tmp_path / "unlocker.sock").resolve(),
    }
    fields.update(overrides)
    return EvidenceUnlockerRuntimeSettings(**fields)


@pytest.mark.parametrize(
    "relative_field",
    [
        "artifact_root",
        "internal_read_evidence_key_file",
        "internal_evidence_unlock_socket_path",
    ],
)
def test_unlocker_runtime_paths_refuse_to_be_relative(tmp_path: Path, relative_field: str) -> None:
    """The unlocker has no database to fall back on; its paths are its whole world."""
    with pytest.raises(ValidationError, match=f"{relative_field} must be an absolute path"):
        _unlocker(tmp_path, **{relative_field: Path("relative") / "material"})


def test_the_unlocker_output_limit_refuses_to_exceed_the_artifact_limit(
    tmp_path: Path,
) -> None:
    """An unlocker allowed to return more than an artifact may hold could smuggle bytes out."""
    with pytest.raises(ValidationError, match="output limit cannot exceed the artifact limit"):
        _unlocker(tmp_path, artifact_max_bytes=1024)
