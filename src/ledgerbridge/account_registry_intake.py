"""Private, evidence-bound intake for one Accounting Owner and Managed Account."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ledgerbridge.account_registry import (
    AccountAliasRegistration,
    AccountBusinessUnitAssignment,
    AccountRegistryOperator,
    AccountRegistryPlan,
    ManagedAccountRegistration,
)
from ledgerbridge.artifacts import ArtifactStore, PublishedArtifact
from ledgerbridge.crypto import ENVELOPE_ALGORITHM, ENVELOPE_SCHEMA, SecretStreamCipher
from ledgerbridge.encrypted_artifacts import (
    EncryptedArtifactPublication,
    EncryptedArtifactStore,
    EncryptedEnvelopeMetadata,
    EncryptedPublishedArtifact,
)
from ledgerbridge.file_key_provider import FileKeyProvider
from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.keyring import WrappedKey
from ledgerbridge.models import EntityType
from ledgerbridge.text import contains_unstorable_text

ACCOUNT_REGISTRY_INTAKE_PLAN_SCHEMA = "ledgerbridge.account-registry-intake-plan.v1"
ACCOUNT_REGISTRY_INTAKE_RECEIPT_SCHEMA = "ledgerbridge.account-registry-intake-receipt.v1"
ACCOUNT_REGISTRY_INTAKE_SCHEMA_REVISION = "20260904_0044"

_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REF = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,199}$")
_SAFE_UNIT_REF = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_INSTITUTION_CODE = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")
_ACCOUNT_SUFFIX = re.compile(r"^[0-9]{4,8}$")
_UPPER_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
_MAX_SOURCE_BYTES = 134_217_728
_RUNNER_PRINCIPAL_REF = "workload:account-registry-intake"
_RUNNER_SAN_URI = "spiffe://ledgerbridge.local/account-registry-intake"
_RUNNER_POLICY_GENERATION = 1


class AccountRegistryIntakeError(RuntimeError):
    """The intake could not prove an exact, atomic result."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IntakeEntity(_FrozenModel):
    entity_ref: UUID
    entity_type: EntityType
    name: str = Field(min_length=1, max_length=200)


class IntakeBusinessUnit(_FrozenModel):
    business_unit_ref: UUID
    ref: str = Field(pattern=_SAFE_UNIT_REF.pattern, min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)


class IntakeEvidence(_FrozenModel):
    evidence_ref: UUID
    source_path: str = Field(min_length=1, max_length=4_096)
    display_name: str = Field(min_length=1, max_length=200)
    declared_media_type: str = Field(pattern=_MEDIA_TYPE.pattern, min_length=3, max_length=200)
    plaintext_sha256: str = Field(pattern=_HEX_64.pattern)
    plaintext_size: int = Field(strict=True, ge=1, le=_MAX_SOURCE_BYTES)


class IntakeAlias(_FrozenModel):
    alias_ref: UUID
    alias_kind: str = Field(pattern=_UPPER_CODE.pattern, min_length=1, max_length=32)
    alias_value: str = Field(min_length=1, max_length=300)


class IntakeAssignment(_FrozenModel):
    assignment_ref: UUID
    effective_from: date
    effective_to: date | None = None

    @model_validator(mode="after")
    def dates_are_ordered(self) -> IntakeAssignment:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("assignment dates are invalid")
        return self


class IntakeAccount(_FrozenModel):
    operation_id: UUID
    expected_registry_revision: int = Field(strict=True, ge=0)
    managed_account_ref: UUID
    account_key: str = Field(pattern=_SAFE_REF.pattern, min_length=1, max_length=200)
    institution_code: str = Field(pattern=_INSTITUTION_CODE.pattern, min_length=1, max_length=32)
    account_suffix: str = Field(pattern=_ACCOUNT_SUFFIX.pattern, min_length=4, max_length=8)
    account_kind: str = Field(pattern=_UPPER_CODE.pattern, min_length=1, max_length=32)
    initial_lifecycle: Literal["ACTIVE", "CLOSED"]
    aliases: tuple[IntakeAlias, ...] = Field(min_length=1, max_length=100)
    business_unit_assignment: IntakeAssignment | None = None


class IntakeAudit(_FrozenModel):
    actor_ref: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1_000)


