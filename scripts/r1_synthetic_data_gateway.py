"""Loopback-only JSON data gateway for the R1 synthetic workflow.

This launcher is deliberately process-local: it accepts one bounded message
and its evidence bytes, runs the reviewed Hermes admission/triage and candidate
intent boundary, and exposes the resulting candidate projection. It never
writes PostgreSQL, an artifact store, a JournalEntry, or a Posting.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from ledgerbridge.candidate_contract import (
    Blocker,
    BlockerCode,
    CandidateAggregate,
    CandidateCommand,
    CandidateContractError,
    CandidateProjection,
    CandidateStatus,
    EvidenceKind,
    EvidenceReference,
    IngestChannel,
    ReviewSummary,
    SourceProjection,
    apply_candidate_command,
)
from ledgerbridge.candidate_intent import EvidenceBinding, create_candidate_intent
from ledgerbridge.hermes_message import HermesPrivateMessage, classify_private_message
from ledgerbridge.hermes_triage import (
    HermesTriageAction,
    SyntheticKeywordHermesTriageClassifier,
    UnavailableHermesTriageClassifier,
    triage_admitted_message,
)
from ledgerbridge.mail_eml import ParsedEml, parse_eml
from ledgerbridge.synthetic_persistence import SyntheticOutputStore, SyntheticPersistenceError

MAX_EVIDENCE_BYTES = 1_048_576
PRIMARY_PROFILE = "profile:primary"
ACTIVATED_AT = datetime(2026, 8, 25, tzinfo=UTC)


class IntakeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_ref: UUID
    media_type: str = Field(min_length=1, max_length=200)
    filename: str | None = Field(default=None, max_length=255)
    content_base64: str = Field(min_length=1, max_length=2_000_000)
    business_unit_ref: str | None = Field(default=None, max_length=200)


class IntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=300)
    source_event_ref: UUID
    profile_ref: str = Field(default=PRIMARY_PROFILE, min_length=1, max_length=200)
    profile_kind: str = "primary"
    chat_kind: str = "private"
    sender_kind: str = "user"
    sent_at: datetime = ACTIVATED_AT
    # Synthetic-only admission watermark. Real connectors must use the
    # configured deployment watermark rather than allowing caller control.
    activation_at: datetime = ACTIVATED_AT
    text: str = Field(default="", max_length=1_000_000)
    source_subject: str | None = Field(default=None, max_length=500)
    source_sender: str | None = Field(default=None, max_length=500)
    source_forwarder: str | None = Field(default=None, max_length=500)
    has_attachments: bool = False
    entity_ref: UUID
    evidence: tuple[IntakeEvidence, ...] = Field(min_length=1, max_length=32)


class EvidenceOutput(BaseModel):
    evidence_ref: UUID
    filename: str | None
    media_type: str
    sha256: str
    size_bytes: int
    business_unit_ref: str | None


class IntakeOutput(BaseModel):
    mode: str = "synthetic"
    source_format: str
    source_subject: str | None
    source_sender: str | None
    source_forwarder: str | None
    source_category: str
    source_received_at: datetime
    disposition: str
    triage_label: str
    triage_action: str
    triage_reason: str
    candidate_ref: UUID | None = None
    source_message_id: str
    source_event_ref: UUID
    entity_ref: UUID
    evidence: tuple[EvidenceOutput, ...]
    writes_posting: bool = False


class CandidateCommandRequest(BaseModel):
    """Local staging command plus the explicitly trusted actor identity."""

    command: dict[str, object]
    actor_ref: str = Field(min_length=1, max_length=200)


STORE: dict[UUID, IntakeOutput] = {}
PERSISTENCE_STORE: SyntheticOutputStore | None = None
app = FastAPI(
    title="LedgerBridge R1 Synthetic Data Gateway",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _decode_evidence(item: IntakeEvidence) -> tuple[bytes, str]:
    try:
        content = base64.b64decode(item.content_base64, validate=True)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid evidence encoding"
        ) from exc
    if not content or len(content) > MAX_EVIDENCE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "evidence exceeds demo limit")
    return content, hashlib.sha256(content).hexdigest()


def _mail_category(
    source_sender: str | None, source_subject: str | None, has_attachments: bool
) -> str:
    sender = (source_sender or "").casefold()
    subject = (source_subject or "").strip()
    if (
        sender == "service@mail.mybank.cn"
        and subject.startswith("浙江网商银行电子凭证-")
        and has_attachments
    ):
        return "MYBANK_DAILY_STATEMENT"
    if sender == "hsprocess@trip.com" and "发票" in subject and has_attachments:
        return "CTRIP_INVOICE"
    if has_attachments:
        return "ATTACHMENT_REVIEW"
    return "GENERAL_MAIL"


def _build_output(
    message: HermesPrivateMessage,
    *,
    source_event_ref: UUID,
    entity_ref: UUID,
    evidence: tuple[tuple[UUID, str, bytes, str | None, str | None], ...],
    activated_at: datetime = ACTIVATED_AT,
    source_format: str = "json",
    source_subject: str | None = None,
    source_sender: str | None = None,
    source_forwarder: str | None = None,
    has_attachments: bool = False,
) -> IntakeOutput:
    admission = classify_private_message(
        message,
        primary_profile_ref=PRIMARY_PROFILE,
        activated_at=activated_at,
    )
    classifier = (
        UnavailableHermesTriageClassifier()
        if has_attachments
        else SyntheticKeywordHermesTriageClassifier()
    )
    triage = triage_admitted_message(message, admission, classifier=classifier)
    bindings = tuple(
        EvidenceBinding(
            evidence_ref,
            entity_ref,
            business_unit_ref,
            hashlib.sha256(content).digest(),
            media_type,
        )
        for evidence_ref, media_type, content, business_unit_ref, _ in evidence
    )
    output_evidence = tuple(
        EvidenceOutput(
            evidence_ref=evidence_ref,
            filename=filename,
            media_type=media_type,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            business_unit_ref=business_unit_ref,
        )
        for evidence_ref, media_type, content, business_unit_ref, filename in evidence
    )
    candidate_ref: UUID | None = None
    if triage.action is HermesTriageAction.CANDIDATE:
        candidate = create_candidate_intent(
            message,
            triage,
            candidate_ref=uuid4(),
            source_event_ref=source_event_ref,
            entity_ref=entity_ref,
            evidence=bindings,
            created_at=message.sent_at,
        )
        candidate_ref = candidate.candidate_ref
    output = IntakeOutput(
        source_format=source_format,
        source_subject=source_subject,
        source_sender=source_sender,
        source_forwarder=source_forwarder,
        source_category=_mail_category(source_sender, source_subject, has_attachments),
        source_received_at=message.sent_at,
        disposition=admission.disposition.value,
        triage_label=triage.label.value,
        triage_action=triage.action.value,
        triage_reason=triage.reason,
        candidate_ref=candidate_ref,
        source_message_id=message.message_id,
        source_event_ref=source_event_ref,
        entity_ref=entity_ref,
        evidence=output_evidence,
    )
    if candidate_ref is not None:
        STORE[candidate_ref] = output
        if PERSISTENCE_STORE is not None:
            PERSISTENCE_STORE.save(
                output.model_dump(mode="json"), aggregate=_initial_aggregate(output)
            )
    return output


@app.post("/v1/intake", response_model=IntakeOutput, status_code=status.HTTP_201_CREATED)
def intake(request: IntakeRequest) -> IntakeOutput:
    try:
        message = HermesPrivateMessage(
            request.message_id,
            request.profile_ref,
            request.profile_kind,  # type: ignore[arg-type]
            request.chat_kind,  # type: ignore[arg-type]
            request.sender_kind,  # type: ignore[arg-type]
            request.sent_at,
            request.text,
        )
        decoded: list[tuple[IntakeEvidence, bytes, str]] = [
            (item, *_decode_evidence(item)) for item in request.evidence
        ]
        return _build_output(
            message,
            source_event_ref=request.source_event_ref,
            entity_ref=request.entity_ref,
            evidence=tuple(
                (item.evidence_ref, item.media_type, content, item.business_unit_ref, item.filename)
                for item, content, _ in decoded
            ),
            activated_at=request.activation_at,
            source_subject=request.source_subject,
            source_sender=request.source_sender,
            source_forwarder=request.source_forwarder,
            has_attachments=request.has_attachments,
        )
    except SyntheticPersistenceError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "staging persistence unavailable"
        ) from exc
    except HTTPException:
        raise
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@app.post("/v1/intake/eml", response_model=IntakeOutput, status_code=status.HTTP_201_CREATED)
async def intake_eml(request: Request) -> IntakeOutput:
    entity_header = request.headers.get("x-ledgerbridge-entity-ref")
    try:
        entity_ref = UUID(entity_header or "")
        parsed = parse_eml(await request.body())
        return _build_eml_output(parsed, entity_ref=entity_ref)
    except SyntheticPersistenceError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "staging persistence unavailable"
        ) from exc
    except HTTPException:
        raise
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


def _build_eml_output(parsed: ParsedEml, *, entity_ref: UUID) -> IntakeOutput:
    message = HermesPrivateMessage(
        parsed.message_id,
        PRIMARY_PROFILE,
        "primary",
        "private",
        "user",
        parsed.received_at,
        f"{parsed.subject}\n{parsed.text}".strip(),
    )
    source_event_ref = uuid5(NAMESPACE_URL, f"ledgerbridge:eml:event:{parsed.message_id}")
    evidence: list[tuple[UUID, str, bytes, str | None, str | None]] = []
    if parsed.attachments:
        for attachment in parsed.attachments:
            evidence.append(
                (
                    uuid5(NAMESPACE_URL, f"ledgerbridge:eml:evidence:{attachment.attachment_id}"),
                    attachment.media_type,
                    attachment.content,
                    None,
                    attachment.filename,
                )
            )
    else:
        evidence.append(
            (
                uuid5(NAMESPACE_URL, f"ledgerbridge:eml:evidence:{parsed.message_id}"),
                "message/rfc822",
                f"Subject: {parsed.subject}\n\n{parsed.text}".encode(),
                None,
                "message.eml",
            )
        )
    return _build_output(
        message,
        source_event_ref=source_event_ref,
        entity_ref=entity_ref,
        evidence=tuple(evidence),
        activated_at=parsed.received_at,
        source_format="eml",
        source_subject=parsed.subject,
    )


@app.get("/v1/candidates", response_model=list[IntakeOutput])
def candidates() -> list[IntakeOutput]:
    if PERSISTENCE_STORE is None:
        return list(STORE.values())
    persisted = [IntakeOutput.model_validate(item) for item in PERSISTENCE_STORE.list()]
    persisted_refs = {item.candidate_ref for item in persisted}
    return [*persisted, *(item for ref, item in STORE.items() if ref not in persisted_refs)]


@app.post(
    "/v1/candidates/{candidate_ref}/command",
    response_model=CandidateAggregate,
    status_code=status.HTTP_200_OK,
)
def candidate_command(candidate_ref: UUID, request: CandidateCommandRequest) -> CandidateAggregate:
    if PERSISTENCE_STORE is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "staging persistence is disabled")
    aggregate = PERSISTENCE_STORE.get_aggregate(str(candidate_ref))
    if aggregate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "candidate not found")
    try:
        command = CandidateCommand.model_validate(request.command)
        outcome = apply_candidate_command(aggregate, command, actor_ref=request.actor_ref)
        PERSISTENCE_STORE.update_aggregate(outcome.aggregate)
        return outcome.aggregate
    except SyntheticPersistenceError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "staging persistence unavailable"
        ) from exc
    except CandidateContractError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


def _build_persistence_store() -> SyntheticOutputStore | None:
    raw_path = os.environ.get("LEDGERBRIDGE_SYNTHETIC_PERSISTENCE_PATH")
    if not raw_path:
        return None
    try:
        return SyntheticOutputStore(Path(raw_path))
    except (OSError, ValueError) as exc:
        raise RuntimeError("invalid synthetic persistence path") from exc


PERSISTENCE_STORE = _build_persistence_store()


def _initial_aggregate(output: IntakeOutput) -> CandidateAggregate:
    if output.candidate_ref is None:
        raise ValueError("only candidate outputs can be persisted")
    evidence = tuple(
        EvidenceReference(
            evidence_ref=item.evidence_ref,
            kind=(
                EvidenceKind.ATTACHMENT
                if item.filename is not None
                else EvidenceKind.MESSAGE_ENVELOPE
            ),
            media_type=item.media_type,
            display_name=item.filename,
            download_available=False,
        )
        for item in output.evidence
    )
    missing: tuple[
        tuple[
            BlockerCode,
            str,
            Literal["business_unit", "category", "amount_minor", "accounting_month"],
        ],
        ...,
    ] = (
        (BlockerCode.MISSING_BUSINESS_UNIT, "business unit is required", "business_unit"),
        (BlockerCode.MISSING_CATEGORY, "category is required", "category"),
        (BlockerCode.MISSING_AMOUNT, "amount is required", "amount_minor"),
        (BlockerCode.MISSING_ACCOUNTING_MONTH, "accounting month is required", "accounting_month"),
    )
    projection = CandidateProjection(
        candidate_ref=output.candidate_ref,
        short_id=f"C-{output.candidate_ref.hex[:6].upper()}",
        revision=1,
        status=CandidateStatus.INCOMPLETE,
        entity_ref=output.entity_ref,
        summary=(output.source_subject or output.source_message_id)[:500],
        confidence_basis_points=0,
        source=SourceProjection(
            ingest_channel=IngestChannel.SYNTHETIC,
            source_system="synthetic",
            source_event_ref=output.source_event_ref,
            display_label=(output.source_subject or output.source_message_id)[:100],
        ),
        evidence=evidence,
        blockers=tuple(
            Blocker(code=code, message=message, field=field) for code, message, field in missing
        ),
        review_summary=ReviewSummary(event_count=0, current_revision=1),
        created_at=output.source_received_at,
        updated_at=output.source_received_at,
    )
    return CandidateAggregate(projection=projection)


def run_self_check() -> dict[str, Any]:
    STORE.clear()
    payload = {
        "message_id": "gateway-demo-message",
        "source_event_ref": "40000000-0000-4000-8000-000000000099",
        "entity_ref": "10000000-0000-4000-8000-000000000001",
        "text": "请处理发票",
        "evidence": [
            {
                "evidence_ref": "20000000-0000-4000-8000-000000000099",
                "media_type": "text/plain",
                "content_base64": base64.b64encode(b"synthetic invoice").decode(),
                "business_unit_ref": "unit-demo-a",
            }
        ],
    }
    with TestClient(app) as client:
        created = client.post("/v1/intake", json=payload)
        listed = client.get("/v1/candidates")
    if created.status_code != status.HTTP_201_CREATED or listed.status_code != status.HTTP_200_OK:
        raise RuntimeError(f"synthetic gateway failed: {created.status_code}/{listed.status_code}")
    result = created.json()
    if result["triage_action"] != HermesTriageAction.CANDIDATE.value or result["writes_posting"]:
        raise RuntimeError("synthetic gateway did not preserve the posting boundary")
    return {
        "mode": "synthetic",
        "created_status": created.status_code,
        "candidate_count": len(listed.json()),
        "triage_action": result["triage_action"],
        "evidence_count": len(result["evidence"]),
        "writes_posting": result["writes_posting"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="run the local gateway self-check")
    args = parser.parse_args()
    if args.check:
        print(json.dumps(run_self_check(), sort_keys=True))
        raise SystemExit(0)
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8653, log_level="info")
