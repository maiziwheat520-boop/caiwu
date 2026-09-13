"""The two candidate decisions, as local single-user mode serves them.

Deployed, a decision crosses two processes that do not trust each other: the Web
BFF holds the person's Passkey session, Core holds the facts, and a signed user
assertion binds the one to one exact command on the other. Locally both run as
the same user on the same machine, bound to loopback, and the person is fixed by
the profile, so the assertion proves nothing that is not already true. The user
chose to drop it ("去掉签名", 2026-09-13).

What is dropped is only the envelope. Each decision still goes through the same
service and the same `internal_command` database functions as production: the
capability check, the entity and business-unit scope, the revision check, the
idempotency receipt and the append-only audit chain are all unchanged. The
production routes in `internal_candidate_command_routes` are not touched and
still require the assertion.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Final
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, Header, Path, Request, status

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.internal_candidate_command import (
    CandidateClassificationBatchReceipt,
    CandidateClassificationBatchRequest,
    CandidateDecisionReceipt,
    CandidateDecisionRequest,
    DatabaseInternalReviewService,
    SyntheticInternalReviewService,
)
from ledgerbridge.internal_candidate_command_routes import (
    InternalCandidateCommandProblem,
    InternalCandidateCommandRoute,
    get_candidate_command_service,
    require_candidate_decide,
    require_internal_candidate_command_api,
)
from ledgerbridge.internal_read_contract import WorkloadPrincipal

#: Who the audit chain records as having decided: the Web BFF's local session
#: principal (`LOCAL_SESSION_PRINCIPAL`), the one user this machine has.
LOCAL_ACTOR: Final = "local-single-user"
#: The database keeps one row per assertion id and refuses one reused for a
#: different operation. With no assertion, the id is derived from the operation,
#: so a retried decision presents the same id and replays instead of conflicting.
_LOCAL_ASSERTION_NAMESPACE: Final = UUID("6c6f6361-6c2d-4465-8369-73696f6e0001")


def require_local_profile(settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """Unsigned decisions exist only where local mode's own conditions hold.

    Mounting this router in the deployed app by mistake would let anything that
    holds the BFF's workload identity decide candidates with no user behind it.
    So it checks, per request, the three things a local profile always is and a
    deployment never is: not production, no mTLS transport, both gates closed.
    """
    if (
        settings.env == "production"
        or settings.internal_read_transport != "disabled"
        or settings.internal_read_operational_gate != "closed"
        or settings.internal_candidate_command_operational_gate != "closed"
    ):
        raise InternalCandidateCommandProblem(
            status.HTTP_404_NOT_FOUND, "CANDIDATE_COMMAND_DISABLED"
        )


router = APIRouter(
    prefix="/internal/v1",
    tags=["local-candidate-command"],
    dependencies=[
        Depends(require_internal_candidate_command_api),
        Depends(require_local_profile),
    ],
    route_class=InternalCandidateCommandRoute,
)

_Service = Annotated[
    SyntheticInternalReviewService | DatabaseInternalReviewService,
    Depends(get_candidate_command_service),
]
_IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=36, max_length=36)]


def _operation(request: Request, idempotency_key: str) -> UUID:
    if request.url.query:
        raise InternalCandidateCommandProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    try:
        operation_id = UUID(idempotency_key)
    except ValueError as exc:
        raise InternalCandidateCommandProblem(
            status.HTTP_400_BAD_REQUEST, "INVALID_IDEMPOTENCY_KEY"
        ) from exc
    if str(operation_id) != idempotency_key.lower():
        raise InternalCandidateCommandProblem(
            status.HTTP_400_BAD_REQUEST, "INVALID_IDEMPOTENCY_KEY"
        )
    return operation_id


def local_assertion_id(operation_id: UUID) -> UUID:
    return uuid5(_LOCAL_ASSERTION_NAMESPACE, str(operation_id))


@router.post("/candidates/{candidate_ref}/decisions", response_model=CandidateDecisionReceipt)
def append_candidate_decision(
    candidate_ref: UUID,
    command: CandidateDecisionRequest,
    request: Request,
    principal: Annotated[WorkloadPrincipal, Depends(require_candidate_decide)],
    service: _Service,
    idempotency_key: _IdempotencyKey,
) -> CandidateDecisionReceipt:
    operation_id = _operation(request, idempotency_key)
    return service.append_decision(
        principal,
        candidate_ref=candidate_ref,
        operation_id=operation_id,
        assertion_jti=local_assertion_id(operation_id),
        actor_ref=LOCAL_ACTOR,
        request=command,
        decided_at=datetime.now(UTC),
    )


@router.post(
    "/candidate-classification-groups/{group_ref}/decisions",
    response_model=CandidateClassificationBatchReceipt,
)
def apply_candidate_classification_group(
    command: CandidateClassificationBatchRequest,
    request: Request,
    principal: Annotated[WorkloadPrincipal, Depends(require_candidate_decide)],
    service: _Service,
    idempotency_key: _IdempotencyKey,
    group_ref: Annotated[str, Path(pattern=r"^cg_[0-9a-f]{32}$")],
) -> CandidateClassificationBatchReceipt:
    operation_id = _operation(request, idempotency_key)
    return service.apply_classification_batch(
        principal,
        group_ref=group_ref,
        operation_id=operation_id,
        assertion_jti=local_assertion_id(operation_id),
        actor_ref=LOCAL_ACTOR,
        request=command,
        decided_at=datetime.now(UTC),
    )


__all__ = ["LOCAL_ACTOR", "local_assertion_id", "require_local_profile", "router"]