class IntakeStorage(_FrozenModel):
    key_file: str = Field(min_length=1, max_length=4_096)
    artifact_root: str = Field(min_length=1, max_length=4_096)


class AccountRegistryIntakePlan(_FrozenModel):
    schema_version: Literal["ledgerbridge.account-registry-intake-plan.v1"]
    target_revision: str = Field(pattern=_HEX_40.pattern)
    entity: IntakeEntity
    business_unit: IntakeBusinessUnit
    evidence: IntakeEvidence
    account: IntakeAccount
    audit: IntakeAudit
    storage: IntakeStorage

    @model_validator(mode="after")
    def identities_and_text_are_safe(self) -> AccountRegistryIntakePlan:
        values = (
            self.entity.name,
            self.business_unit.label,
            self.evidence.display_name,
            self.audit.actor_ref,
            self.audit.reason,
            *(alias.alias_value for alias in self.account.aliases),
        )
        if any(value != value.strip() or contains_unstorable_text(value) for value in values):
            raise ValueError("intake text is invalid")
        if len({alias.alias_ref for alias in self.account.aliases}) != len(self.account.aliases):
            raise ValueError("alias refs must be unique")
        return self


@dataclass(frozen=True, slots=True)
class LoadedAccountRegistryIntake:
    plan: AccountRegistryIntakePlan
    plan_sha256: str
    source_path: Path
    key_file: Path
    artifact_root: Path
    registry_plan: AccountRegistryPlan
    principal: WorkloadPrincipal


