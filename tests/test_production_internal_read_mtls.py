from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from ledgerbridge.config import Settings
from ledgerbridge.internal_read_contract import (
    Capability,
    EntityGrant,
    WorkloadPrincipal,
)
from ledgerbridge.production_mtls import (
    MtlsPolicyError,
    MtlsWorkloadIdentity,
    MtlsWorkloadPolicy,
    MtlsWorkloadPolicyV2,
    UnixSocketMtlsVerifier,
    load_mtls_workload_policy,
    verify_configured_mtls_principal,
)

ENTITY = UUID("10000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _principal() -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:ledgerbridge-web",
        san_uri="spiffe://ledgerbridge.local/web/review",
        policy_generation=31,
        capabilities=frozenset(
            {
                Capability.CANDIDATE_READ,
                Capability.CANDIDATE_DECIDE,
                Capability.EVIDENCE_READ,
                Capability.RECONCILIATION_READ,
            }
        ),
        grants=(
            EntityGrant(
                entity_ref=ENTITY,
                business_unit_refs=frozenset({"hotel-operations"}),
                allow_unassigned_candidates=True,
            ),
        ),
    )


def _policy() -> MtlsWorkloadPolicy:
    return MtlsWorkloadPolicy(
        certificate_serial="71A09F2C",
        policy_generation=31,
        principal=_principal(),
    )


def _report_principal() -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:ledgerbridge-company-reports",
        san_uri="spiffe://ledgerbridge.local/web/company-reports",
        policy_generation=31,
        capabilities=frozenset({Capability.COMPANY_REPORT_READ}),
        grants=(
            EntityGrant(
                entity_ref=UUID("10000000-0000-4000-8000-000000000002"),
                business_unit_refs=frozenset({"company-unit"}),
                allow_unassigned_candidates=True,
            ),
        ),
    )


def _policy_v2() -> MtlsWorkloadPolicyV2:
    return MtlsWorkloadPolicyV2(
        policy_generation=31,
        identities=(
            MtlsWorkloadIdentity(
                certificate_serial="71A09F2C",
                principal=_principal(),
            ),
            MtlsWorkloadIdentity(
                certificate_serial="81B10A3D",
                principal=_report_principal(),
            ),
        ),
    )


def _scope(
    *,
    client: object = None,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, object]:
    return {
        "type": "http",
        "client": client,
        "headers": headers
        or [
            (b"x-ledgerbridge-mtls-verified", b"SUCCESS"),
            (b"x-ledgerbridge-client-san", b"spiffe://ledgerbridge.local/web/review"),
            (b"x-ledgerbridge-client-serial", b"71A09F2C"),
        ],
    }


def test_unix_socket_mtls_verifier_accepts_only_exact_gateway_identity() -> None:
    verified = UnixSocketMtlsVerifier(_policy(), clock=lambda: NOW)(_scope())
    assert verified is not None
    assert verified.principal == _principal()
    assert verified.policy_generation == 31
    assert verified.issued_at == NOW

    assert (
        UnixSocketMtlsVerifier(_policy(), clock=lambda: NOW)(_scope(client=("127.0.0.1", 45000)))
        is None
    )
    for name, value in (
        (b"x-ledgerbridge-mtls-verified", b"FAILED"),
        (b"x-ledgerbridge-client-san", b"spiffe://ledgerbridge.local/attacker"),
        (b"x-ledgerbridge-client-serial", b"00000000"),
    ):
        headers = list(cast(list[tuple[bytes, bytes]], _scope()["headers"]))
        headers = [(key, value if key == name else current) for key, current in headers]
        assert UnixSocketMtlsVerifier(_policy(), clock=lambda: NOW)(_scope(headers=headers)) is None


def test_unix_socket_mtls_verifier_rejects_duplicate_or_noncanonical_headers() -> None:
    base = list(cast(list[tuple[bytes, bytes]], _scope()["headers"]))
    for headers in (
        [*base, (b"x-ledgerbridge-client-san", b"spiffe://ledgerbridge.local/web/review")],
        [(b"X-LedgerBridge-Mtls-Verified", b"SUCCESS"), *base],
        [*base, (b"x-forwarded-client-cert", b"forged")],
    ):
        assert UnixSocketMtlsVerifier(_policy(), clock=lambda: NOW)(_scope(headers=headers)) is None


