"""Build, but never activate, one dedicated cash-reconciliation mTLS policy.

The new principal derives its immutable scope from the already reviewed Web
personal grant and company-report grants.  The private input contains only the
new certificate identity.  By default the command validates the transition;
``--write`` creates a new mode-0600 candidate and never replaces a file.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.production_mtls import (
    LoadedMtlsWorkloadPolicy,
    MtlsWorkloadIdentity,
    MtlsWorkloadPolicyV2,
    load_mtls_workload_policy,
)

MAX_IDENTITY_BYTES = 16 * 1024
EXPECTED_COMPANY_GRANTS = 7
PRIMARY_PRINCIPAL_REF = "workload:ledgerbridge-web"
PRIMARY_SAN_URI = "spiffe://ledgerbridge.local/web-review"
REPORT_PRINCIPAL_REF = "workload:ledgerbridge-company-reports"
REPORT_SAN_URI = "spiffe://ledgerbridge.local/web/company-reports"


class CashReconciliationPolicyError(RuntimeError):
    """A candidate policy could not be built without weakening authorization."""


class CashReconciliationIdentityInput(BaseModel):
    """Private deployment input; capabilities and grants are derived here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    certificate_serial: str = Field(pattern=r"^[0-9A-F]{2,40}$")
    principal_ref: Literal["workload:ledgerbridge-cash-reconciliation"]
    san_uri: Literal["spiffe://ledgerbridge.local/web/cash-reconciliation"]


def _single_identity(
    current: MtlsWorkloadPolicyV2,
    principal_ref: str,
    *,
    error_code: str,
) -> MtlsWorkloadIdentity:
    matches = [
        identity
        for identity in current.identities
        if identity.principal.principal_ref == principal_ref
    ]
    if len(matches) != 1:
        raise CashReconciliationPolicyError(error_code)
    return matches[0]


def _derived_grants(current: MtlsWorkloadPolicyV2) -> tuple[EntityGrant, ...]:
    primary = _single_identity(
        current,
        PRIMARY_PRINCIPAL_REF,
        error_code="PRIMARY_SCOPE_INVALID",
    )
    if primary.principal.san_uri != PRIMARY_SAN_URI or not {
        Capability.RECONCILIATION_READ,
        Capability.LEDGER_READ,
    }.issubset(primary.principal.capabilities):
        raise CashReconciliationPolicyError("PRIMARY_SCOPE_INVALID")
    personal_grants = tuple(
        grant
        for grant in primary.principal.grants
        if grant.business_unit_refs and grant.business_unit_ids and grant.business_unit_bindings
    )
    if len(personal_grants) != 1:
        raise CashReconciliationPolicyError("PRIMARY_SCOPE_INVALID")

    report = _single_identity(
        current,
        REPORT_PRINCIPAL_REF,
        error_code="COMPANY_SCOPE_INVALID",
    )
    if report.principal.san_uri != REPORT_SAN_URI or report.principal.capabilities != frozenset(
        {Capability.COMPANY_REPORT_READ}
    ):
        raise CashReconciliationPolicyError("COMPANY_SCOPE_INVALID")
    company_grants = report.principal.grants
    if (
        len(company_grants) != EXPECTED_COMPANY_GRANTS
        or len({grant.entity_ref for grant in company_grants}) != EXPECTED_COMPANY_GRANTS
        or any(
            grant.allow_account_registry
            or not grant.business_unit_refs
            or not grant.business_unit_ids
            or not grant.business_unit_bindings
            for grant in company_grants
        )
    ):
        raise CashReconciliationPolicyError("COMPANY_SCOPE_INVALID")

    grants = (*personal_grants, *company_grants)
    if len({grant.entity_ref for grant in grants}) != len(grants):
        raise CashReconciliationPolicyError("CANDIDATE_POLICY_INVALID")
    return grants