@dataclass(frozen=True, slots=True)
class AccountRegistryIntakeReceipt:
    plan_sha256: str
    operation_id: UUID
    owner_entity_ref: UUID
    business_unit_ref: UUID
    evidence_ref: UUID
    managed_account_ref: UUID
    registry_revision: int
    lifecycle_revision: int
    lifecycle_status: Literal["ACTIVE", "CLOSED"]
    entity_created: bool
    business_unit_created: bool
    evidence_created: bool
    registry_created: bool

    @property
    def created(self) -> bool:
        return any(
            (
                self.entity_created,
                self.business_unit_created,
                self.evidence_created,
                self.registry_created,
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ACCOUNT_REGISTRY_INTAKE_RECEIPT_SCHEMA,
            "plan_sha256": self.plan_sha256,
            "operation_id": str(self.operation_id),
            "owner_entity_ref": str(self.owner_entity_ref),
            "business_unit_ref": str(self.business_unit_ref),
            "evidence_ref": str(self.evidence_ref),
            "managed_account_ref": str(self.managed_account_ref),
            "registry_revision": self.registry_revision,
            "lifecycle_revision": self.lifecycle_revision,
            "lifecycle_status": self.lifecycle_status,
            "created": self.created,
            "entity_created": self.entity_created,
            "business_unit_created": self.business_unit_created,
            "evidence_created": self.evidence_created,
            "registry_created": self.registry_created,
        }


@dataclass(frozen=True, slots=True)
class AccountRegistryIntakeInventory:
    entities: int
    business_units: int
    evidence_objects: int
    encrypted_object_identities: int
    encrypted_blob_versions: int
    managed_accounts: int
    managed_account_lifecycles: int
    account_registry_operations: int
    managed_account_aliases: int
    account_business_unit_assignments: int


def load_private_account_registry_intake(path: Path) -> LoadedAccountRegistryIntake:
    """Load one strict private plan and bind every derived command value."""

    try:
        raw = _read_private_file(path, maximum=1024 * 1024)
        payload = json.loads(raw)
        plan = AccountRegistryIntakePlan.model_validate(payload)
        source_path = _absolute_path(plan.evidence.source_path)
        key_file = _absolute_path(plan.storage.key_file)
        artifact_root = _absolute_path(plan.storage.artifact_root)
        assignment = plan.account.business_unit_assignment
        registry_assignment: tuple[AccountBusinessUnitAssignment, ...] = ()
        if assignment is not None:
            registry_assignment = (
                AccountBusinessUnitAssignment(
                    assignment_ref=assignment.assignment_ref,
                    managed_account_ref=plan.account.managed_account_ref,
                    business_unit_id=plan.business_unit.business_unit_ref,
                    business_unit_ref_snapshot=plan.business_unit.ref,
                    business_unit_label_snapshot=plan.business_unit.label,
                    effective_from=assignment.effective_from,
                    effective_to=assignment.effective_to,
                ),
            )
        registry_plan = AccountRegistryPlan(
            operation_id=plan.account.operation_id,
            owner_entity_ref=plan.entity.entity_ref,
            expected_owner_kind=plan.entity.entity_type,
            expected_registry_revision=plan.account.expected_registry_revision,
            actor_ref=plan.audit.actor_ref,
            reason=plan.audit.reason,
            accounts=(
                ManagedAccountRegistration(
                    managed_account_ref=plan.account.managed_account_ref,
                    admission_evidence_ref=plan.evidence.evidence_ref,
                    account_key=plan.account.account_key,
                    institution_code=plan.account.institution_code,
                    account_suffix=plan.account.account_suffix,
                    account_kind=plan.account.account_kind,
                    aliases=tuple(
                        AccountAliasRegistration(
                            alias_ref=alias.alias_ref,
                            alias_kind=alias.alias_kind,
                            alias_value=alias.alias_value,
                        )
                        for alias in plan.account.aliases
                    ),
                ),
            ),
            business_unit_assignments=registry_assignment,
        )
        principal = WorkloadPrincipal(
            principal_ref=_RUNNER_PRINCIPAL_REF,
            san_uri=_RUNNER_SAN_URI,
            policy_generation=_RUNNER_POLICY_GENERATION,
            capabilities=frozenset({Capability.ACCOUNT_REGISTRY_WRITE}),
            grants=(EntityGrant(entity_ref=plan.entity.entity_ref, allow_account_registry=True),),
        )
        return LoadedAccountRegistryIntake(
            plan=plan,
            plan_sha256=hashlib.sha256(_canonical_json(payload)).hexdigest(),
            source_path=source_path,
            key_file=key_file,
            artifact_root=artifact_root,
            registry_plan=registry_plan,
            principal=principal,
        )
    except AccountRegistryIntakeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise AccountRegistryIntakeError(
            "private account intake plan is unavailable or invalid"
        ) from None


def run_transactional_account_registry_intake(
    engine: Engine,
    loaded: LoadedAccountRegistryIntake,
    *,
    commit: bool,
) -> AccountRegistryIntakeReceipt:
    """Apply and replay one intake in a database-owner transaction."""

    if type(commit) is not bool:
        raise AccountRegistryIntakeError("account intake transaction mode is invalid")
    _verify_source_file(loaded.source_path, loaded.plan.evidence)
    store = _build_evidence_store(loaded.key_file, loaded.artifact_root)
    publication: EncryptedArtifactPublication | None = None
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(connection, join_transaction_mode="rollback_only")
        try:
            _require_database_owner_target(session)
            receipt, publication = _apply_once(session, store, loaded)
            before_replay = _read_target_inventory(session, loaded.plan)
            audit_before_replay = _current_transaction_audit_count(session)

            replay, replay_publication = _apply_once(session, store, loaded)
            if replay_publication is not None:
                replay_publication.abort()
                raise AccountRegistryIntakeError("account intake replay staged new evidence")
            after_replay = _read_target_inventory(session, loaded.plan)
            audit_after_replay = _current_transaction_audit_count(session)
            _require_exact_replay(
                receipt,
                replay,
                before_inventory=before_replay,
                after_inventory=after_replay,
                audit_before=audit_before_replay,
                audit_after=audit_after_replay,
            )
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            if commit:
                transaction.commit()
                if publication is not None:
                    publication.commit()
            else:
                transaction.rollback()
                if publication is not None:
                    publication.abort()
            return receipt
        except BaseException:
            if transaction.is_active:
                transaction.rollback()
            if publication is not None:
                publication.abort()
            raise
        finally:
            session.close()


def _require_database_owner_target(session: Session) -> None:
    row = (
        session.execute(
            text(
                "SELECT current_user AS current_user, session_user AS session_user, "
                "current_database() AS database_name, "
                "(SELECT version_num FROM public.alembic_version) AS schema_revision, "
                "current_setting('transaction_read_only') AS transaction_read_only"
            )
        )
        .mappings()
        .one()
    )
    if dict(row) != {
        "current_user": "ledgerbridge_owner",
        "session_user": "ledgerbridge_owner",
        "database_name": "ledgerbridge",
        "schema_revision": ACCOUNT_REGISTRY_INTAKE_SCHEMA_REVISION,
        "transaction_read_only": "off",
    }:
        raise AccountRegistryIntakeError("account intake database owner target is invalid")


def _apply_once(
    session: Session,
    store: EncryptedArtifactStore,
    loaded: LoadedAccountRegistryIntake,
) -> tuple[AccountRegistryIntakeReceipt, EncryptedArtifactPublication | None]:
    publication: EncryptedArtifactPublication | None = None
    try:
        entity_created, unit_created = _ensure_exact_scope(session, loaded.plan)
        evidence_created, publication = _ensure_exact_evidence(session, store, loaded)
        registry = AccountRegistryOperator(lambda: session).apply(
            loaded.registry_plan,
            principal=loaded.principal,
            session=session,
        )
        if (
            registry.operation_id != loaded.registry_plan.operation_id
            or registry.owner_entity_ref != loaded.plan.entity.entity_ref
            or registry.registry_revision != loaded.registry_plan.expected_registry_revision + 1
            or registry.managed_account_refs != (loaded.plan.account.managed_account_ref,)
        ):
            raise AccountRegistryIntakeError("account registry receipt conflicts with intake")
        lifecycle_revision, lifecycle_status = _ensure_initial_lifecycle(
            session,
            loaded.plan,
            registry_created=registry.created,
        )
        return (
            AccountRegistryIntakeReceipt(
                plan_sha256=loaded.plan_sha256,
                operation_id=registry.operation_id,
                owner_entity_ref=registry.owner_entity_ref,
                business_unit_ref=loaded.plan.business_unit.business_unit_ref,
                evidence_ref=loaded.plan.evidence.evidence_ref,
                managed_account_ref=loaded.plan.account.managed_account_ref,
                registry_revision=registry.registry_revision,
                lifecycle_revision=lifecycle_revision,
                lifecycle_status=lifecycle_status,
                entity_created=entity_created,
                business_unit_created=unit_created,
                evidence_created=evidence_created,
                registry_created=registry.created,
            ),
            publication,
        )
    except BaseException:
        if publication is not None:
            publication.abort()
        raise


def _read_target_inventory(
    session: Session,
    plan: AccountRegistryIntakePlan,
) -> AccountRegistryIntakeInventory:
    row = (
        session.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM public.entity WHERE id=:entity) AS entities, "
                "(SELECT count(*) FROM public.business_unit WHERE id=:unit) AS business_units, "
                "(SELECT count(*) FROM public.evidence_object WHERE evidence_ref=:evidence) "
                "AS evidence_objects, "
                "(SELECT count(*) FROM public.encrypted_object_identity "
                "WHERE evidence_ref=:evidence) AS encrypted_object_identities, "
                "(SELECT count(*) FROM public.encrypted_blob_version "
                "WHERE evidence_ref=:evidence) AS encrypted_blob_versions, "
                "(SELECT count(*) FROM public.managed_account "
                "WHERE managed_account_ref=:account) AS managed_accounts, "
                "(SELECT count(*) FROM public.managed_account_lifecycle "
                "WHERE managed_account_ref=:account) AS managed_account_lifecycles, "
                "(SELECT count(*) FROM public.account_registry_operation "
                "WHERE operation_id=:operation) AS account_registry_operations, "
                "(SELECT count(*) FROM public.managed_account_alias "
                "WHERE managed_account_ref=:account) AS managed_account_aliases, "
                "(SELECT count(*) FROM public.account_business_unit_assignment "
                "WHERE managed_account_ref=:account) AS account_business_unit_assignments"
            ),
            {
                "entity": plan.entity.entity_ref,
                "unit": plan.business_unit.business_unit_ref,
                "evidence": plan.evidence.evidence_ref,
                "account": plan.account.managed_account_ref,
                "operation": plan.account.operation_id,
            },
        )
        .mappings()
        .one()
    )
    try:
        return AccountRegistryIntakeInventory(**{key: int(value) for key, value in row.items()})
    except (TypeError, ValueError):
        raise AccountRegistryIntakeError("account intake inventory is invalid") from None


