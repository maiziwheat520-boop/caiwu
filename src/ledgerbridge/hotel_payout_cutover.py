"""Audited cutover from weekly hotel summaries to OCR payout candidates.

The cutover is deliberately separate from the original controlled batch.  It
registers prepared encrypted evidence when needed, ignores only weekly candidates
replaced by more precise OCR rows, and records one-to-one bank-credit evidence
links.  It never confirms a candidate or posts a ledger entry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ledgerbridge.controlled_import import (
    ControlledImportError,
    ImportCandidate,
    PreparedManifest,
    SourceManifest,
    _insert_candidate,
    _insert_dimensions,
    _insert_evidence,
    load_prepared_manifest,
    load_source_manifest,
)

HOTEL_PAYOUT_CUTOVER_SCHEMA = "ledgerbridge.hotel-payout-cutover.v1"
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_RISK_CODE = "HOTEL_PAYOUT_STATEMENT_REQUIRED"
_RELATION = "SAME_ECONOMIC_TRANSACTION"


class HotelPayoutCutoverError(ControlledImportError):
    """The hotel payout cutover could not prove an atomic result."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HotelReplacement(_FrozenModel):
    legacy_candidate_ref: UUID
    ocr_candidate_ref: UUID
    amount_minor: Annotated[int, Field(strict=True, gt=0)]

    @model_validator(mode="after")
    def distinct_candidates(self) -> HotelReplacement:
        if self.legacy_candidate_ref == self.ocr_candidate_ref:
            raise ValueError("replacement candidate identities must differ")
        return self


class HotelMatchBasis(_FrozenModel):
    method: Literal["EXACT_AMOUNT_DATE_PLATFORM_ONE_TO_ONE"]
    platform: Literal["CTRIP_EBOOKING", "MEITUAN_MOBILE"]
    subject_period_start: date
    subject_period_end: date
    evidence_date: date
    evidence_transaction_ref: str = Field(pattern=r"^TX-[0-9]{4,}$", max_length=40)

    @model_validator(mode="after")
    def settlement_date_is_bounded(self) -> HotelMatchBasis:
        if self.subject_period_start > self.subject_period_end:
            raise ValueError("hotel payout period is inverted")
        if not (
            self.subject_period_end
            <= self.evidence_date
            <= self.subject_period_end + timedelta(days=7)
        ):
            raise ValueError("bank credit must be within seven days after the payout period")
        return self


class CandidateEvidenceLink(_FrozenModel):
    link_ref: UUID
    subject_candidate_ref: UUID
    evidence_candidate_ref: UUID
    risk_code: Literal["HOTEL_PAYOUT_STATEMENT_REQUIRED"]
    relation: Literal["SAME_ECONOMIC_TRANSACTION"]
    amount_minor: Annotated[int, Field(strict=True, gt=0)]
    currency: Literal["CNY"]
    match_basis: HotelMatchBasis

    @model_validator(mode="after")
    def candidates_are_distinct(self) -> CandidateEvidenceLink:
        if self.subject_candidate_ref == self.evidence_candidate_ref:
            raise ValueError("subject and evidence candidates must differ")
        return self


class HotelPayoutCutoverManifest(_FrozenModel):
    schema_version: Literal["ledgerbridge.hotel-payout-cutover.v1"]
    cutover_ref: UUID
    generated_at: datetime
    source_manifest_sha256: str = Field(pattern=_HEX_64.pattern)
    entity_ref: UUID
    business_unit_ref: UUID
    ocr_candidate_refs: tuple[UUID, ...] = Field(min_length=1)
    replacements: tuple[HotelReplacement, ...] = Field(min_length=1)
    evidence_links: tuple[CandidateEvidenceLink, ...]

    @model_validator(mode="after")
    def references_are_closed_and_unique(self) -> HotelPayoutCutoverManifest:
        if self.generated_at.tzinfo is None:
            raise ValueError("cutover generation time must be timezone-aware")
        ocr_refs = set(self.ocr_candidate_refs)
        if len(ocr_refs) != len(self.ocr_candidate_refs):
            raise ValueError("OCR candidate references must be unique")
        legacy_refs = [item.legacy_candidate_ref for item in self.replacements]
        replacement_refs = [item.ocr_candidate_ref for item in self.replacements]
        subject_refs = [item.subject_candidate_ref for item in self.evidence_links]
        evidence_refs = [item.evidence_candidate_ref for item in self.evidence_links]
        if len(set(legacy_refs)) != len(legacy_refs):
            raise ValueError("legacy replacement references must be unique")
        if len(set(replacement_refs)) != len(replacement_refs):
            raise ValueError("replacement OCR references must be unique")
        if len(set(subject_refs)) != len(subject_refs):
            raise ValueError("evidence-link subjects must be unique")
        if len(set(evidence_refs)) != len(evidence_refs):
            raise ValueError("bank evidence candidates cannot be reused")
        if not set(replacement_refs) <= ocr_refs or not set(subject_refs) <= ocr_refs:
            raise ValueError("cutover references an undeclared OCR candidate")
        return self


