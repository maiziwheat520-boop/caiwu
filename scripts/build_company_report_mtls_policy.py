"""Build, but never activate, one dedicated company-report mTLS policy.

The input files remain private deployment material. By default the command only
validates the transition; ``--write`` creates a new mode-0600 candidate file and
refuses to replace an existing path. Policy activation is a separate, reviewed
deployment step.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.production_mtls import (
    LoadedMtlsWorkloadPolicy,
    MtlsWorkloadIdentity,
    MtlsWorkloadPolicy,
    MtlsWorkloadPolicyV2,
    load_mtls_workload_policy,
)

MAX_REPORT_IDENTITY_BYTES = 64 * 1024
EXPECTED_REPORT_COMPANIES = 6


class CompanyReportPolicyError(RuntimeError):
    """A candidate policy could not be built without weakening authorization."""


class CompanyReportIdentityInput(BaseModel):
    """Private deployment input; capability and generation are derived here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    certificate_serial: str = Field(pattern=r"^[0-9A-F]{2,40}$")
    principal_ref: Literal["workload:ledgerbridge-company-reports"]
    san_uri: Literal["spiffe://ledgerbridge.local/web/company-reports"]
    grants: tuple[EntityGrant, ...] = Field(
        min_length=EXPECTED_REPORT_COMPANIES,
        max_length=EXPECTED_REPORT_COMPANIES,
    )

    @model_validator(mode="after")
    def grants_are_distinct_reporting_scopes(self) -> CompanyReportIdentityInput:
        entity_refs = [grant.entity_ref for grant in self.grants]
        if len(entity_refs) != len(set(entity_refs)):
            raise ValueError("company report grants must contain distinct entities")
        if any(
            grant.allow_account_registry
            or not grant.business_unit_refs
            or not grant.business_unit_ids
            or not grant.business_unit_bindings
            for grant in self.grants
        ):
            raise ValueError("company report grants require immutable reporting-unit bindings")
        return self


def build_candidate_policy(
    current: LoadedMtlsWorkloadPolicy,
    report_identity: CompanyReportIdentityInput,
    *,
    expected_generation: int,
    target_generation: int,
) -> MtlsWorkloadPolicyV2:
    """Preserve the primary identity and add one least-privilege report identity."""

    if (
        current.policy_generation != expected_generation
        or target_generation != expected_generation + 1
    ):
        raise CompanyReportPolicyError("POLICY_GENERATION_TRANSITION_INVALID")
    report_principal = WorkloadPrincipal(
        principal_ref=report_identity.principal_ref,
        san_uri=report_identity.san_uri,
        policy_generation=target_generation,
        capabilities=frozenset({Capability.COMPANY_REPORT_READ}),
        grants=report_identity.grants,
    )
    try:
        if isinstance(current, MtlsWorkloadPolicy):
            primary_principal = current.principal.model_copy(
                update={"policy_generation": target_generation}
            )
            identities: tuple[MtlsWorkloadIdentity, ...] = (
                MtlsWorkloadIdentity(
                    certificate_serial=current.certificate_serial,
                    principal=primary_principal,
                ),
                MtlsWorkloadIdentity(
                    certificate_serial=report_identity.certificate_serial,
                    principal=report_principal,
                ),
            )
        else:
            report_matches = [
                identity
                for identity in current.identities
                if identity.principal.principal_ref == report_identity.principal_ref
            ]
            if (
                len(report_matches) != 1
                or report_matches[0].certificate_serial != report_identity.certificate_serial
                or report_matches[0].principal.san_uri != report_identity.san_uri
            ):
                raise CompanyReportPolicyError("CURRENT_REPORT_IDENTITY_INVALID")
            identities = tuple(
                MtlsWorkloadIdentity(
                    certificate_serial=identity.certificate_serial,
                    principal=(
                        report_principal
                        if identity.principal.principal_ref == report_identity.principal_ref
                        else identity.principal.model_copy(
                            update={"policy_generation": target_generation}
                        )
                    ),
                )
                for identity in current.identities
            )
        return MtlsWorkloadPolicyV2(
            policy_generation=target_generation,
            identities=identities,
        )
    except ValueError as exc:
        raise CompanyReportPolicyError("CANDIDATE_POLICY_INVALID") from exc


def _read_report_identity(path: Path) -> CompanyReportIdentityInput:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise CompanyReportPolicyError("REPORT_IDENTITY_FILE_INVALID")
    size = path.stat().st_size
    if not 2 <= size <= MAX_REPORT_IDENTITY_BYTES:
        raise CompanyReportPolicyError("REPORT_IDENTITY_FILE_INVALID")
    try:
        payload = json.loads(
            path.read_bytes(),
            object_pairs_hook=_unique_object,
        )
        return CompanyReportIdentityInput.model_validate(payload)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise CompanyReportPolicyError("REPORT_IDENTITY_INVALID") from exc


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("private report identity contains a duplicate JSON key")
        result[key] = value
    return result


def _write_new_candidate(path: Path, policy: MtlsWorkloadPolicyV2) -> None:
    if not path.is_absolute() or not path.parent.is_dir() or path.parent.is_symlink():
        raise CompanyReportPolicyError("OUTPUT_PATH_INVALID")
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
        raise CompanyReportPolicyError("OUTPUT_CREATE_FAILED") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-policy", type=Path, required=True)
    parser.add_argument("--report-identity", type=Path, required=True)
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
        report_identity = _read_report_identity(args.report_identity)
        candidate = build_candidate_policy(
            current,
            report_identity,
            expected_generation=args.expected_generation,
            target_generation=args.target_generation,
        )
        if args.write:
            _write_new_candidate(args.output, candidate)
            print(
                "POLICY_WRITTEN "
                f"generation={candidate.policy_generation} "
                f"identities={len(candidate.identities)} "
                f"report_companies={len(report_identity.grants)}"
            )
        else:
            print(
                "PLAN_READY "
                f"generation={candidate.policy_generation} "
                f"identities={len(candidate.identities)} "
                f"report_companies={len(report_identity.grants)}"
            )
    except (CompanyReportPolicyError, OSError, ValueError) as exc:
        code = str(exc) if isinstance(exc, CompanyReportPolicyError) else "POLICY_BUILD_FAILED"
        print(code)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
