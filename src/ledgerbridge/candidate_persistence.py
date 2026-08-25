"""Explicit Core persistence boundary for an initial Candidate revision.

This adapter is intentionally not wired into any runtime route. A caller must
inject an already-authorized SQLAlchemy ``Session`` and registered source IDs.
It persists only the Candidate/evidence side of R1; it never creates a
JournalEntry, Posting, or decision event.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ledgerbridge.audit import append_audit_event
from ledgerbridge.candidate_contract import CandidateAggregate, CandidateProjection


class CandidatePersistenceError(RuntimeError):
    """A candidate cannot be persisted without weakening the Core contract."""


@dataclass(frozen=True, slots=True)
class CandidatePersistReceipt:
    candidate_ref: UUID
    audit_event_id: UUID
    revision: int


def persist_initial_candidate(
    session: Session,
    aggregate: CandidateAggregate,
    *,
    ingest_channel_id: str,
    source_system_id: str,
    actor_ref: str = "ledgerbridge:staging",
    reason: str = "candidate created from reviewed intake",
) -> CandidatePersistReceipt:
    """Persist revision 1 and its CREATE audit binding in the current transaction.

    The function deliberately does not commit. The caller owns transaction
    boundaries and must use a database role explicitly authorized for this
    future write path. Existing production roles remain denied by migration.
    Evidence objects must already exist; raw bytes never cross this adapter.
    """

    candidate = aggregate.projection
    _validate_initial(candidate, aggregate, ingest_channel_id, source_system_id, actor_ref, reason)
    _insert_candidate(session, candidate)
    _insert_source(session, candidate, ingest_channel_id, source_system_id)
    _insert_revision(session, candidate)
    _insert_blockers_and_evidence(session, candidate)

    payload = {
        "candidate_ref": str(candidate.candidate_ref),
        "revision": candidate.revision,
        "source_event_ref": str(candidate.source.source_event_ref),
        "source_system": source_system_id,
        "status": candidate.status.value,
    }
    audit_event_id = append_audit_event(
        session,
        actor=actor_ref,
        action="candidate.create",
        reason=reason,
        rule_version=candidate.contract_version,
        payload=payload,
    )
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).digest()
    session.execute(
        text(
            """
            INSERT INTO public.candidate_event
                (candidate_id, operation_id, command_fingerprint, event_type, action,
                 from_revision, to_revision, from_status, to_status, actor_ref, reason,
                 occurred_at, audit_event_id)
            VALUES
                (:candidate_id, :operation_id, :fingerprint, 'CREATE', NULL,
                 NULL, :revision, NULL, :status, :actor_ref, :reason,
                 :occurred_at, :audit_event_id)
            """
        ),
        {
            "candidate_id": candidate.candidate_ref,
            "operation_id": candidate.candidate_ref,
            "fingerprint": fingerprint,
            "revision": candidate.revision,
            "status": candidate.status.value,
            "actor_ref": actor_ref,
            "reason": reason,
            "occurred_at": candidate.created_at,
            "audit_event_id": audit_event_id,
        },
    )
    return CandidatePersistReceipt(candidate.candidate_ref, audit_event_id, candidate.revision)


def _validate_initial(
    candidate: CandidateProjection,
    aggregate: CandidateAggregate,
    ingest_channel_id: str,
    source_system_id: str,
    actor_ref: str,
    reason: str,
) -> None:
    if candidate.revision != 1 or aggregate.events or aggregate.derived_candidates:
        raise CandidatePersistenceError("only a complete revision-1 candidate can be created")
    for name, value in (
        ("ingest_channel_id", ingest_channel_id),
        ("source_system_id", source_system_id),
        ("actor_ref", actor_ref),
        ("reason", reason),
    ):
        maximum = 200 if name == "actor_ref" else 1_000
        if name in {"ingest_channel_id", "source_system_id"}:
            maximum = 64
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise CandidatePersistenceError(f"{name} is invalid")


def _insert_candidate(session: Session, candidate: CandidateProjection) -> None:
    session.execute(
        text(
            """
            INSERT INTO public.candidate
                (id, short_id, entity_id, contract_version, created_at)
            VALUES (:id, :short_id, :entity_id, :contract_version, :created_at)
            """
        ),
        {
            "id": candidate.candidate_ref,
            "short_id": candidate.short_id,
            "entity_id": candidate.entity_ref,
            "contract_version": candidate.contract_version,
            "created_at": candidate.created_at,
        },
    )


def _insert_source(
    session: Session,
    candidate: CandidateProjection,
    ingest_channel_id: str,
    source_system_id: str,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO public.candidate_source
                (candidate_id, ingest_channel_id, source_system_id, source_event_ref,
                 display_label)
            VALUES (:candidate_id, :ingest_channel_id, :source_system_id,
                    :source_event_ref, :display_label)
            """
        ),
        {
            "candidate_id": candidate.candidate_ref,
            "ingest_channel_id": ingest_channel_id,
            "source_system_id": source_system_id,
            "source_event_ref": candidate.source.source_event_ref,
            "display_label": candidate.source.display_label,
        },
    )