class HotelPayoutCutoverResult(_FrozenModel):
    cutover_ref: UUID
    replayed: bool
    ignored_candidate_count: int = Field(ge=0)
    imported_candidate_count: int = Field(gt=0)
    link_count: int = Field(ge=0)
    audit_horizon_sequence: int = Field(gt=0)
    audit_horizon_hash: str = Field(pattern=_HEX_64.pattern)


def load_hotel_payout_cutover_manifest(
    path: Path,
) -> tuple[HotelPayoutCutoverManifest, bytes]:
    raw = _read_bounded_regular_file(path, max_bytes=4 * 1024 * 1024)
    try:
        manifest = HotelPayoutCutoverManifest.model_validate_json(raw, strict=True)
    except ValueError as exc:
        raise HotelPayoutCutoverError("hotel payout cutover manifest is invalid") from exc
    return manifest, raw


def import_hotel_payout_cutover(
    engine: Engine,
    *,
    source_manifest_path: Path,
    prepared_manifest_path: Path,
    cutover_manifest_path: Path,
) -> HotelPayoutCutoverResult:
    source, source_raw = load_source_manifest(source_manifest_path)
    prepared, prepared_raw = load_prepared_manifest(prepared_manifest_path)
    cutover, cutover_raw = load_hotel_payout_cutover_manifest(cutover_manifest_path)
    source_digest = hashlib.sha256(source_raw).hexdigest()
    prepared_digest = hashlib.sha256(prepared_raw).digest()
    if source_digest != cutover.source_manifest_sha256:
        raise HotelPayoutCutoverError("cutover source manifest digest does not match")
    _validate_prepared_source(source, prepared, source_digest)
    if (
        source.entity.entity_ref != cutover.entity_ref
        or source.business_unit.business_unit_ref != cutover.business_unit_ref
    ):
        raise HotelPayoutCutoverError("cutover scope does not match the source manifest")
    source_candidates = {item.candidate_ref: item for item in source.candidates}
    ocr_candidates = _resolve_ocr_candidates(source_candidates, cutover)
    _validate_manifest_relations(source_candidates, ocr_candidates, cutover)

    manifest_sha = hashlib.sha256(cutover_raw).digest()
    source_sha = bytes.fromhex(source_digest)
    with engine.begin() as connection:
        receipt = connection.execute(
            text(
                "SELECT manifest_sha256, source_manifest_sha256, prepared_manifest_sha256, "
                "ignored_candidate_count, "
                "imported_candidate_count, link_count, audit_horizon_sequence, "
                "audit_horizon_hash FROM internal_import.hotel_payout_cutover_receipt "
                "WHERE cutover_ref = :cutover"
            ),
            {"cutover": cutover.cutover_ref},
        ).mappings().first()
        if receipt is not None:
            expected = (
                bytes(receipt["manifest_sha256"]) == manifest_sha
                and bytes(receipt["source_manifest_sha256"]) == source_sha
                and bytes(receipt["prepared_manifest_sha256"]) == prepared_digest
                and receipt["ignored_candidate_count"] == len(cutover.replacements)
                and receipt["imported_candidate_count"] == len(ocr_candidates)
                and receipt["link_count"] == len(cutover.evidence_links)
            )
            if not expected:
                raise HotelPayoutCutoverError("hotel payout cutover receipt conflicts")
            return _result_from_receipt(cutover.cutover_ref, receipt, replayed=True)

        missing_evidence = _preflight_cutover(
            connection, source, prepared, ocr_candidates, cutover
        )
        _insert_dimensions(connection, prepared)
        prepared_evidence = {item.evidence_ref: item for item in prepared.evidence}
        for evidence_ref in sorted(missing_evidence, key=str):
            _insert_evidence(connection, prepared, prepared_evidence[evidence_ref])
        categories = {item.code: item for item in source.categories}
        for candidate in ocr_candidates:
            _insert_candidate(
                connection,
                source,
                categories[candidate.category_code],
                candidate,
            )
        for replacement in cutover.replacements:
            _ignore_replaced_candidate(connection, replacement)
        for link in cutover.evidence_links:
            _insert_evidence_link(connection, cutover, link)

        horizon = connection.execute(
            text("SELECT sequence, hash FROM public.audit_event ORDER BY sequence DESC LIMIT 1")
        ).mappings().one()
        connection.execute(
            text(
                "INSERT INTO internal_import.hotel_payout_cutover_receipt "
                "(cutover_ref, manifest_sha256, source_manifest_sha256, "
                "prepared_manifest_sha256, "
                "ignored_candidate_count, imported_candidate_count, link_count, "
                "audit_horizon_sequence, audit_horizon_hash) VALUES "
                "(:cutover, :manifest_sha, :source_sha, :prepared_sha, "
                ":ignored, :imported, :links, "
                ":sequence, :hash)"
            ),
            {
                "cutover": cutover.cutover_ref,
                "manifest_sha": manifest_sha,
                "source_sha": source_sha,
                "prepared_sha": prepared_digest,
                "ignored": len(cutover.replacements),
                "imported": len(ocr_candidates),
                "links": len(cutover.evidence_links),
                "sequence": horizon["sequence"],
                "hash": horizon["hash"],
            },
        )
        return HotelPayoutCutoverResult(
            cutover_ref=cutover.cutover_ref,
            replayed=False,
            ignored_candidate_count=len(cutover.replacements),
            imported_candidate_count=len(ocr_candidates),
            link_count=len(cutover.evidence_links),
            audit_horizon_sequence=horizon["sequence"],
            audit_horizon_hash=bytes(horizon["hash"]).hex(),
        )


