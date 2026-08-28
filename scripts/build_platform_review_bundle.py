"""Build a private controlled-review bundle from normalized platform records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ledgerbridge.controlled_import import SOURCE_MANIFEST_SCHEMA, SourceManifest

_NAMESPACE = UUID("bd23ace7-59c3-49ab-8df5-6ed83f0d114e")
_EXCEL_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class PlatformBundleError(RuntimeError):
    """The platform source bundle could not be proven complete."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NormalizedPlatformRecord(_StrictModel):
    recordId: str = Field(pattern=r"^(WX|ZFB)-[0-9a-f]{12}$")
    date: str = Field(pattern=r"^2026-05-[0-3][0-9]$")
    source: Literal["微信", "支付宝"]
    amountMinor: int = Field(ge=-9_007_199_254_740_991, le=9_007_199_254_740_991)
    effect: bool
    direction: str = Field(min_length=1, max_length=50)
    category: str = Field(max_length=200)
    counterparty: str = Field(max_length=200)
    description: str = Field(max_length=500)
    paymentMethod: str = Field(max_length=200)
    status: str = Field(max_length=200)
    evidenceAlias: Literal["wechat", "alipay"]

    @model_validator(mode="after")
    def source_matches_evidence(self) -> NormalizedPlatformRecord:
        expected = "wechat" if self.source == "微信" else "alipay"
        if self.evidenceAlias != expected:
            raise ValueError("platform record evidence alias does not match its source")
        if self.effect and self.amountMinor == 0:
            raise ValueError("effective platform record must have a non-zero amount")
        return self


