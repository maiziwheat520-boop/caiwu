"""Fail-closed preparation and owner-only import of controlled review batches."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Connection, text
from sqlalchemy.engine import Engine

from ledgerbridge.artifacts import ArtifactStore
from ledgerbridge.crypto import (
    ENVELOPE_ALGORITHM,
    ENVELOPE_SCHEMA,
    SecretStreamCipher,
)
from ledgerbridge.encrypted_artifacts import EncryptedArtifactStore
from ledgerbridge.file_key_provider import FileKeyProvider

SOURCE_MANIFEST_SCHEMA = "ledgerbridge.controlled-review-source.v1"
PREPARED_MANIFEST_SCHEMA = "ledgerbridge.controlled-review-prepared.v1"
ARTIFACT_PURPOSE = "ledgerbridge-artifact-v2"

_DATABASE_INGEST_CHANNEL = {
    "CONTROLLED_UPLOAD": "controlled_upload",
    "OUTLOOK": "outlook",
}

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_SAFE_REF = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_MONTH = re.compile(r"^20[0-9]{2}-(0[1-9]|1[0-2])$")
_STORAGE_KEY = re.compile(r"^sha256/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}$")
_GENERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

MoneyMinor = Annotated[int, Field(strict=True, ge=-9_007_199_254_740_991, le=9_007_199_254_740_991)]


class ControlledImportError(RuntimeError):
    """A controlled import could not prove a complete, idempotent result."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ImportEntity(_FrozenModel):
    entity_ref: UUID
    name: str = Field(min_length=1, max_length=200)


class ImportBusinessUnit(_FrozenModel):
    business_unit_ref: UUID
    ref: str = Field(pattern=_SAFE_REF.pattern, min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)


class ImportCategory(_FrozenModel):
    category_ref: UUID
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,99}$", min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)


class SourceEvidence(_FrozenModel):
    evidence_ref: UUID
    source_file: str = Field(pattern=_SAFE_FILE.pattern, min_length=1, max_length=200)
    display_name: str = Field(pattern=_SAFE_FILE.pattern, min_length=1, max_length=200)
    declared_media_type: str = Field(min_length=1, max_length=200)
    plaintext_sha256: str = Field(pattern=_HEX_64.pattern)
    plaintext_size: int = Field(strict=True, ge=0, le=134_217_728)