def _current_transaction_audit_count(session: Session) -> int:
    value = session.execute(
        text("SELECT count(*) FROM public.audit_event WHERE xmin = pg_current_xact_id()::text::xid")
    ).scalar_one()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AccountRegistryIntakeError("account intake audit inventory is invalid")
    return int(value)


def _require_exact_replay(
    first: AccountRegistryIntakeReceipt,
    replay: AccountRegistryIntakeReceipt,
    *,
    before_inventory: AccountRegistryIntakeInventory,
    after_inventory: AccountRegistryIntakeInventory,
    audit_before: int,
    audit_after: int,
) -> None:
    if (
        replay.plan_sha256 != first.plan_sha256
        or replay.operation_id != first.operation_id
        or replay.owner_entity_ref != first.owner_entity_ref
        or replay.business_unit_ref != first.business_unit_ref
        or replay.evidence_ref != first.evidence_ref
        or replay.managed_account_ref != first.managed_account_ref
        or replay.registry_revision != first.registry_revision
        or replay.lifecycle_revision != first.lifecycle_revision
        or replay.lifecycle_status != first.lifecycle_status
        or replay.entity_created
        or replay.business_unit_created
        or replay.evidence_created
        or replay.registry_created
        or after_inventory != before_inventory
        or audit_after != audit_before
    ):
        raise AccountRegistryIntakeError("account intake replay changed target inventory")


