"""Bring a payment platform's rows into the ledger as review candidates.

A bank statement and a payment bill are different kinds of document, and Core
already knows that. A statement is an account's own record, carries a balance,
and enters through the statement cutover. A payment bill records what was paid
to whom; it has no balance and, in WeChat's case, no single account for one to
belong to. It enters as *candidates*: facts that are real but not yet
classified, and that a person still has to decide about.

Two platforms arrive here, WeChat and Alipay, read through different readers
into one shape. The batch manifest names which, because a financial document's
format is not something to sniff: guessing wrong is worse than being told.

That difference is why this path is the lighter one. ``import_prepared_manifest``
needs no environment gate, no encrypted backup and no restore rehearsal,
because it writes nothing a person has confirmed - and it already carries the
receipted, idempotent replay D-028 asks for, keyed on a batch receipt that
compares both manifest digests and both counts. Re-running a batch is a
no-op, not a second import.

Nothing here classifies anything. Every candidate arrives under a catch-all
category at zero confidence, which is the honest statement of what an importer
knows: that the transaction happened, and nothing about what it was for.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid5

from sqlalchemy import Engine, text

from ledgerbridge.controlled_import import (
    ImportBusinessUnit,
    ImportCandidate,
    ImportCategory,
    ImportEntity,
    ImportResult,
    SourceEvidence,
    SourceManifest,
    import_prepared_manifest,
    prepare_source_manifest,
)
from ledgerbridge.local_alipay import ALIPAY_SOURCE_SYSTEM, read_alipay_export
from ledgerbridge.local_payments import DIRECTIONLESS, PaymentExport, PaymentRow
from ledgerbridge.local_wechat import WECHAT_SOURCE_SYSTEM, read_wechat_export

CANDIDATE_BATCH_SCHEMA: Final = "ledgerbridge.local-candidate-batch.v1"

#: Every reference this module mints is derived from the facts, so re-running
#: the builder produces the same manifest and the receipt recognises it.
_NAMESPACE: Final = UUID("6f2d7b64-4b5f-5a3e-9c81-2f0f6d9a1c47")

#: What an importer is entitled to say about a row it has not classified.
UNCLASSIFIED: Final = "UNCLASSIFIED"

#: WeChat's own direction-less rows: top-ups, withdrawals, 零钱通 movements and
#: card repayments. They are movements between the owner's own accounts rather
#: than income or expenditure, and the bill totals them separately and
#: unsigned. Keeping them in their own category means they neither disappear
#: nor get counted as spending before anyone has looked at them.
INTERNAL_TRANSFER: Final = "INTERNAL_TRANSFER"

_CATEGORY_LABELS: Final = {
    UNCLASSIFIED: "Unclassified, pending review",
    INTERNAL_TRANSFER: "Movement between the owner's own accounts, pending review",
}

_MAX_MANIFEST_BYTES: Final = 4 * 1024 * 1024
_MAX_SOURCES: Final = 20
_MAX_LABEL: Final = 100
_MAX_SUMMARY: Final = 500


class LocalCandidateError(RuntimeError):
    """A local candidate batch could not be read or imported."""


@dataclass(frozen=True, slots=True)
class _Platform:
    """What differs between one payment platform's exports and another's."""

    reader: Callable[[bytes], PaymentExport]
    source_system: str
    extension: str
    media_type: str
    #: Prefixed to every derived reference, so two platforms cannot mint the
    #: same one from the same order number. WeChat's is empty and has to stay
    #: empty: 2,212 of its candidates are already in the ledger under
    #: unprefixed references, and changing them would import them a second time
    #: rather than replay them.
    ref_scope: tuple[str, ...]


#: A reference that belongs to the book rather than to one platform's
#: file is minted without a platform prefix.
_BOOK_SCOPE: Final[tuple[str, ...]] = ()

_PLATFORMS: Final[dict[str, _Platform]] = {
    "wechat": _Platform(
        reader=read_wechat_export,
        source_system=WECHAT_SOURCE_SYSTEM,
        extension="xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ref_scope=(),
    ),
    "alipay": _Platform(
        reader=read_alipay_export,
        source_system=ALIPAY_SOURCE_SYSTEM,
        extension="csv",
        media_type="text/csv",
        ref_scope=(ALIPAY_SOURCE_SYSTEM,),
    ),
}


@dataclass(frozen=True, slots=True)
class LocalCandidateBatch:
    """One platform's exports, and the book they belong to."""

    platform: str
    entity_ref: UUID
    business_unit_ref: UUID
    sources: tuple[Path, ...]
    description: str

    @property
    def spec(self) -> _Platform:
        """The reader and naming this batch's platform uses."""

        return _PLATFORMS[self.platform]