def _resolve_ocr_candidates(
    source_candidates: dict[UUID, ImportCandidate],
    cutover: HotelPayoutCutoverManifest,
) -> tuple[ImportCandidate, ...]:
    try:
        values = tuple(source_candidates[item] for item in cutover.ocr_candidate_refs)
    except KeyError as exc:
        raise HotelPayoutCutoverError("cutover OCR candidate is absent from source") from exc
    if any(item.source_system != "hotel_bill_ocr" for item in values):
        raise HotelPayoutCutoverError("cutover OCR candidate source is invalid")
    return values


def _validate_prepared_source(
    source: SourceManifest,
    prepared: PreparedManifest,
    source_digest: str,
) -> None:
    if prepared.source_manifest_sha256 != source_digest:
        raise HotelPayoutCutoverError("prepared manifest belongs to another source")
    if (
        prepared.batch_ref != source.batch_ref
        or prepared.entity != source.entity
        or prepared.business_unit != source.business_unit
        or prepared.categories != source.categories
        or prepared.candidates != source.candidates
    ):
        raise HotelPayoutCutoverError("prepared manifest content conflicts with source")
    source_evidence = {item.evidence_ref: item for item in source.evidence}
    if set(source_evidence) != {item.evidence_ref for item in prepared.evidence}:
        raise HotelPayoutCutoverError("prepared evidence set conflicts with source")
    for item in prepared.evidence:
        expected = source_evidence[item.evidence_ref]
        if (
            item.display_name != expected.display_name
            or item.declared_media_type != expected.declared_media_type
            or item.plaintext_sha256 != expected.plaintext_sha256
            or item.plaintext_size != expected.plaintext_size
        ):
            raise HotelPayoutCutoverError("prepared evidence identity conflicts with source")


def _validate_manifest_relations(
    source_candidates: dict[UUID, ImportCandidate],
    ocr_candidates: tuple[ImportCandidate, ...],
    cutover: HotelPayoutCutoverManifest,
) -> None:
    ocr_by_ref = {item.candidate_ref: item for item in ocr_candidates}
    for replacement in cutover.replacements:
        if ocr_by_ref[replacement.ocr_candidate_ref].amount_minor != replacement.amount_minor:
            raise HotelPayoutCutoverError("replacement amount does not match OCR candidate")
    for link in cutover.evidence_links:
        subject = ocr_by_ref[link.subject_candidate_ref]
        evidence = source_candidates.get(link.evidence_candidate_ref)
        if evidence is None or evidence.source_system != "boc_mail_derived_review":
            raise HotelPayoutCutoverError("link bank evidence candidate is invalid")
        if subject.amount_minor != link.amount_minor or evidence.amount_minor != link.amount_minor:
            raise HotelPayoutCutoverError("link amount does not match both candidates")