def _ensure_exact_scope(
    session: Session,
    plan: AccountRegistryIntakePlan,
) -> tuple[bool, bool]:
    entity = plan.entity
    unit = plan.business_unit
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"account-intake-entity:{entity.entity_type.value}:{entity.name.casefold()}"},
    )
    matches = (
        session.execute(
            text(
                "SELECT id, entity_type::text AS entity_type, name FROM public.entity "
                "WHERE id=:entity OR (entity_type::text=:entity_type AND name=:name)"
            ),
            {
                "entity": entity.entity_ref,
                "entity_type": entity.entity_type.value,
                "name": entity.name,
            },
        )
        .mappings()
        .all()
    )
    entity_created = not matches
    if matches and (
        len(matches) != 1
        or matches[0]["id"] != entity.entity_ref
        or matches[0]["entity_type"] != entity.entity_type.value
        or matches[0]["name"] != entity.name
    ):
        raise AccountRegistryIntakeError("accounting owner identity conflicts")
    if entity_created:
        session.execute(
            text(
                "INSERT INTO public.entity(id, entity_type, name) "
                "VALUES (:entity, CAST(:entity_type AS public.entity_type), :name)"
            ),
            {
                "entity": entity.entity_ref,
                "entity_type": entity.entity_type.value,
                "name": entity.name,
            },
        )

    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"account-intake-unit:{entity.entity_ref}:{unit.ref}"},
    )
    unit_matches = (
        session.execute(
            text(
                "SELECT id, entity_id, ref, label, retired_at FROM public.business_unit "
                "WHERE id=:unit OR (entity_id=:entity AND (ref=:ref OR label=:label))"
            ),
            {
                "unit": unit.business_unit_ref,
                "entity": entity.entity_ref,
                "ref": unit.ref,
                "label": unit.label,
            },
        )
        .mappings()
        .all()
    )
    unit_created = not unit_matches
    if unit_matches and (
        len(unit_matches) != 1
        or unit_matches[0]["id"] != unit.business_unit_ref
        or unit_matches[0]["entity_id"] != entity.entity_ref
        or unit_matches[0]["ref"] != unit.ref
        or unit_matches[0]["label"] != unit.label
        or unit_matches[0]["retired_at"] is not None
    ):
        raise AccountRegistryIntakeError("business unit identity conflicts")
    if unit_created:
        session.execute(
            text(
                "INSERT INTO public.business_unit(id, entity_id, ref, label) "
                "VALUES (:unit, :entity, :ref, :label)"
            ),
            {
                "unit": unit.business_unit_ref,
                "entity": entity.entity_ref,
                "ref": unit.ref,
                "label": unit.label,
            },
        )
    return entity_created, unit_created