class ImportCandidate(_FrozenModel):
    candidate_ref: UUID
    operation_id: UUID
    ingest_channel: Literal["CONTROLLED_UPLOAD", "OUTLOOK"]
    source_system: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    source_event_ref: UUID
    display_label: str = Field(min_length=1, max_length=100)
    category_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")
    amount_minor: MoneyMinor
    accounting_month: str = Field(pattern=_MONTH.pattern)
    summary: str = Field(min_length=1, max_length=500)
    confidence_basis_points: int = Field(strict=True, ge=0, le=10_000)
    evidence_refs: tuple[UUID, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def evidence_is_unique(self) -> ImportCandidate:
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("candidate evidence references must be unique")
        return self


class SourceManifest(_FrozenModel):
    schema_version: Literal["ledgerbridge.controlled-review-source.v1"]
    batch_ref: UUID
    generated_at: datetime
    source_description: str = Field(min_length=1, max_length=500)
    entity: ImportEntity
    business_unit: ImportBusinessUnit
    categories: tuple[ImportCategory, ...] = Field(min_length=1)
    evidence: tuple[SourceEvidence, ...] = Field(min_length=1)
    candidates: tuple[ImportCandidate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def batch_is_closed(self) -> SourceManifest:
        if self.generated_at.tzinfo is None:
            raise ValueError("manifest generation time must be timezone-aware")
        evidence_refs = [item.evidence_ref for item in self.evidence]
        candidate_refs = [item.candidate_ref for item in self.candidates]
        operation_ids = [item.operation_id for item in self.candidates]
        source_events = [
            (item.source_system, item.source_event_ref) for item in self.candidates
        ]
        category_codes = [item.code for item in self.categories]
        if len(set(evidence_refs)) != len(evidence_refs):
            raise ValueError("manifest evidence references must be unique")
        if len(set(candidate_refs)) != len(candidate_refs):
            raise ValueError("manifest candidate references must be unique")
        if len(set(operation_ids)) != len(operation_ids):
            raise ValueError("manifest operation IDs must be unique")
        if len(set(source_events)) != len(source_events):
            raise ValueError("manifest source events must be unique")
        if len(set(category_codes)) != len(category_codes):
            raise ValueError("manifest category codes must be unique")
        known_evidence = set(evidence_refs)
        known_categories = set(category_codes)
        for candidate in self.candidates:
            if not set(candidate.evidence_refs) <= known_evidence:
                raise ValueError("candidate references unknown evidence")
            if candidate.category_code not in known_categories:
                raise ValueError("candidate references an unknown category")
        return self


class PreparedEvidence(_FrozenModel):
    evidence_ref: UUID
    display_name: str = Field(pattern=_SAFE_FILE.pattern, min_length=1, max_length=200)
    declared_media_type: str = Field(min_length=1, max_length=200)
    plaintext_sha256: str = Field(pattern=_HEX_64.pattern)
    plaintext_size: int = Field(strict=True, ge=0, le=134_217_728)
    object_ref: str = Field(pattern=_HEX_64.pattern)
    ciphertext_sha256: str = Field(pattern=_HEX_64.pattern)
    ciphertext_size: int = Field(strict=True, ge=1, le=268_435_456)
    storage_key: str = Field(pattern=_STORAGE_KEY.pattern)
    envelope_schema: Literal["ledgerbridge.secretstream.v1"]
    algorithm: Literal["xchacha20poly1305-secretstream"]
    chunk_size: int = Field(strict=True, ge=1, le=1_048_576)
    stream_header: str = Field(pattern=r"^[0-9a-f]{48}$")
    wrapped_key_generation: str = Field(pattern=_GENERATION.pattern)
    wrapped_key_nonce: str = Field(pattern=r"^[0-9a-f]{48}$")
    wrapped_key_ciphertext: str = Field(pattern=r"^[0-9a-f]{96}$")
    purpose: Literal["ledgerbridge-artifact-v2"]


class PreparedManifest(_FrozenModel):
    schema_version: Literal["ledgerbridge.controlled-review-prepared.v1"]
    source_manifest_sha256: str = Field(pattern=_HEX_64.pattern)
    batch_ref: UUID
    generated_at: datetime
    source_description: str = Field(min_length=1, max_length=500)
    entity: ImportEntity
    business_unit: ImportBusinessUnit
    categories: tuple[ImportCategory, ...] = Field(min_length=1)
    evidence: tuple[PreparedEvidence, ...] = Field(min_length=1)
    candidates: tuple[ImportCandidate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def prepared_batch_is_closed(self) -> PreparedManifest:
        SourceManifest.model_validate(
            {
                "schema_version": SOURCE_MANIFEST_SCHEMA,
                "batch_ref": self.batch_ref,
                "generated_at": self.generated_at,
                "source_description": self.source_description,
                "entity": self.entity,
                "business_unit": self.business_unit,
                "categories": self.categories,
                "evidence": tuple(
                    {
                        "evidence_ref": item.evidence_ref,
                        "source_file": item.display_name,
                        "display_name": item.display_name,
                        "declared_media_type": item.declared_media_type,
                        "plaintext_sha256": item.plaintext_sha256,
                        "plaintext_size": item.plaintext_size,
                    }
                    for item in self.evidence
                ),
                "candidates": self.candidates,
            },
            strict=True,
        )
        return self


class ImportResult(_FrozenModel):
    batch_ref: UUID
    replayed: bool
    evidence_count: int
    candidate_count: int
    audit_horizon_sequence: int
    audit_horizon_hash: str = Field(pattern=_HEX_64.pattern)


def load_source_manifest(path: Path) -> tuple[SourceManifest, bytes]:
    raw = _read_bounded_regular_file(path, max_bytes=16 * 1024 * 1024)
    try:
        manifest = SourceManifest.model_validate_json(raw, strict=True)
    except ValueError as exc:
        raise ControlledImportError("source manifest is invalid") from exc
    return manifest, raw


def load_prepared_manifest(path: Path) -> tuple[PreparedManifest, bytes]:
    raw = _read_bounded_regular_file(path, max_bytes=16 * 1024 * 1024)
    try:
        manifest = PreparedManifest.model_validate_json(raw, strict=True)
    except ValueError as exc:
        raise ControlledImportError("prepared manifest is invalid") from exc
    return manifest, raw


def prepare_source_manifest(
    source_manifest_path: Path,
    *,
    key_file: Path,
    artifact_root: Path,
    prepared_manifest_path: Path,
) -> PreparedManifest:
    """Encrypt every evidence file and atomically persist its DB descriptor."""

    source_manifest, source_raw = load_source_manifest(source_manifest_path)
    if prepared_manifest_path.exists():
        existing, _ = load_prepared_manifest(prepared_manifest_path)
        if existing.source_manifest_sha256 != hashlib.sha256(source_raw).hexdigest():
            raise ControlledImportError("existing prepared manifest belongs to another source")
        return existing
    provider = FileKeyProvider(key_file)
    cipher = SecretStreamCipher(provider)
    cipher.self_test()
    durable = ArtifactStore(
        artifact_root,
        max_bytes=150 * 1024 * 1024,
        total_max_bytes=10 * 1024 * 1024 * 1024,
        staging_max_bytes=512 * 1024 * 1024,
    )
    store = EncryptedArtifactStore(
        durable,
        cipher,
        max_plaintext_bytes=134_217_728,
    )
    source_root = source_manifest_path.parent
    prepared_evidence: list[PreparedEvidence] = []
    for descriptor in source_manifest.evidence:
        source = source_root / descriptor.source_file
        _verify_source_evidence(source, descriptor)
        with source.open("rb") as stream:
            published = store.publish(stream)
        metadata = store.envelope_metadata(published)
        with store.open_verified(published, envelope_metadata=metadata) as verified:
            if hashlib.sha256(verified.read()).hexdigest() != descriptor.plaintext_sha256:
                raise ControlledImportError("encrypted evidence verification failed")
        if (
            published.plaintext_sha256.hex() != descriptor.plaintext_sha256
            or published.plaintext_size != descriptor.plaintext_size
        ):
            raise ControlledImportError("encrypted evidence plaintext identity changed")
        prepared_evidence.append(
            PreparedEvidence(
                evidence_ref=descriptor.evidence_ref,
                display_name=descriptor.display_name,
                declared_media_type=descriptor.declared_media_type,
                plaintext_sha256=descriptor.plaintext_sha256,
                plaintext_size=descriptor.plaintext_size,
                object_ref=published.object_ref,
                ciphertext_sha256=published.ciphertext.sha256.hex(),
                ciphertext_size=published.ciphertext.byte_size,
                storage_key=published.storage_key,
                envelope_schema=ENVELOPE_SCHEMA,
                algorithm=ENVELOPE_ALGORITHM,
                chunk_size=metadata.chunk_size,
                stream_header=metadata.stream_header.hex(),
                wrapped_key_generation=metadata.wrapped_key.generation,
                wrapped_key_nonce=metadata.wrapped_key.nonce.hex(),
                wrapped_key_ciphertext=metadata.wrapped_key.ciphertext.hex(),
                purpose="ledgerbridge-artifact-v2",
            )
        )
    prepared = PreparedManifest(
        schema_version="ledgerbridge.controlled-review-prepared.v1",
        source_manifest_sha256=hashlib.sha256(source_raw).hexdigest(),
        batch_ref=source_manifest.batch_ref,
        generated_at=source_manifest.generated_at,
        source_description=source_manifest.source_description,
        entity=source_manifest.entity,
        business_unit=source_manifest.business_unit,
        categories=source_manifest.categories,
        evidence=tuple(prepared_evidence),
        candidates=source_manifest.candidates,
    )
    _write_new_private_json(prepared_manifest_path, prepared.model_dump(mode="json"))
    return prepared


def import_prepared_manifest(engine: Engine, prepared_manifest_path: Path) -> ImportResult:
    manifest, raw = load_prepared_manifest(prepared_manifest_path)
    prepared_sha256 = hashlib.sha256(raw).digest()
    source_sha256 = bytes.fromhex(manifest.source_manifest_sha256)
    with engine.begin() as connection:
        receipt = connection.execute(
            text(
                "SELECT source_manifest_sha256, prepared_manifest_sha256, evidence_count, "
                "candidate_count, audit_horizon_sequence, audit_horizon_hash "
                "FROM internal_import.controlled_batch_receipt WHERE batch_ref = :batch"
            ),
            {"batch": manifest.batch_ref},
        ).mappings().first()
        if receipt is not None:
            if (
                bytes(receipt["source_manifest_sha256"]) != source_sha256
                or bytes(receipt["prepared_manifest_sha256"]) != prepared_sha256
                or receipt["evidence_count"] != len(manifest.evidence)
                or receipt["candidate_count"] != len(manifest.candidates)
            ):
                raise ControlledImportError("batch receipt conflicts with prepared manifest")
            return ImportResult(
                batch_ref=manifest.batch_ref,
                replayed=True,
                evidence_count=receipt["evidence_count"],
                candidate_count=receipt["candidate_count"],
                audit_horizon_sequence=receipt["audit_horizon_sequence"],
                audit_horizon_hash=bytes(receipt["audit_horizon_hash"]).hex(),
            )
        _preflight_empty_batch(connection, manifest)
        _insert_dimensions(connection, manifest)
        for evidence in manifest.evidence:
            _insert_evidence(connection, manifest, evidence)
        categories = {item.code: item for item in manifest.categories}
        for candidate in manifest.candidates:
            _insert_candidate(connection, manifest, categories[candidate.category_code], candidate)
        horizon = connection.execute(
            text("SELECT sequence, hash FROM public.audit_event ORDER BY sequence DESC LIMIT 1")
        ).mappings().one()
        connection.execute(
            text(
                "INSERT INTO internal_import.controlled_batch_receipt "
                "(batch_ref, source_manifest_sha256, prepared_manifest_sha256, entity_id, "
                "business_unit_id, evidence_count, candidate_count, audit_horizon_sequence, "
                "audit_horizon_hash) VALUES "
                "(:batch, :source_sha, :prepared_sha, :entity, :unit, :evidence_count, "
                ":candidate_count, :sequence, :hash)"
            ),
            {
                "batch": manifest.batch_ref,
                "source_sha": source_sha256,
                "prepared_sha": prepared_sha256,
                "entity": manifest.entity.entity_ref,
                "unit": manifest.business_unit.business_unit_ref,
                "evidence_count": len(manifest.evidence),
                "candidate_count": len(manifest.candidates),
                "sequence": horizon["sequence"],
                "hash": horizon["hash"],
            },
        )
        return ImportResult(
            batch_ref=manifest.batch_ref,
            replayed=False,
            evidence_count=len(manifest.evidence),
            candidate_count=len(manifest.candidates),
            audit_horizon_sequence=horizon["sequence"],
            audit_horizon_hash=bytes(horizon["hash"]).hex(),
        )


def _preflight_empty_batch(connection: Connection, manifest: PreparedManifest) -> None:
    candidate_ids = [item.candidate_ref for item in manifest.candidates]
    evidence_ids = [item.evidence_ref for item in manifest.evidence]
    existing_candidates = connection.execute(
        text("SELECT count(*) FROM public.candidate WHERE id = ANY(:ids)"),
        {"ids": candidate_ids},
    ).scalar_one()
    existing_evidence = connection.execute(
        text("SELECT count(*) FROM public.evidence_object WHERE evidence_ref = ANY(:ids)"),
        {"ids": evidence_ids},
    ).scalar_one()
    if existing_candidates or existing_evidence:
        raise ControlledImportError("unreceipted batch rows already exist")


def _insert_dimensions(connection: Connection, manifest: PreparedManifest) -> None:
    registries = {
        "controlled_upload": "Controlled local evidence upload",
        "outlook": "Controlled Outlook evidence import",
    }
    for channel, description in registries.items():
        connection.execute(
            text(
                "INSERT INTO public.ingest_channel(id, description) VALUES (:id, :description) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": channel, "description": description},
        )
    source_systems = {
        candidate.source_system
        for candidate in manifest.candidates
    }
    for source_system in sorted(source_systems):
        connection.execute(
            text(
                "INSERT INTO public.source_system(id, description) VALUES (:id, :description) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": source_system, "description": "Controlled review evidence source"},
        )
    connection.execute(
        text(
            "INSERT INTO public.entity(id, entity_type, name) VALUES (:id, 'COMPANY', :name) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": manifest.entity.entity_ref, "name": manifest.entity.name},
    )
    connection.execute(
        text(
            "INSERT INTO public.business_unit(id, entity_id, ref, label) "
            "VALUES (:id, :entity, :ref, :label) ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": manifest.business_unit.business_unit_ref,
            "entity": manifest.entity.entity_ref,
            "ref": manifest.business_unit.ref,
            "label": manifest.business_unit.label,
        },
    )
    for category in manifest.categories:
        connection.execute(
            text(
                "INSERT INTO public.reporting_category(id, entity_id, code, label) "
                "VALUES (:id, :entity, :code, :label) ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": category.category_ref,
                "entity": manifest.entity.entity_ref,
                "code": category.code,
                "label": category.label,
            },
        )
    expected = connection.execute(
        text(
            "SELECT e.name, b.entity_id, b.ref, b.label FROM public.entity e "
            "JOIN public.business_unit b ON b.id = :unit WHERE e.id = :entity"
        ),
        {
            "entity": manifest.entity.entity_ref,
            "unit": manifest.business_unit.business_unit_ref,
        },
    ).mappings().one_or_none()
    if expected is None or dict(expected) != {
        "name": manifest.entity.name,
        "entity_id": manifest.entity.entity_ref,
        "ref": manifest.business_unit.ref,
        "label": manifest.business_unit.label,
    }:
        raise ControlledImportError("entity or business unit identity conflicts")


def _insert_evidence(
    connection: Connection,
    manifest: PreparedManifest,
    evidence: PreparedEvidence,
) -> None:
    evidence_payload: dict[str, object] = {
        "evidence_ref": str(evidence.evidence_ref),
        "entity_id": str(manifest.entity.entity_ref),
        "business_unit_id": str(manifest.business_unit.business_unit_ref),
    }
    evidence_audit = _append_audit(
        connection,
        action="evidence.object.create",
        reason="controlled review evidence import",
        payload=evidence_payload,
    )
    connection.execute(
        text(
            "INSERT INTO public.evidence_object "
            "(evidence_ref, entity_id, business_unit_id, media_type, display_name, "
            "plaintext_sha256, plaintext_size, audit_event_id) VALUES "
            "(:evidence, :entity, :unit, 'application/octet-stream', :display_name, "
            ":plaintext_sha, :plaintext_size, :audit)"
        ),
        {
            "evidence": evidence.evidence_ref,
            "entity": manifest.entity.entity_ref,
            "unit": manifest.business_unit.business_unit_ref,
            "display_name": evidence.display_name,
            "plaintext_sha": bytes.fromhex(evidence.plaintext_sha256),
            "plaintext_size": evidence.plaintext_size,
            "audit": evidence_audit,
        },
    )
    blob_ref = UUID(bytes=hashlib.sha256(evidence.evidence_ref.bytes + b"blob-v1").digest()[:16])
    blob_payload: dict[str, object] = {
        "rotation_mode": "GENESIS",
        "blob_ref": str(blob_ref),
        "evidence_ref": str(evidence.evidence_ref),
        "predecessor_blob_ref": None,
        "object_ref": evidence.object_ref,
        "ciphertext_sha256": evidence.ciphertext_sha256,
        "ciphertext_size": evidence.ciphertext_size,
        "storage_key": evidence.storage_key,
        "envelope_schema": evidence.envelope_schema,
        "algorithm": evidence.algorithm,
        "chunk_size": evidence.chunk_size,
        "stream_header": evidence.stream_header,
        "wrapped_key_generation": evidence.wrapped_key_generation,
        "wrapped_key_nonce": evidence.wrapped_key_nonce,
        "wrapped_key_ciphertext": evidence.wrapped_key_ciphertext,
        "purpose": evidence.purpose,
    }
    blob_audit = _append_audit(
        connection,
        action="evidence.blob.version",
        reason="controlled review encrypted evidence genesis",
        payload=blob_payload,
    )
    connection.execute(
        text(
            "INSERT INTO public.encrypted_object_identity(object_ref, evidence_ref) "
            "VALUES (:object_ref, :evidence)"
        ),
        {"object_ref": evidence.object_ref, "evidence": evidence.evidence_ref},
    )
    connection.execute(
        text(
            "INSERT INTO public.encrypted_blob_version "
            "(blob_ref, evidence_ref, object_ref, ciphertext_sha256, ciphertext_size, "
            "storage_key, envelope_schema, algorithm, chunk_size, stream_header, "
            "wrapped_key_generation, wrapped_key_nonce, wrapped_key_ciphertext, purpose, "
            "audit_event_id) VALUES "
            "(:blob, :evidence, :object_ref, :ciphertext_sha, :ciphertext_size, :storage_key, "
            ":envelope_schema, :algorithm, :chunk_size, :stream_header, :generation, :nonce, "
            ":wrapped, :purpose, :audit)"
        ),
        {
            "blob": blob_ref,
            "evidence": evidence.evidence_ref,
            "object_ref": evidence.object_ref,
            "ciphertext_sha": bytes.fromhex(evidence.ciphertext_sha256),
            "ciphertext_size": evidence.ciphertext_size,
            "storage_key": evidence.storage_key,
            "envelope_schema": evidence.envelope_schema,
            "algorithm": evidence.algorithm,
            "chunk_size": evidence.chunk_size,
            "stream_header": bytes.fromhex(evidence.stream_header),
            "generation": evidence.wrapped_key_generation,
            "nonce": bytes.fromhex(evidence.wrapped_key_nonce),
            "wrapped": bytes.fromhex(evidence.wrapped_key_ciphertext),
            "purpose": evidence.purpose,
            "audit": blob_audit,
        },
    )


def _insert_candidate(
    connection: Connection,
    manifest: PreparedManifest,
    category: ImportCategory,
    candidate: ImportCandidate,
) -> None:
    fingerprint = hashlib.sha256(_canonical_json(candidate.model_dump(mode="json"))).digest()
    event_ref = UUID(
        bytes=hashlib.sha256(candidate.candidate_ref.bytes + b"create-event-v1").digest()[:16]
    )
    reason = "controlled review candidate import"
    event_payload: dict[str, object] = {
        "event_ref": str(event_ref),
        "candidate_id": str(candidate.candidate_ref),
        "candidate_ref": str(candidate.candidate_ref),
        "operation_id": str(candidate.operation_id),
        "command_fingerprint": fingerprint.hex(),
        "event_type": "CREATE",
        "action": None,
        "from_revision": None,
        "to_revision": 1,
        "from_status": None,
        "to_status": "PENDING",
        "field_changes": [],
        "conflict_resolutions": [],
        "actor_ref": "system:controlled-review-import",
        "reason": reason,
        "derived_candidate_id": None,
    }
    create_audit = _append_audit(
        connection,
        action="candidate.create",
        reason=reason,
        payload=event_payload,
    )
    occurred_at = connection.execute(
        text("SELECT occurred_at FROM public.audit_event WHERE id = :id"),
        {"id": create_audit},
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO public.candidate "
            "(id, short_id, entity_id, contract_version, created_at) VALUES "
            "(:id, :short_id, :entity, 'ledgerbridge.candidate.v1', :occurred_at)"
        ),
        {
            "id": candidate.candidate_ref,
            "short_id": "C-" + candidate.candidate_ref.hex[:8].upper(),
            "entity": manifest.entity.entity_ref,
            "occurred_at": occurred_at,
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.candidate_source "
            "(candidate_id, ingest_channel_id, source_system_id, source_event_ref, "
            "display_label) VALUES (:candidate, :channel, :system, :source_event, :label)"
        ),
        {
            "candidate": candidate.candidate_ref,
            "channel": _DATABASE_INGEST_CHANNEL[candidate.ingest_channel],
            "system": candidate.source_system,
            "source_event": candidate.source_event_ref,
            "label": candidate.display_label,
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.candidate_revision "
            "(candidate_id, revision, status, business_unit_id, business_unit_ref_snapshot, "
            "business_unit_label_snapshot, category_id, category_code_snapshot, "
            "category_label_snapshot, amount_minor, currency, accounting_month, summary, "
            "confidence_basis_points, created_at, updated_at) VALUES "
            "(:candidate, 1, 'PENDING', :unit, :unit_ref, :unit_label, :category, "
            ":category_code, :category_label, :amount, 'CNY', CAST(:month AS date), :summary, "
            ":confidence, :occurred_at, :occurred_at)"
        ),
        {
            "candidate": candidate.candidate_ref,
            "unit": manifest.business_unit.business_unit_ref,
            "unit_ref": manifest.business_unit.ref,
            "unit_label": manifest.business_unit.label,
            "category": category.category_ref,
            "category_code": category.code,
            "category_label": category.label,
            "amount": candidate.amount_minor,
            "month": candidate.accounting_month + "-01",
            "summary": candidate.summary,
            "confidence": candidate.confidence_basis_points,
            "occurred_at": occurred_at,
        },
    )
    evidence_by_ref = {item.evidence_ref: item for item in manifest.evidence}
    for ordinal, evidence_ref in enumerate(candidate.evidence_refs):
        evidence = evidence_by_ref[evidence_ref]
        connection.execute(
            text(
                "INSERT INTO public.candidate_evidence "
                "(candidate_id, ordinal, evidence_ref, kind, media_type_snapshot, "
                "display_name_snapshot, download_available, candidate_entity_id, "
                "evidence_entity_id, evidence_business_unit_id) VALUES "
                "(:candidate, :ordinal, :evidence, 'ATTACHMENT', :media_type, :display_name, "
                "true, :entity, :entity, :unit)"
            ),
            {
                "candidate": candidate.candidate_ref,
                "ordinal": ordinal,
                "evidence": evidence_ref,
                "media_type": evidence.declared_media_type,
                "display_name": evidence.display_name,
                "entity": manifest.entity.entity_ref,
                "unit": manifest.business_unit.business_unit_ref,
            },
        )
    connection.execute(
        text(
            "INSERT INTO public.candidate_event "
            "(event_ref, candidate_id, operation_id, command_fingerprint, event_type, "
            "to_revision, to_status, actor_ref, reason, occurred_at, audit_event_id) VALUES "
            "(:event, :candidate, :operation, :fingerprint, 'CREATE', 1, 'PENDING', "
            "'system:controlled-review-import', :reason, :occurred_at, :audit)"
        ),
        {
            "event": event_ref,
            "candidate": candidate.candidate_ref,
            "operation": candidate.operation_id,
            "fingerprint": fingerprint,
            "reason": reason,
            "occurred_at": occurred_at,
            "audit": create_audit,
        },
    )


def _append_audit(
    connection: Connection,
    *,
    action: str,
    reason: str,
    payload: dict[str, object],
) -> UUID:
    value = connection.execute(
        text(
            "SELECT public.append_audit_event(:actor, :action, :reason, :rule_version, "
            "CAST(:payload AS jsonb))"
        ),
        {
            "actor": "system:controlled-review-import",
            "action": action,
            "reason": reason,
            "rule_version": "ledgerbridge.controlled-review-import.v1",
            "payload": json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        },
    ).scalar_one()
    if not isinstance(value, UUID):
        raise ControlledImportError("audit append returned an invalid identity")
    return value


def _verify_source_evidence(path: Path, descriptor: SourceEvidence) -> None:
    raw = _read_bounded_regular_file(path, max_bytes=134_217_728)
    if len(raw) != descriptor.plaintext_size:
        raise ControlledImportError("source evidence size changed")
    if hashlib.sha256(raw).hexdigest() != descriptor.plaintext_sha256:
        raise ControlledImportError("source evidence digest changed")


def _read_bounded_regular_file(path: Path, *, max_bytes: int) -> bytes:
    if not path.is_absolute():
        raise ControlledImportError("controlled import paths must be absolute")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ControlledImportError("controlled import file is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ControlledImportError("controlled import file must be regular")
    if metadata.st_size <= 0 or metadata.st_size > max_bytes:
        raise ControlledImportError("controlled import file size is invalid")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ControlledImportError("controlled import file cannot be read") from exc


def _write_new_private_json(path: Path, payload: dict[str, Any]) -> None:
    if not path.is_absolute():
        raise ControlledImportError("prepared manifest path must be absolute")
    encoded = _canonical_json(payload)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with io.FileIO(descriptor, mode="wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