def _preflight_cutover(
    connection: Connection,
    source: SourceManifest,
    prepared: PreparedManifest,
    ocr_candidates: tuple[ImportCandidate, ...],
    cutover: HotelPayoutCutoverManifest,
) -> set[UUID]:
    existing_new = connection.execute(
        text("SELECT count(*) FROM public.candidate WHERE id = ANY(:ids)"),
        {"ids": [item.candidate_ref for item in ocr_candidates]},
    ).scalar_one()
    if existing_new:
        raise HotelPayoutCutoverError("unreceipted OCR candidates already exist")

    evidence_refs = {ref for item in ocr_candidates for ref in item.evidence_refs}
    evidence_rows = connection.execute(
        text(
            "SELECT evidence_ref, entity_id, business_unit_id, plaintext_sha256, "
            "plaintext_size FROM public.evidence_object WHERE evidence_ref = ANY(:ids)"
        ),
        {"ids": list(evidence_refs)},
    ).mappings()
    evidence_by_ref = {row["evidence_ref"]: row for row in evidence_rows}
    source_evidence = {item.evidence_ref: item for item in source.evidence}
    prepared_evidence = {item.evidence_ref: item for item in prepared.evidence}
    if not evidence_refs <= set(prepared_evidence):
        raise HotelPayoutCutoverError("OCR source evidence is absent from prepared import")
    for evidence_ref, row in evidence_by_ref.items():
        expected = source_evidence[evidence_ref]
        if (
            row["entity_id"] != cutover.entity_ref
            or row["business_unit_id"] != cutover.business_unit_ref
            or bytes(row["plaintext_sha256"]).hex() != expected.plaintext_sha256
            or row["plaintext_size"] != expected.plaintext_size
        ):
            raise HotelPayoutCutoverError("existing OCR evidence identity conflicts")

    for replacement in cutover.replacements:
        row = _current_candidate_row(connection, replacement.legacy_candidate_ref, lock=True)
        if (
            row["source_system_id"] != "hotel_photo_reconciliation"
            or row["status"] != "PENDING"
            or row["entity_id"] != cutover.entity_ref
            or row["business_unit_id"] != cutover.business_unit_ref
            or row["amount_minor"] != replacement.amount_minor
        ):
            raise HotelPayoutCutoverError("legacy hotel candidate is not replaceable")
    for link in cutover.evidence_links:
        row = _current_candidate_row(connection, link.evidence_candidate_ref, lock=False)
        if (
            row["source_system_id"] != "boc_mail_derived_review"
            or row["status"] != "PENDING"
            or row["entity_id"] != cutover.entity_ref
            or row["business_unit_id"] != cutover.business_unit_ref
            or row["amount_minor"] != link.amount_minor
        ):
            raise HotelPayoutCutoverError("bank evidence candidate does not match cutover")
    return evidence_refs - set(evidence_by_ref)


def _current_candidate_row(
    connection: Connection, candidate_ref: UUID, *, lock: bool
) -> dict[str, object]:
    suffix = " FOR UPDATE OF c" if lock else ""
    row = connection.execute(
        text(
            "SELECT c.id, c.entity_id, cs.source_system_id, r.revision, r.status, "
            "r.business_unit_id, r.business_unit_ref_snapshot, "
            "r.business_unit_label_snapshot, r.category_id, r.category_code_snapshot, "
            "r.category_label_snapshot, r.amount_minor, r.accounting_month "
            "FROM public.candidate c JOIN public.candidate_source cs ON cs.candidate_id = c.id "
            "JOIN LATERAL (SELECT * FROM public.candidate_revision cr "
            "WHERE cr.candidate_id = c.id ORDER BY cr.revision DESC LIMIT 1) r ON true "
            "WHERE c.id = :candidate" + suffix
        ),
        {"candidate": candidate_ref},
    ).mappings().one_or_none()
    if row is None:
        raise HotelPayoutCutoverError("referenced candidate does not exist")
    return dict(row)