def _ensure_exact_evidence(
    session: Session,
    store: EncryptedArtifactStore,
    loaded: LoadedAccountRegistryIntake,
) -> tuple[bool, EncryptedArtifactPublication | None]:
    descriptor = loaded.plan.evidence
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"account-intake-evidence:{descriptor.evidence_ref}"},
    )
    matches = (
        session.execute(
            text(
                "SELECT evidence_ref, entity_id, business_unit_id, media_type, display_name, "
                "plaintext_sha256, plaintext_size FROM public.evidence_object "
                "WHERE evidence_ref=:evidence"
            ),
            {
                "evidence": descriptor.evidence_ref,
            },
        )
        .mappings()
        .all()
    )
    if matches:
        expected = {
            "evidence_ref": descriptor.evidence_ref,
            "entity_id": loaded.plan.entity.entity_ref,
            "business_unit_id": loaded.plan.business_unit.business_unit_ref,
            "media_type": descriptor.declared_media_type,
            "display_name": descriptor.display_name,
            "plaintext_sha256": bytes.fromhex(descriptor.plaintext_sha256),
            "plaintext_size": descriptor.plaintext_size,
        }
        if len(matches) != 1 or dict(matches[0]) != expected:
            raise AccountRegistryIntakeError("admission evidence identity conflicts")
        _verify_existing_encrypted_evidence(session, store, descriptor)
        return False, None

    publication: EncryptedArtifactPublication | None = None
    try:
        with loaded.source_path.open("rb") as source:
            publication = store.begin_publication(source)
        artifact = publication.artifact
        metadata = store.envelope_metadata(artifact)
        with store.open_verified(artifact, envelope_metadata=metadata) as verified:
            verified_digest = hashlib.sha256(verified.read()).hexdigest()
        if (
            artifact.plaintext_sha256.hex() != descriptor.plaintext_sha256
            or artifact.plaintext_size != descriptor.plaintext_size
            or verified_digest != descriptor.plaintext_sha256
        ):
            raise AccountRegistryIntakeError("encrypted evidence source identity conflicts")
        _insert_fresh_evidence(session, loaded, artifact, metadata)
        return True, publication
    except BaseException:
        if publication is not None:
            publication.abort()
        raise


def _insert_fresh_evidence(
    session: Session,
    loaded: LoadedAccountRegistryIntake,
    artifact: EncryptedPublishedArtifact,
    metadata: EncryptedEnvelopeMetadata,
) -> None:
    plan = loaded.plan
    descriptor = plan.evidence
    evidence_audit = _append_audit(
        session,
        actor=plan.audit.actor_ref,
        action="evidence.object.create",
        reason=plan.audit.reason,
        rule_version="ledgerbridge.account-registry-intake.v1",
        payload={
            "evidence_ref": str(descriptor.evidence_ref),
            "entity_id": str(plan.entity.entity_ref),
            "business_unit_id": str(plan.business_unit.business_unit_ref),
        },
    )
    session.execute(
        text(
            "INSERT INTO public.evidence_object "
            "(evidence_ref, entity_id, business_unit_id, media_type, display_name, "
            "plaintext_sha256, plaintext_size, audit_event_id) VALUES "
            "(:evidence, :entity, :unit, :media_type, :display_name, :digest, :size, :audit)"
        ),
        {
            "evidence": descriptor.evidence_ref,
            "entity": plan.entity.entity_ref,
            "unit": plan.business_unit.business_unit_ref,
            "media_type": descriptor.declared_media_type,
            "display_name": descriptor.display_name,
            "digest": bytes.fromhex(descriptor.plaintext_sha256),
            "size": descriptor.plaintext_size,
            "audit": evidence_audit,
        },
    )
    blob_ref = UUID(bytes=hashlib.sha256(descriptor.evidence_ref.bytes + b"blob-v1").digest()[:16])
    blob_payload: dict[str, object] = {
        "rotation_mode": "GENESIS",
        "blob_ref": str(blob_ref),
        "evidence_ref": str(descriptor.evidence_ref),
        "predecessor_blob_ref": None,
        "object_ref": artifact.object_ref,
        "ciphertext_sha256": artifact.ciphertext.sha256.hex(),
        "ciphertext_size": artifact.ciphertext.byte_size,
        "storage_key": artifact.storage_key,
        "envelope_schema": ENVELOPE_SCHEMA,
        "algorithm": ENVELOPE_ALGORITHM,
        "chunk_size": metadata.chunk_size,
        "stream_header": metadata.stream_header.hex(),
        "wrapped_key_generation": metadata.wrapped_key.generation,
        "wrapped_key_nonce": metadata.wrapped_key.nonce.hex(),
        "wrapped_key_ciphertext": metadata.wrapped_key.ciphertext.hex(),
        "purpose": "ledgerbridge-artifact-v2",
    }
    blob_audit = _append_audit(
        session,
        actor=plan.audit.actor_ref,
        action="evidence.blob.version",
        reason=plan.audit.reason,
        rule_version="ledgerbridge.account-registry-intake.v1",
        payload=blob_payload,
    )
    session.execute(
        text(
            "INSERT INTO public.encrypted_object_identity(object_ref, evidence_ref) "
            "VALUES (:object_ref, :evidence)"
        ),
        {"object_ref": artifact.object_ref, "evidence": descriptor.evidence_ref},
    )
    session.execute(
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
            "evidence": descriptor.evidence_ref,
            "object_ref": artifact.object_ref,
            "ciphertext_sha": artifact.ciphertext.sha256,
            "ciphertext_size": artifact.ciphertext.byte_size,
            "storage_key": artifact.storage_key,
            "envelope_schema": ENVELOPE_SCHEMA,
            "algorithm": ENVELOPE_ALGORITHM,
            "chunk_size": metadata.chunk_size,
            "stream_header": metadata.stream_header,
            "generation": metadata.wrapped_key.generation,
            "nonce": metadata.wrapped_key.nonce,
            "wrapped": metadata.wrapped_key.ciphertext,
            "purpose": "ledgerbridge-artifact-v2",
            "audit": blob_audit,
        },
    )