def _insert_revision(session: Session, candidate: CandidateProjection) -> None:
    session.execute(
        text(
            """
            INSERT INTO public.candidate_revision
                (candidate_id, revision, status, business_unit_id,
                 business_unit_ref_snapshot, business_unit_label_snapshot, category_id,
                 category_code_snapshot, category_label_snapshot, amount_minor, currency,
                 accounting_month, summary, confidence_basis_points, created_at, updated_at)
            VALUES (
                :candidate_id, :revision, :status,
                (SELECT id FROM public.business_unit
                   WHERE entity_id = :entity_id AND ref = :business_unit_ref),
                :business_unit_ref, :business_unit_label,
                (SELECT id FROM public.reporting_category
                   WHERE entity_id = :entity_id AND code = :category_code),
                :category_code, :category_label, :amount_minor, :currency, :accounting_month,
                :summary, :confidence, :created_at, :updated_at
            )
            """
        ),
        {
            "candidate_id": candidate.candidate_ref,
            "revision": candidate.revision,
            "status": candidate.status.value,
            "entity_id": candidate.entity_ref,
            "business_unit_ref": candidate.business_unit_ref,
            "business_unit_label": candidate.business_unit_label,
            "category_code": candidate.category_code,
            "category_label": candidate.category_label,
            "amount_minor": candidate.amount_minor,
            "currency": candidate.currency,
            "accounting_month": _month_date(candidate.accounting_month),
            "summary": candidate.summary,
            "confidence": candidate.confidence_basis_points,
            "created_at": candidate.created_at,
            "updated_at": candidate.updated_at,
        },
    )


def _insert_blockers_and_evidence(session: Session, candidate: CandidateProjection) -> None:
    for ordinal, blocker in enumerate(candidate.blockers):
        session.execute(
            text(
                """
                INSERT INTO public.candidate_blocker
                    (candidate_id, revision, ordinal, code, message, field,
                     conflict_ref, evidence_ref)
                VALUES (:candidate_id, :revision, :ordinal, :code, :message, :field,
                        :conflict_ref, :evidence_ref)
                """
            ),
            {
                "candidate_id": candidate.candidate_ref,
                "revision": candidate.revision,
                "ordinal": ordinal,
                "code": blocker.code.value,
                "message": blocker.message,
                "field": blocker.field,
                "conflict_ref": blocker.conflict_ref,
                "evidence_ref": blocker.evidence_ref,
            },
        )
    for ordinal, evidence in enumerate(candidate.evidence):
        session.execute(
            text(
                """
                INSERT INTO public.candidate_evidence
                    (candidate_id, ordinal, evidence_ref, kind, media_type_snapshot,
                     display_name_snapshot, download_available)
                VALUES (:candidate_id, :ordinal, :evidence_ref, :kind, :media_type,
                        :display_name, :download_available)
                """
            ),
            {
                "candidate_id": candidate.candidate_ref,
                "ordinal": ordinal,
                "evidence_ref": evidence.evidence_ref,
                "kind": evidence.kind.value,
                "media_type": evidence.media_type,
                "display_name": evidence.display_name,
                "download_available": evidence.download_available,
            },
        )


def _month_date(value: str | None) -> date | None:
    return date.fromisoformat(f"{value}-01") if value is not None else None