def _ignore_replaced_candidate(
    connection: Connection, replacement: HotelReplacement
) -> None:
    row = _current_candidate_row(connection, replacement.legacy_candidate_ref, lock=True)
    reason = "weekly photo summary replaced by a field-complete OCR payout candidate"
    connection.execute(
        text(
            "SELECT internal_command.append_candidate_transition("
            ":candidate, :revision, 'IGNORE', :actor, :reason, CURRENT_TIMESTAMP, "
            ":unit, :unit_ref, :unit_label, :category, :category_code, :category_label, "
            ":amount, :month, NULL)"
        ),
        {
            "candidate": replacement.legacy_candidate_ref,
            "revision": row["revision"],
            "actor": "system:hotel-payout-cutover",
            "reason": reason,
            "unit": row["business_unit_id"],
            "unit_ref": row["business_unit_ref_snapshot"],
            "unit_label": row["business_unit_label_snapshot"],
            "category": row["category_id"],
            "category_code": row["category_code_snapshot"],
            "category_label": row["category_label_snapshot"],
            "amount": row["amount_minor"],
            "month": row["accounting_month"],
        },
    ).scalar_one()


def _insert_evidence_link(
    connection: Connection,
    cutover: HotelPayoutCutoverManifest,
    link: CandidateEvidenceLink,
) -> None:
    payload: dict[str, object] = {
        "link_ref": str(link.link_ref),
        "subject_candidate_id": str(link.subject_candidate_ref),
        "evidence_candidate_id": str(link.evidence_candidate_ref),
        "risk_code": link.risk_code,
        "amount_minor": link.amount_minor,
        "match_basis": link.match_basis.model_dump(mode="json"),
    }
    audit_id = connection.execute(
        text(
            "SELECT public.append_audit_event(:actor, :action, :reason, :rule_version, "
            "CAST(:payload AS jsonb))"
        ),
        {
            "actor": "system:hotel-payout-cutover",
            "action": "candidate.evidence.match",
            "reason": "one-to-one hotel payout matched to receiving bank credit",
            "rule_version": "ledgerbridge.candidate-evidence-link.v1",
            "payload": json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
        },
    ).scalar_one()
    if not isinstance(audit_id, UUID):
        raise HotelPayoutCutoverError("candidate evidence audit returned an invalid identity")
    connection.execute(
        text(
            "INSERT INTO public.candidate_evidence_link "
            "(link_ref, subject_candidate_id, evidence_candidate_id, entity_id, "
            "business_unit_id, risk_code, relation, amount_minor, currency, "
            "match_basis, audit_event_id) VALUES "
            "(:link, :subject, :evidence, :entity, :unit, :risk, :relation, "
            ":amount, 'CNY', CAST(:basis AS jsonb), :audit)"
        ),
        {
            "link": link.link_ref,
            "subject": link.subject_candidate_ref,
            "evidence": link.evidence_candidate_ref,
            "entity": cutover.entity_ref,
            "unit": cutover.business_unit_ref,
            "risk": _RISK_CODE,
            "relation": _RELATION,
            "amount": link.amount_minor,
            "basis": json.dumps(
                link.match_basis.model_dump(mode="json"),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
            "audit": audit_id,
        },
    )


def _result_from_receipt(
    cutover_ref: UUID, receipt: object, *, replayed: bool
) -> HotelPayoutCutoverResult:
    row = receipt
    return HotelPayoutCutoverResult(
        cutover_ref=cutover_ref,
        replayed=replayed,
        ignored_candidate_count=row["ignored_candidate_count"],  # type: ignore[index]
        imported_candidate_count=row["imported_candidate_count"],  # type: ignore[index]
        link_count=row["link_count"],  # type: ignore[index]
        audit_horizon_sequence=row["audit_horizon_sequence"],  # type: ignore[index]
        audit_horizon_hash=bytes(row["audit_horizon_hash"]).hex(),  # type: ignore[index]
    )


def _read_bounded_regular_file(path: Path, *, max_bytes: int) -> bytes:
    if not path.is_absolute():
        raise HotelPayoutCutoverError("cutover paths must be absolute")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HotelPayoutCutoverError("cutover file is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise HotelPayoutCutoverError("cutover file must be regular")
    if metadata.st_size <= 0 or metadata.st_size > max_bytes:
        raise HotelPayoutCutoverError("cutover file size is invalid")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise HotelPayoutCutoverError("cutover file cannot be read") from exc


def write_private_cutover_manifest(
    path: Path, manifest: HotelPayoutCutoverManifest
) -> None:
    encoded = json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        written = 0
        while written < len(encoded):
            count = os.write(descriptor, encoded[written:])
            if count <= 0:
                raise HotelPayoutCutoverError("cutover manifest write made no progress")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