def _verify_existing_encrypted_evidence(
    session: Session,
    store: EncryptedArtifactStore,
    descriptor: IntakeEvidence,
) -> None:
    rows = (
        session.execute(
            text(
                "SELECT identity.object_ref, blob.ciphertext_sha256, blob.ciphertext_size, "
                "blob.storage_key, blob.envelope_schema, blob.algorithm, blob.chunk_size, "
                "blob.stream_header, blob.wrapped_key_generation, blob.wrapped_key_nonce, "
                "blob.wrapped_key_ciphertext, blob.purpose "
                "FROM public.encrypted_object_identity identity "
                "JOIN public.encrypted_blob_version blob "
                "ON blob.object_ref=identity.object_ref "
                "AND blob.evidence_ref=identity.evidence_ref "
                "WHERE identity.evidence_ref=:evidence AND NOT EXISTS ("
                "SELECT 1 FROM public.encrypted_blob_version child "
                "WHERE child.predecessor_blob_ref=blob.blob_ref)"
            ),
            {"evidence": descriptor.evidence_ref},
        )
        .mappings()
        .all()
    )
    if len(rows) != 1:
        raise AccountRegistryIntakeError("encrypted admission evidence lineage conflicts")
    row = rows[0]
    if (
        row["envelope_schema"] != ENVELOPE_SCHEMA
        or row["algorithm"] != ENVELOPE_ALGORITHM
        or row["purpose"] != "ledgerbridge-artifact-v2"
    ):
        raise AccountRegistryIntakeError("encrypted admission evidence metadata conflicts")
    artifact = EncryptedPublishedArtifact(
        object_ref=row["object_ref"],
        plaintext_sha256=bytes.fromhex(descriptor.plaintext_sha256),
        plaintext_size=descriptor.plaintext_size,
        ciphertext=PublishedArtifact(
            sha256=bytes(row["ciphertext_sha256"]),
            byte_size=row["ciphertext_size"],
            storage_key=row["storage_key"],
            created=False,
        ),
    )
    metadata = EncryptedEnvelopeMetadata(
        chunk_size=row["chunk_size"],
        stream_header=bytes(row["stream_header"]),
        wrapped_key=WrappedKey(
            generation=row["wrapped_key_generation"],
            nonce=bytes(row["wrapped_key_nonce"]),
            ciphertext=bytes(row["wrapped_key_ciphertext"]),
        ),
    )
    with store.open_verified(artifact, envelope_metadata=metadata) as verified:
        if hashlib.sha256(verified.read()).hexdigest() != descriptor.plaintext_sha256:
            raise AccountRegistryIntakeError("encrypted admission evidence identity conflicts")


