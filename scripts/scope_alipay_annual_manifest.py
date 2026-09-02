"""Bind a verified Alipay-only annual manifest to one opaque source account."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path
from uuid import UUID, uuid5

from ledgerbridge.controlled_import import SourceManifest, load_source_manifest

_NAMESPACE = UUID("75f6c5ce-5e8b-48f4-a285-86837a1c7b8b")


class AnnualManifestScopeError(RuntimeError):
    """The annual manifest cannot be safely account-scoped."""


def scope_alipay_annual_manifest(
    *, source_manifest: Path, source_account_ref: UUID, output_directory: Path
) -> Path:
    manifest, _ = load_source_manifest(source_manifest)
    if output_directory.exists():
        raise AnnualManifestScopeError("output directory already exists")
    if len(manifest.evidence) != 1 or manifest.candidate_links or manifest.welfare_benefit_facts:
        raise AnnualManifestScopeError("annual Alipay manifest must contain one unlinked evidence")
    if not manifest.candidates or any(
        item.source_system != "alipay_export" for item in manifest.candidates
    ):
        raise AnnualManifestScopeError("annual manifest must contain only Alipay candidates")

    evidence = manifest.evidence[0]
    evidence_ref = _scoped(source_account_ref, "evidence", evidence.evidence_ref)
    scoped_evidence = evidence.model_copy(update={"evidence_ref": evidence_ref})
    scoped_candidates = tuple(
        item.model_copy(
            update={
                "candidate_ref": _scoped(source_account_ref, "candidate", item.candidate_ref),
                "operation_id": _scoped(source_account_ref, "operation", item.operation_id),
                "source_event_ref": _scoped(
                    source_account_ref, "source-event", item.source_event_ref
                ),
                "evidence_refs": (evidence_ref,),
            }
        )
        for item in manifest.candidates
    )
    scoped = SourceManifest.model_validate(
        manifest.model_dump(mode="python")
        | {
            "batch_ref": _scoped(source_account_ref, "batch", manifest.batch_ref),
            "evidence": (scoped_evidence,),
            "candidates": scoped_candidates,
        },
        strict=True,
    )

    source_evidence = source_manifest.parent / evidence.source_file
    if not source_evidence.is_file() or source_evidence.is_symlink():
        raise AnnualManifestScopeError("source evidence is not a regular file")
    output_directory.mkdir(mode=0o700, parents=False)
    try:
        target_evidence = output_directory / evidence.source_file
        shutil.copyfile(source_evidence, target_evidence)
        os.chmod(target_evidence, 0o600)
        target_manifest = output_directory / "source-manifest.json"
        payload = scoped.model_dump_json(indent=2).encode("utf-8") + b"\n"
        with target_manifest.open("xb") as stream:
            stream.write(payload)
        os.chmod(target_manifest, 0o600)
        return target_manifest
    except Exception:
        shutil.rmtree(output_directory, ignore_errors=True)
        raise


def _scoped(account_ref: UUID, kind: str, source_ref: UUID) -> UUID:
    return uuid5(_NAMESPACE, f"alipay-account:{account_ref}:{kind}:{source_ref}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--source-account-ref", type=UUID, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    result = scope_alipay_annual_manifest(
        source_manifest=args.source_manifest.resolve(),
        source_account_ref=args.source_account_ref,
        output_directory=args.output_directory.resolve(),
    )
    print(f"ALIPAY_ANNUAL_SCOPE_OK manifest={result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