def load_candidate_batch(path: Path) -> LocalCandidateBatch:
    """Read one strict batch manifest. Unknown fields are refused."""

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise LocalCandidateError(f"candidate manifest is unreadable: {error}") from None
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise LocalCandidateError("candidate manifest is implausibly large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise LocalCandidateError("candidate manifest is not valid JSON") from None
    if not isinstance(payload, dict) or payload.get("schema_version") != CANDIDATE_BATCH_SCHEMA:
        raise LocalCandidateError(f"candidate manifest must declare {CANDIDATE_BATCH_SCHEMA}")
    expected = {"schema_version", "platform", "book", "sources", "description"}
    if set(payload) != expected:
        raise LocalCandidateError(f"candidate manifest keys must be exactly {sorted(expected)}")

    platform = payload["platform"]
    if platform not in _PLATFORMS:
        raise LocalCandidateError(
            f"candidate manifest platform must be one of {sorted(_PLATFORMS)}"
        )
    book = payload["book"]
    if not isinstance(book, dict) or set(book) != {"entity_ref", "business_unit_ref"}:
        raise LocalCandidateError("candidate manifest book is invalid")
    sources_payload = payload["sources"]
    if not isinstance(sources_payload, list) or not sources_payload:
        raise LocalCandidateError("candidate manifest names no source file")
    if len(sources_payload) > _MAX_SOURCES:
        raise LocalCandidateError(f"a batch reads at most {_MAX_SOURCES} export files")
    sources: list[Path] = []
    for item in sources_payload:
        if not isinstance(item, str) or not item:
            raise LocalCandidateError("candidate manifest source path is invalid")
        source = Path(item)
        if not source.is_absolute():
            raise LocalCandidateError(f"source path must be absolute: {source}")
        sources.append(source)
    if len({source.resolve() for source in sources}) != len(sources):
        raise LocalCandidateError("candidate manifest names one file twice")
    description = payload["description"]
    if not isinstance(description, str) or not 1 <= len(description) <= 500:
        raise LocalCandidateError("candidate manifest description is invalid")
    return LocalCandidateBatch(
        platform=platform,
        entity_ref=_uuid(book["entity_ref"]),
        business_unit_ref=_uuid(book["business_unit_ref"]),
        sources=tuple(sources),
        description=description,
    )


def build_source_manifest(
    engine: Engine,
    batch: LocalCandidateBatch,
) -> tuple[SourceManifest, dict[UUID, Path]]:
    """Read every export and turn its rows into candidates.

    Every file is read before anything is written, which is the bounded preview:
    an export that disagrees with its own preamble stops the run here.

    The book's name and labels are read from the ledger rather than taken from
    the manifest, because the import verifies them against what is already
    stored and a manifest that guessed them would fail deep inside instead of
    here.
    """

    entity_name, unit_ref, unit_label = _book_identity(engine, batch)
    spec = batch.spec

    evidence: list[SourceEvidence] = []
    files: dict[UUID, Path] = {}
    # Serial -> (row, the evidence it came from). Overlapping exports repeat
    # the same transaction; one candidate is made for it. The exports are read
    # oldest-first so that where they differ descriptively, the newer one is
    # the one kept - see `_reconcile`.
    rows: dict[str, tuple[PaymentRow, UUID]] = {}
    exports = _exports_oldest_first(batch)
    identity = _identity_scope(spec, _one_account(exports))
    for source, raw, export in exports:
        path = source.resolve()
        digest = hashlib.sha256(raw).hexdigest()
        evidence_ref = _ref(spec.ref_scope, "evidence", digest)
        if evidence_ref in files:
            raise LocalCandidateError("batch names two exports with the same content")
        files[evidence_ref] = path
        evidence.append(
            SourceEvidence(
                evidence_ref=evidence_ref,
                # The bank's own filename carries the owner's name and is not
                # ASCII; the stored name is the digest, which identifies the
                # bytes without describing whose they are.
                source_file=f"{digest[:16]}.{spec.extension}",
                display_name=f"{batch.platform}-{digest[:16]}.{spec.extension}",
                declared_media_type=spec.media_type,
                plaintext_sha256=digest,
                plaintext_size=len(raw),
            )
        )
        for row in export.rows:
            held = rows.get(row.serial)
            rows[row.serial] = (row if held is None else _reconcile(held[0], row), evidence_ref)

    candidates = tuple(
        _candidate(spec, identity, row, evidence_ref)
        for _, (row, evidence_ref) in sorted(rows.items(), key=lambda item: item[1][0].occurred_at)
    )
    if not candidates:
        raise LocalCandidateError("batch holds no transaction")
    used = {candidate.category_code for candidate in candidates}
    manifest = SourceManifest(
        schema_version="ledgerbridge.controlled-review-source.v1",
        batch_ref=_ref(
            spec.ref_scope, "batch", *sorted(item.plaintext_sha256 for item in evidence)
        ),
        # The moment the newest export was taken, not the moment this ran.
        # The batch receipt compares manifest digests to tell a replay from a
        # second import, so a clock reading here would make every re-run look
        # like a different batch and the idempotence would be a fiction.
        generated_at=max(export.exported_at for _, _, export in exports),
        source_description=batch.description,
        entity=ImportEntity(entity_ref=batch.entity_ref, name=entity_name),
        business_unit=ImportBusinessUnit(
            business_unit_ref=batch.business_unit_ref,
            ref=unit_ref,
            label=unit_label,
        ),
        categories=tuple(
            ImportCategory(
                # Unscoped by platform on purpose: a reporting category
                # belongs to the book, and the database agrees - it is
                # unique on (entity, code). WeChat and Alipay rows land in
                # the same UNCLASSIFIED, which is what a review queue wants.
                category_ref=_ref(_BOOK_SCOPE, "category", str(batch.entity_ref), code),
                code=code,
                label=_CATEGORY_LABELS[code],
            )
            for code in sorted(used)
        ),
        evidence=tuple(evidence),
        candidates=candidates,
    )
    return manifest, files


def import_local_candidates(
    engine: Engine,
    batch: LocalCandidateBatch,
    *,
    key_file: Path,
    artifact_root: Path,
) -> ImportResult:
    """Encrypt the exports, then import their candidates under one transaction.

    The staging directory holds a copy of the exports only while they are being
    encrypted, and is removed afterwards whether or not the import succeeds -
    an unencrypted second copy of a year of payments is not something to leave
    lying about.
    """

    manifest, files = build_source_manifest(engine, batch)
    staging = Path(tempfile.mkdtemp(prefix="ledgerbridge-candidates-"))
    try:
        for descriptor in manifest.evidence:
            shutil.copyfile(files[descriptor.evidence_ref], staging / descriptor.source_file)
        source_path = staging / "source-manifest.json"
        source_path.write_text(
            json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        prepare_source_manifest(
            source_path,
            key_file=key_file,
            artifact_root=artifact_root,
            prepared_manifest_path=staging / "prepared-manifest.json",
        )
        return import_prepared_manifest(engine, staging / "prepared-manifest.json")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _exports_oldest_first(
    batch: LocalCandidateBatch,
) -> list[tuple[Path, bytes, PaymentExport]]:
    """Read every export before anything is written, oldest export first."""

    reader = batch.spec.reader
    read: list[tuple[Path, bytes, PaymentExport]] = []
    for source in batch.sources:
        try:
            raw = source.resolve().read_bytes()
        except OSError as error:
            raise LocalCandidateError(f"export is unreadable: {error}") from None
        read.append((source, raw, reader(raw)))
    return sorted(read, key=lambda item: item[2].exported_at)


def _one_account(exports: list[tuple[Path, bytes, PaymentExport]]) -> str:
    """The account this batch belongs to, refusing files that name two.

    Alipay's export says which login it belongs to. Two logins in one batch
    would merge two accounts' payments into one set of candidates with nothing
    recording which was which. WeChat's export names no account, so there is
    nothing to compare, nothing is claimed, and the empty string comes back.
    """

    named = {export.account_hint for _, _, export in exports if export.account_hint}
    if len(named) > 1:
        raise LocalCandidateError(
            "the exports in this batch name different accounts, so they are not "
            "one account's bill. Import each account as its own batch."
        )
    return named.pop() if named else ""


def _identity_scope(spec: _Platform, account: str) -> tuple[str, ...]:
    """The namespace a transaction's identity lives in.

    A payment is identified by the account it moved through *and* the order
    number, not by the order number alone. One Alipay account paying another
    writes the same order number into both bills - once as money going out and
    once as money coming in - and those are two facts, one per account, not one
    fact seen twice. Keying on the order number alone would collapse them.

    Where the file names no account, nothing is added: WeChat's bill names
    none, and its candidates are already in the ledger under references minted
    without one.

    The account is folded in as a digest rather than in the clear. A login is
    an email address or a phone number, and a derived reference is stored
    forever; it has no business carrying one.
    """

    if not account:
        return spec.ref_scope
    digest = hashlib.sha256(f"{spec.source_system}|{account}".encode()).hexdigest()
    return (*spec.ref_scope, digest[:16])


def _reconcile(held: PaymentRow, found: PaymentRow) -> PaymentRow:
    """Settle two exports' accounts of one transaction.

    What the transaction *is* - when it happened, its direction, its amount,
    its type, what funded it, the merchant's own reference - must agree, and a
    disagreement stops the batch. Those are the facts, and a source that
    contradicts itself about them is not one to import from quietly.

    The rest is description, and description moves: a counterparty's WeChat
    display name is their profile, not a property of a payment they received,
    and a refunded payment is shown with a later status than the export taken
    before the refund. So the newer export's account of those fields wins,
    which is why the exports are read oldest-first.
    """

    for field in ("occurred_at", "direction", "amount_minor", "kind", "funding", "merchant_serial"):
        if getattr(held, field) != getattr(found, field):
            raise LocalCandidateError(
                f"two exports disagree about the {field} of transaction "
                f"{found.serial[:8]}..., which is a fact rather than a description. "
                "One of them is wrong; neither was imported."
            )
    return found


def _candidate(
    spec: _Platform,
    identity: tuple[str, ...],
    row: PaymentRow,
    evidence_ref: UUID,
) -> ImportCandidate:
    """One row, stated as a candidate and classified as nothing.

    The amount keeps the sign the bill states and no more: negative for
    expenditure, positive for income, and the bare magnitude for a row the
    platform itself declines to give a direction. Guessing a sign from the
    transaction type would be inference presented as a fact, and the category
    they land in exists so that a person can do it instead.
    """

    return ImportCandidate(
        candidate_ref=_ref(identity, "candidate", row.serial),
        operation_id=_ref(identity, "operation", row.serial),
        ingest_channel="CONTROLLED_UPLOAD",
        source_system=spec.source_system,
        source_event_ref=_ref(identity, "source-event", spec.source_system, row.serial),
        display_label=_bounded(row.counterparty or row.kind, _MAX_LABEL),
        category_code=INTERNAL_TRANSFER if row.direction == DIRECTIONLESS else UNCLASSIFIED,
        amount_minor=row.signed_amount_minor,
        accounting_month=f"{row.occurred_at:%Y-%m}",
        summary=_bounded(
            " / ".join(
                part
                for part in (row.kind, row.product, row.funding, row.status, row.note)
                if part and part != "/"
            )
            or row.kind,
            _MAX_SUMMARY,
        ),
        # Nothing here looked at what the transaction was for, and saying so is
        # the point: a review queue sorted by confidence should not put an
        # importer's silence above a person's judgement.
        confidence_basis_points=0,
        evidence_refs=(evidence_ref,),
    )


def _book_identity(engine: Engine, batch: LocalCandidateBatch) -> tuple[str, str, str]:
    with engine.connect() as connection:
        found = (
            connection.execute(
                text(
                    "SELECT e.name, b.ref, b.label FROM public.entity e "
                    "JOIN public.business_unit b ON b.entity_id = e.id "
                    "WHERE e.id = :entity AND b.id = :unit"
                ),
                {"entity": batch.entity_ref, "unit": batch.business_unit_ref},
            )
            .mappings()
            .one_or_none()
        )
    if found is None:
        raise LocalCandidateError(
            "the book this batch names does not exist, or its business unit "
            "belongs to another entity. Candidates are imported into a book "
            "that already holds accounts; admit the book first."
        )
    return str(found["name"]), str(found["ref"]), str(found["label"])


def _bounded(value: str, limit: int) -> str:
    text_value = " ".join(value.split())
    if not text_value:
        raise LocalCandidateError("transaction carries no text to describe it")
    return text_value[:limit]


def _ref(scope: tuple[str, ...], kind: str, *parts: str) -> UUID:
    return uuid5(_NAMESPACE, "|".join((*scope, kind, *parts)))


def _uuid(value: Any) -> UUID:
    if not isinstance(value, str):
        raise LocalCandidateError("candidate manifest reference is invalid")
    try:
        return UUID(value)
    except ValueError:
        raise LocalCandidateError(f"candidate manifest reference is invalid: {value}") from None


__all__ = [
    "CANDIDATE_BATCH_SCHEMA",
    "INTERNAL_TRANSFER",
    "UNCLASSIFIED",
    "LocalCandidateBatch",
    "LocalCandidateError",
    "build_source_manifest",
    "import_local_candidates",
    "load_candidate_batch",
]