def _ensure_initial_lifecycle(
    session: Session,
    plan: AccountRegistryIntakePlan,
    *,
    registry_created: bool,
) -> tuple[int, Literal["ACTIVE", "CLOSED"]]:
    account_ref = plan.account.managed_account_ref
    rows = (
        session.execute(
            text(
                "SELECT revision, status FROM public.managed_account_lifecycle "
                "WHERE managed_account_ref=:account ORDER BY revision"
            ),
            {"account": account_ref},
        )
        .mappings()
        .all()
    )
    rendered_rows = [dict(row) for row in rows]
    if registry_created and rendered_rows != [{"revision": 1, "status": "ACTIVE"}]:
        raise AccountRegistryIntakeError("managed account initial lifecycle conflicts")
    if not registry_created:
        expected = [{"revision": 1, "status": "ACTIVE"}]
        if plan.account.initial_lifecycle == "CLOSED":
            expected.append({"revision": 2, "status": "CLOSED"})
        if len(rendered_rows) < len(expected) or rendered_rows[: len(expected)] != expected:
            raise AccountRegistryIntakeError("managed account lifecycle replay conflicts")
        if any(
            row.get("revision") != revision
            or row.get("status") not in {"ACTIVE", "INACTIVE", "CLOSED"}
            for revision, row in enumerate(rendered_rows, start=1)
        ):
            raise AccountRegistryIntakeError("managed account lifecycle replay conflicts")
        return len(expected), plan.account.initial_lifecycle
    if plan.account.initial_lifecycle == "ACTIVE":
        return 1, "ACTIVE"

    audit_ref = _append_audit(
        session,
        actor=plan.audit.actor_ref,
        action="managed_account.lifecycle",
        reason=plan.audit.reason,
        rule_version="ledgerbridge.bank-statement.v1",
        payload={
            "managed_account_ref": str(account_ref),
            "revision": 2,
            "status": "CLOSED",
        },
    )
    session.execute(
        text(
            "INSERT INTO public.managed_account_lifecycle "
            "(managed_account_ref, revision, status, audit_event_id, effective_at) "
            "VALUES (:account, 2, 'CLOSED', :audit, "
            "(SELECT occurred_at FROM public.audit_event WHERE id=:audit))"
        ),
        {"account": account_ref, "audit": audit_ref},
    )
    return 2, "CLOSED"


def _append_audit(
    session: Session,
    *,
    actor: str,
    action: str,
    reason: str,
    rule_version: str,
    payload: dict[str, object],
) -> UUID:
    value = session.execute(
        text(
            "SELECT public.append_audit_event(:actor, :action, :reason, :rule_version, "
            "CAST(:payload AS jsonb))"
        ),
        {
            "actor": actor,
            "action": action,
            "reason": reason,
            "rule_version": rule_version,
            "payload": json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ).scalar_one()
    if not isinstance(value, UUID):
        raise AccountRegistryIntakeError("audit append returned an invalid identity")
    return value


def _build_evidence_store(key_file: Path, artifact_root: Path) -> EncryptedArtifactStore:
    provider = FileKeyProvider(key_file)
    cipher = SecretStreamCipher(provider)
    cipher.self_test()
    return EncryptedArtifactStore(
        ArtifactStore(
            artifact_root,
            max_bytes=150 * 1024 * 1024,
            total_max_bytes=10 * 1024 * 1024 * 1024,
            staging_max_bytes=512 * 1024 * 1024,
        ),
        cipher,
        max_plaintext_bytes=_MAX_SOURCE_BYTES,
    )


def _verify_source_file(path: Path, descriptor: IntakeEvidence) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AccountRegistryIntakeError("account intake source is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AccountRegistryIntakeError("account intake source must be a regular file")
    if metadata.st_size != descriptor.plaintext_size:
        raise AccountRegistryIntakeError("account intake source identity changed")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise AccountRegistryIntakeError("account intake source cannot be read") from exc
    if digest.hexdigest() != descriptor.plaintext_sha256:
        raise AccountRegistryIntakeError("account intake source identity changed")


def _read_private_file(path: Path, *, maximum: int) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        raise ValueError
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
        raise ValueError
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError
    return path.read_bytes()


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError
    return path


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
