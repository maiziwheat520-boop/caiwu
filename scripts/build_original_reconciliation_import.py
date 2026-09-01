"""Preview or build private controlled-import bundles from the original workbook."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from ledgerbridge.original_reconciliation_import import (
    OriginalReconciliationImportPlan,
    build_original_reconciliation_manifests,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--write-bundles", action="store_true")
    args = parser.parse_args()

    workbook_path = args.workbook.resolve()
    plan_path = args.plan.resolve()
    plan = OriginalReconciliationImportPlan.model_validate_json(
        plan_path.read_text(encoding="utf-8"),
        strict=True,
    )
    manifests = build_original_reconciliation_manifests(workbook_path, plan)
    summary = {
        "schema_version": "ledgerbridge.original-reconciliation-import-preview.v1",
        "mapping_version": plan.mapping_version,
        "scope_count": len(manifests),
        "candidate_count": sum(len(item.candidates) for item in manifests),
        "months": sorted(
            {candidate.accounting_month for item in manifests for candidate in item.candidates}
        ),
        "workbook_sha256": manifests[0].evidence[0].plaintext_sha256,
    }
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True))

    if not args.write_bundles:
        return 0
    if args.output_dir is None:
        parser.error("--output-dir is required with --write-bundles")
    output_dir = args.output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        parser.error("--output-dir must be absent or empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    for manifest in manifests:
        scope_dir = output_dir / manifest.business_unit.ref
        scope_dir.mkdir()
        evidence_path = scope_dir / manifest.evidence[0].source_file
        shutil.copyfile(workbook_path, evidence_path)
        (scope_dir / "source-manifest.json").write_text(
            manifest.model_dump_json(indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