def test_v2_policy_selects_two_independent_certificate_principals() -> None:
    report_scope = _scope(
        headers=[
            (b"x-ledgerbridge-mtls-verified", b"SUCCESS"),
            (
                b"x-ledgerbridge-client-san",
                b"spiffe://ledgerbridge.local/web/company-reports",
            ),
            (b"x-ledgerbridge-client-serial", b"81B10A3D"),
        ]
    )

    primary = UnixSocketMtlsVerifier(_policy_v2(), clock=lambda: NOW)(_scope())
    report = UnixSocketMtlsVerifier(_policy_v2(), clock=lambda: NOW)(report_scope)

    assert primary is not None and primary.principal == _principal()
    assert report is not None and report.principal == _report_principal()

    crossed = cast(list[tuple[bytes, bytes]], report_scope["headers"])
    crossed = [
        (name, b"71A09F2C" if name == b"x-ledgerbridge-client-serial" else value)
        for name, value in crossed
    ]
    assert UnixSocketMtlsVerifier(_policy_v2(), clock=lambda: NOW)(_scope(headers=crossed)) is None


def test_mtls_policy_loader_is_bounded_stable_and_generation_checked(tmp_path: Path) -> None:
    policy_path = tmp_path / "web-mtls-policy.json"
    policy_path.write_text(
        json.dumps(_policy().model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    loaded = load_mtls_workload_policy(
        policy_path.resolve(),
        expected_policy_generation=31,
        require_root_owner=False,
    )
    assert loaded == _policy()

    with pytest.raises(MtlsPolicyError, match="generation"):
        load_mtls_workload_policy(
            policy_path.resolve(),
            expected_policy_generation=32,
            require_root_owner=False,
        )


def test_mtls_policy_loader_accepts_bounded_v2_identity_sets(tmp_path: Path) -> None:
    policy_path = tmp_path / "multi-workload-policy.json"
    policy_path.write_text(
        json.dumps(_policy_v2().model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )

    loaded = load_mtls_workload_policy(
        policy_path.resolve(),
        expected_policy_generation=31,
        require_root_owner=False,
    )

    assert loaded == _policy_v2()

    link = tmp_path / "linked-policy.json"
    try:
        link.symlink_to(policy_path)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(MtlsPolicyError, match="regular file"):
        load_mtls_workload_policy(
            link,
            expected_policy_generation=31,
            require_root_owner=False,
        )


def test_mtls_policy_rejects_generation_drift_and_unknown_fields() -> None:
    with pytest.raises(ValueError, match="generation"):
        MtlsWorkloadPolicy(
            certificate_serial="71A09F2C",
            principal=_principal().model_copy(update={"policy_generation": 30}),
            policy_generation=31,
        )
    with pytest.raises(ValueError, match="extra"):
        MtlsWorkloadPolicy.model_validate(
            {
                "version": "ledgerbridge.mtls-workload-policy.v1",
                "certificate_serial": "71A09F2C",
                "policy_generation": 31,
                "principal": _principal().model_dump(mode="json"),
                "actor": "attacker",
            }
        )

    for identities, message in (
        (
            (
                _policy_v2().identities[0],
                _policy_v2().identities[0],
            ),
            "serials",
        ),
        (
            (
                _policy_v2().identities[0],
                _policy_v2().identities[1].model_copy(update={"principal": _principal()}),
            ),
            "principal refs",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            MtlsWorkloadPolicyV2(policy_generation=31, identities=identities)


def test_internal_ingress_binds_each_workload_to_a_distinct_port_and_san() -> None:
    configuration = (
        Path(__file__).parents[1] / "docker" / "internal-ingress.nginx.conf"
    ).read_text(encoding="utf-8")

    assert "8443 spiffe://ledgerbridge.local/web-review;" in configuration
    assert "8444 spiffe://ledgerbridge.local/web/company-reports;" in configuration
    assert "listen 8443 ssl;" in configuration
    assert "listen 8444 ssl;" in configuration
    assert "proxy_set_header X-LedgerBridge-Client-SAN $ledgerbridge_client_san;" in configuration


def test_configured_verifier_stays_closed_without_complete_runtime_policy(
    tmp_path: Path,
) -> None:
    disabled = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        artifact_root=tmp_path.resolve(),
    )
    assert verify_configured_mtls_principal(_scope(), disabled) is None

    missing = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        artifact_root=tmp_path.resolve(),
        enable_internal_read_api=True,
        internal_read_policy_generation=31,
        internal_read_transport="unix-mtls-proxy",
        internal_read_mtls_policy_path=(tmp_path / "missing.json").resolve(),
    )
    assert verify_configured_mtls_principal(_scope(), missing) is None