class NormalizedPlatformEnvelope(_StrictModel):
    schemaVersion: Literal["ledgerbridge.financial-foundation-normalized.v1"]
    period: Literal["2026-05"]
    records: tuple[NormalizedPlatformRecord, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def record_ids_are_unique(self) -> NormalizedPlatformEnvelope:
        ids = [record.recordId for record in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("normalized platform record ids must be unique")
        return self


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--normalized-records", type=Path, required=True)
    parser.add_argument("--wechat-statement", type=Path, required=True)
    parser.add_argument("--alipay-statement", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    manifest_path, manifest = build_platform_bundle(
        normalized_records=args.normalized_records.resolve(),
        wechat_statement=args.wechat_statement.resolve(),
        alipay_statement=args.alipay_statement.resolve(),
        output_directory=args.output_directory.resolve(),
    )
    print(
        "PLATFORM_REVIEW_BUNDLE_OK "
        f"evidence={len(manifest.evidence)} candidates={len(manifest.candidates)} "
        f"manifest_sha256={_digest(manifest_path)}"
    )
    return 0


def build_platform_bundle(
    *,
    normalized_records: Path,
    wechat_statement: Path,
    alipay_statement: Path,
    output_directory: Path,
) -> tuple[Path, SourceManifest]:
    envelope = _load_normalized(normalized_records)
    statements = {
        "wechat": (wechat_statement, "wechat-pay-statement.xlsx", _EXCEL_MEDIA_TYPE),
        "alipay": (alipay_statement, "alipay-statement.csv", "text/csv"),
    }
    for source, _, _ in statements.values():
        _require_regular_file(source)
    if wechat_statement.suffix.lower() != ".xlsx":
        raise PlatformBundleError("WeChat statement must be an xlsx file")
    if alipay_statement.suffix.lower() != ".csv":
        raise PlatformBundleError("Alipay statement must be a csv file")
    if output_directory.exists():
        raise PlatformBundleError("output directory already exists")
    output_directory.mkdir(mode=0o700, parents=False)

    evidence: list[dict[str, object]] = []
    evidence_refs: dict[str, UUID] = {}
    source_digests: list[str] = []
    for alias, (source, safe_name, media_type) in statements.items():
        digest = _digest(source)
        source_digests.append(digest)
        ref = uuid5(_NAMESPACE, f"evidence:{alias}:{digest}")
        evidence_refs[alias] = ref
        shutil.copy2(source, output_directory / safe_name)
        evidence.append(
            {
                "evidence_ref": ref,
                "source_file": safe_name,
                "display_name": safe_name,
                "declared_media_type": media_type,
                "plaintext_sha256": digest,
                "plaintext_size": source.stat().st_size,
            }
        )

    effective = tuple(record for record in envelope.records if record.effect)
    if not effective:
        raise PlatformBundleError("normalized platform bundle has no effective records")
    categories = (
        {
            "category_ref": uuid5(_NAMESPACE, "category:wechat-transaction-review"),
            "code": "WECHAT_TRANSACTION_REVIEW",
            "label": "微信交易复核",
        },
        {
            "category_ref": uuid5(_NAMESPACE, "category:alipay-transaction-review"),
            "code": "ALIPAY_TRANSACTION_REVIEW",
            "label": "支付宝交易复核",
        },
    )
    candidates: list[dict[str, object]] = []
    for record in effective:
        source_system = "wechat_pay_export" if record.source == "微信" else "alipay_export"
        category_code = (
            "WECHAT_TRANSACTION_REVIEW" if record.source == "微信" else "ALIPAY_TRANSACTION_REVIEW"
        )
        stable = f"{source_system}:2026-05:{record.recordId}"
        summary_parts = (
            record.source,
            record.date,
            record.direction,
            record.category,
            record.counterparty,
            record.paymentMethod,
            record.status,
        )
        summary = " | ".join(part for part in summary_parts if part)[:500]
        candidates.append(
            {
                "candidate_ref": uuid5(_NAMESPACE, f"candidate:{stable}"),
                "operation_id": uuid5(_NAMESPACE, f"operation:{stable}"),
                "ingest_channel": "CONTROLLED_UPLOAD",
                "source_system": source_system,
                "source_event_ref": uuid5(_NAMESPACE, f"source-event:{stable}"),
                "display_label": f"{record.source} {record.date} {record.category}"[:100],
                "category_code": category_code,
                "amount_minor": record.amountMinor,
                "accounting_month": "2026-05",
                "summary": summary,
                "confidence_basis_points": 9900,
                "evidence_refs": (evidence_refs[record.evidenceAlias],),
            }
        )

    fingerprint = ":".join((*sorted(source_digests), *(record.recordId for record in effective)))
    payload = {
        "schema_version": SOURCE_MANIFEST_SCHEMA,
        "batch_ref": uuid5(_NAMESPACE, f"batch:{fingerprint}"),
        "generated_at": datetime.fromtimestamp(
            max(wechat_statement.stat().st_mtime, alipay_statement.stat().st_mtime),
            tz=UTC,
        ),
        "source_description": (
            "Native WeChat Pay and Alipay May 2026 exports admitted as pending review "
            "candidates; no automatic confirmation or posting."
        ),
        "entity": {
            "entity_ref": uuid5(
                UUID("b2f82a31-26cf-4b43-a6a7-8e90339ab468"),
                "entity:controlled-reconciliation",
            ),
            "name": "LedgerBridge controlled reconciliation",
        },
        "business_unit": {
            "business_unit_ref": uuid5(
                UUID("b2f82a31-26cf-4b43-a6a7-8e90339ab468"),
                "business-unit:2026-05-controlled-review",
            ),
            "ref": "review-2026-05",
            "label": "2026年5月对账复核",
        },
        "categories": categories,
        "evidence": tuple(evidence),
        "candidates": tuple(candidates),
    }
    manifest = SourceManifest.model_validate(payload, strict=True)
    manifest_path = output_directory / "source-manifest.json"
    _write_private_json(manifest_path, manifest.model_dump(mode="json"))
    return manifest_path, manifest


def _load_normalized(path: Path) -> NormalizedPlatformEnvelope:
    _require_regular_file(path)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise PlatformBundleError("normalized record file exceeds the size limit")
    try:
        return NormalizedPlatformEnvelope.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise PlatformBundleError("normalized platform record file is invalid") from exc


def _require_regular_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PlatformBundleError("source file is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PlatformBundleError("source file must be a regular file")
    if metadata.st_size <= 0 or metadata.st_size > 134_217_728:
        raise PlatformBundleError("source file size is invalid")


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        written = 0
        while written < len(encoded):
            count = os.write(descriptor, encoded[written:])
            if count <= 0:
                raise PlatformBundleError("manifest write made no progress")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