def build_candidate_policy(
    current: LoadedMtlsWorkloadPolicy,
    reconciliation_identity: CashReconciliationIdentityInput,
    *,
    expected_generation: int,
    target_generation: int,
) -> MtlsWorkloadPolicyV2:
    """Add one least-privilege reconciliation identity to the current v2 policy."""

    if (
        current.policy_generation != expected_generation
        or target_generation != expected_generation + 1
    ):
        raise CashReconciliationPolicyError("POLICY_GENERATION_TRANSITION_INVALID")
    if not isinstance(current, MtlsWorkloadPolicyV2):
        raise CashReconciliationPolicyError("CURRENT_POLICY_V2_REQUIRED")
    if any(
        identity.principal.principal_ref == reconciliation_identity.principal_ref
        for identity in current.identities
    ):
        raise CashReconciliationPolicyError("CURRENT_RECONCILIATION_IDENTITY_EXISTS")

    principal = WorkloadPrincipal(
        principal_ref=reconciliation_identity.principal_ref,
        san_uri=reconciliation_identity.san_uri,
        policy_generation=target_generation,
        capabilities=frozenset({Capability.RECONCILIATION_READ, Capability.LEDGER_READ}),
        grants=_derived_grants(current),
    )
    try:
        identities = (
            *(
                MtlsWorkloadIdentity(
                    certificate_serial=identity.certificate_serial,
                    principal=identity.principal.model_copy(
                        update={"policy_generation": target_generation}
                    ),
                )
                for identity in current.identities
            ),
            MtlsWorkloadIdentity(
                certificate_serial=reconciliation_identity.certificate_serial,
                principal=principal,
            ),
        )
        return MtlsWorkloadPolicyV2(
            policy_generation=target_generation,
            identities=identities,
        )
    except ValueError as exc:
        raise CashReconciliationPolicyError("CANDIDATE_POLICY_INVALID") from exc


def _read_identity(path: Path) -> CashReconciliationIdentityInput:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise CashReconciliationPolicyError("RECONCILIATION_IDENTITY_FILE_INVALID")
    if not 2 <= path.stat().st_size <= MAX_IDENTITY_BYTES:
        raise CashReconciliationPolicyError("RECONCILIATION_IDENTITY_FILE_INVALID")
    try:
        payload = json.loads(path.read_bytes(), object_pairs_hook=_unique_object)
        return CashReconciliationIdentityInput.model_validate(payload)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise CashReconciliationPolicyError("RECONCILIATION_IDENTITY_INVALID") from exc


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("private reconciliation identity contains a duplicate JSON key")
        result[key] = value
    return result


def _write_new_candidate(path: Path, policy: MtlsWorkloadPolicyV2) -> None:
    if not path.is_absolute() or not path.parent.is_dir() or path.parent.is_symlink():
        raise CashReconciliationPolicyError("OUTPUT_PATH_INVALID")
    content = (
        json.dumps(
            policy.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("candidate policy write failed")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise CashReconciliationPolicyError("OUTPUT_CREATE_FAILED") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-policy", type=Path, required=True)
    parser.add_argument("--reconciliation-identity", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-generation", type=int, required=True)
    parser.add_argument("--target-generation", type=int, required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        current = load_mtls_workload_policy(
            args.current_policy,
            expected_policy_generation=args.expected_generation,
            require_root_owner=False,
        )
        identity = _read_identity(args.reconciliation_identity)
        candidate = build_candidate_policy(
            current,
            identity,
            expected_generation=args.expected_generation,
            target_generation=args.target_generation,
        )
        if args.write:
            _write_new_candidate(args.output, candidate)
            outcome = "POLICY_WRITTEN"
        else:
            outcome = "PLAN_READY"
        print(
            f"{outcome} generation={candidate.policy_generation} "
            f"identities={len(candidate.identities)} reconciliation_scopes="
            f"{len(candidate.identities[-1].principal.grants)}"
        )
    except (CashReconciliationPolicyError, OSError, ValueError) as exc:
        code = str(exc) if isinstance(exc, CashReconciliationPolicyError) else "POLICY_BUILD_FAILED"
        print(code)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
