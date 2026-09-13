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

Two more routes serve the personal rules carried over from the old desk
(`ledgerbridge.local_rules`): one reads the pending candidates grouped by the
rule each matches, and one confirms a sampled group. The second is a loop over
the same per-candidate decision, not a new write path: every member is
re-loaded, re-matched against the file as it is now, and decided with its own
operation id, revision check and audit event.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path as FilePath
from typing import Annotated, Final
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, Header, Path, Request, status

from ledgerbridge.candidate_contract import (
    CandidateContractError,
    CandidateProjection,
    CandidateRevisionConflict,
    CandidateStatus,
)
from ledgerbridge.config import Settings, get_settings
from ledgerbridge.internal_candidate_command import (
    CandidateClassificationBatchReceipt,
    CandidateClassificationBatchRequest,
    CandidateCommandIdempotencyConflict,
    CandidateCommandRejected,
    CandidateCommandUnavailable,
    CandidateCorrections,
    CandidateDecision,
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
    require_candidate_groups_read,
    require_internal_candidate_command_api,
)
from ledgerbridge.internal_read_contract import (
    AuthorizationDenied,
    ResourceNotVisible,
    WorkloadPrincipal,
)
from ledgerbridge.internal_read_routes import (
    InternalReadProblem,
    _closed_query,
    _parse_business_unit,
    _parse_uuid,
)
from ledgerbridge.internal_read_service import InternalReadBackendUnavailable
from ledgerbridge.local_rules import (
    LocalRulesInvalid,
    LocalRulesNotConfigured,
    Rule,
    RuleBatchOutcome,
    RuleBatchReceipt,
    RuleBatchRequest,
    RuleGroupMember,
    RuleSet,
    RuleSuggestions,
    build_suggestions,
    category_of_group,
    decision_reason,
    group_key_of,
    load_rules,
    match,
    rules_path,
)

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


def get_local_rules_path() -> FilePath:
    """Where the rules file is read from; a dependency so tests point it elsewhere."""
    return rules_path()


_RulesPath = Annotated[FilePath, Depends(get_local_rules_path)]
#: Pending candidates one suggestion read may walk; the classification groups
#: stop at the same size.
_MAX_PENDING: Final = 10_000


def _closed(request: Request, keys: frozenset[str]) -> Mapping[str, str]:
    """Parse a closed query by the read routes' rules, answered as a command problem."""
    try:
        return _closed_query(request, allowed=keys, required=keys)
    except InternalReadProblem as exc:
        raise InternalCandidateCommandProblem(exc.status_code, exc.code) from exc


def _entity_and_unit(request: Request) -> tuple[UUID, str]:
    query = _closed(request, frozenset({"entity_ref", "business_unit"}))
    try:
        return _parse_uuid(query["entity_ref"]), _parse_business_unit(query["business_unit"])
    except InternalReadProblem as exc:
        raise InternalCandidateCommandProblem(exc.status_code, exc.code) from exc


def _entity(request: Request) -> UUID:
    query = _closed(request, frozenset({"entity_ref"}))
    try:
        return _parse_uuid(query["entity_ref"])
    except InternalReadProblem as exc:
        raise InternalCandidateCommandProblem(exc.status_code, exc.code) from exc


def _idempotency_operation(idempotency_key: str) -> UUID:
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


def _rules(path: FilePath) -> RuleSet:
    try:
        return load_rules(path)
    except LocalRulesNotConfigured as exc:
        raise InternalCandidateCommandProblem(
            status.HTTP_404_NOT_FOUND, "LOCAL_RULES_NOT_CONFIGURED"
        ) from exc
    except LocalRulesInvalid as exc:
        raise InternalCandidateCommandProblem(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "LOCAL_RULES_INVALID"
        ) from exc


def _ready_category_codes(
    service: SyntheticInternalReviewService | DatabaseInternalReviewService,
    principal: WorkloadPrincipal,
    entity_ref: UUID,
) -> frozenset[str]:
    dimensions = service.get_accounting_dimensions(principal, entity_ref=entity_ref)
    return frozenset(item.code for item in dimensions.categories)


def _pending_candidates(
    service: SyntheticInternalReviewService | DatabaseInternalReviewService,
    principal: WorkloadPrincipal,
    *,
    entity_ref: UUID,
    business_unit: str,
) -> list[CandidateProjection]:
    found: list[CandidateProjection] = []
    cursor: str | None = None
    visited: set[str] = set()
    while True:
        page = service.list_candidates(
            principal,
            status=CandidateStatus.PENDING,
            business_unit=business_unit,
            cursor=cursor,
        )
        found.extend(item for item in page.items if item.entity_ref == entity_ref)
        if len(found) > _MAX_PENDING:
            raise CandidateCommandUnavailable("too many pending candidates to suggest rules for")
        if page.next_cursor is None:
            break
        if page.next_cursor in visited:
            raise CandidateCommandUnavailable("pending candidate cursor repeated")
        visited.add(page.next_cursor)
        cursor = page.next_cursor
    return found


@router.get("/local/rule-suggestions", response_model=RuleSuggestions)
def list_rule_suggestions(
    request: Request,
    principal: Annotated[WorkloadPrincipal, Depends(require_candidate_groups_read)],
    service: _Service,
    path: _RulesPath,
) -> RuleSuggestions:
    """Pending candidates grouped by the rule each matches. Reads only."""
    entity_ref, business_unit = _entity_and_unit(request)
    ruleset = _rules(path)
    ready = _ready_category_codes(service, principal, entity_ref)
    candidates = _pending_candidates(
        service, principal, entity_ref=entity_ref, business_unit=business_unit
    )
    return build_suggestions(
        ruleset, candidates, business_unit=business_unit, ready_category_codes=ready
    )


#: Refusals from the decision service that belong to one member, not the batch.
_MEMBER_PROBLEMS: Final[tuple[tuple[type[Exception], str], ...]] = (
    (ResourceNotVisible, "RESOURCE_NOT_FOUND"),
    (AuthorizationDenied, "CAPABILITY_REQUIRED"),
    (CandidateCommandIdempotencyConflict, "IDEMPOTENCY_CONFLICT"),
    (CandidateCommandRejected, "COMMAND_REJECTED"),
    (CandidateCommandUnavailable, "CANDIDATE_COMMAND_UNAVAILABLE"),
    (InternalReadBackendUnavailable, "CANDIDATE_COMMAND_UNAVAILABLE"),
    (CandidateContractError, "INVALID_TRANSITION"),
)
_MEMBER_EXCEPTIONS: Final = tuple(kind for kind, _ in _MEMBER_PROBLEMS)
#: What a replay probe may be refused with without anything having been written.
_NOT_REPLAYED: Final = (
    CandidateRevisionConflict,
    CandidateCommandIdempotencyConflict,
    CandidateCommandRejected,
    CandidateContractError,
    ResourceNotVisible,
)


def _problem_code(exc: Exception) -> str:
    return next(code for kind, code in _MEMBER_PROBLEMS if isinstance(exc, kind))


def _decision_requests(
    member: RuleGroupMember, rule: Rule, reason: str
) -> tuple[CandidateDecisionRequest, CandidateDecisionRequest]:
    """The two shapes a rule decision takes: correct the category, or confirm as is."""
    stated = decision_reason(reason, rule)
    return (
        CandidateDecisionRequest(
            decision=CandidateDecision.CORRECT_AND_CONFIRM,
            expected_revision=member.expected_revision,
            reason=stated,
            corrections=CandidateCorrections(category=rule.category_code),
        ),
        CandidateDecisionRequest(
            decision=CandidateDecision.CONFIRM,
            expected_revision=member.expected_revision,
            reason=stated,
        ),
    )


def _decide_member(
    service: SyntheticInternalReviewService | DatabaseInternalReviewService,
    principal: WorkloadPrincipal,
    *,
    entity_ref: UUID,
    ruleset: RuleSet,
    rule: Rule,
    member: RuleGroupMember,
    batch_operation: UUID,
    reason: str,
    decided_at: datetime,
) -> RuleBatchOutcome:
    ref = member.candidate_ref
    operation_id = uuid5(batch_operation, str(ref))

    def decide(request: CandidateDecisionRequest) -> CandidateDecisionReceipt:
        return service.append_decision(
            principal,
            candidate_ref=ref,
            operation_id=operation_id,
            assertion_jti=local_assertion_id(operation_id),
            actor_ref=LOCAL_ACTOR,
            request=request,
            decided_at=decided_at,
        )

    try:
        candidate = service.get_candidate(principal, ref)
        if candidate.entity_ref != entity_ref:
            raise ResourceNotVisible("candidate belongs to another entity")
        correct, confirm = _decision_requests(member, rule, reason)
        if candidate.revision != member.expected_revision:
            # The candidate has moved on. If this batch moved it, the decision
            # service still holds the receipt under this operation id and asking
            # again replays it. Which shape was sent cannot be read back off the
            # candidate, so both are asked; the revision no longer matches, so
            # neither can write anything.
            for probe in (correct, confirm):
                try:
                    replay = decide(probe)
                except _NOT_REPLAYED:
                    continue
                return RuleBatchOutcome(
                    candidate_ref=ref, outcome="CONFIRMED", replayed=replay.replayed
                )
        matched = match(ruleset, candidate.summary)
        if matched is None or group_key_of(matched) != group_key_of(rule):
            return RuleBatchOutcome(candidate_ref=ref, outcome="NOT_MATCHED")
        if candidate.status != CandidateStatus.PENDING:
            return RuleBatchOutcome(candidate_ref=ref, outcome="NOT_PENDING")
        if candidate.revision != member.expected_revision:
            return RuleBatchOutcome(candidate_ref=ref, outcome="STALE")
        receipt = decide(confirm if candidate.category_code == rule.category_code else correct)
    except CandidateRevisionConflict:
        return RuleBatchOutcome(candidate_ref=ref, outcome="STALE")
    except _MEMBER_EXCEPTIONS as exc:
        return RuleBatchOutcome(
            candidate_ref=ref, outcome="REJECTED", problem_code=_problem_code(exc)
        )
    return RuleBatchOutcome(candidate_ref=ref, outcome="CONFIRMED", replayed=receipt.replayed)


@router.post("/local/rule-batches/decisions", response_model=RuleBatchReceipt)
def decide_rule_batch(
    command: RuleBatchRequest,
    request: Request,
    principal: Annotated[WorkloadPrincipal, Depends(require_candidate_decide)],
    service: _Service,
    idempotency_key: _IdempotencyKey,
    path: _RulesPath,
) -> RuleBatchReceipt:
    """Confirm a sampled rule group, one audited candidate decision per member."""
    entity_ref = _entity(request)
    batch_operation = _idempotency_operation(idempotency_key)
    ruleset = _rules(path)
    if ruleset.rules_version != command.rules_version:
        raise InternalCandidateCommandProblem(status.HTTP_409_CONFLICT, "LOCAL_RULES_CHANGED")
    ready = _ready_category_codes(service, principal, entity_ref)
    if category_of_group(command.group_key) not in ready:
        raise InternalCandidateCommandProblem(status.HTTP_409_CONFLICT, "LOCAL_CATEGORY_NOT_READY")
    rule = ruleset.rule_for_group(command.group_key)
    decided_at = datetime.now(UTC)
    outcomes = tuple(
        RuleBatchOutcome(candidate_ref=member.candidate_ref, outcome="NOT_MATCHED")
        if rule is None
        else _decide_member(
            service,
            principal,
            entity_ref=entity_ref,
            ruleset=ruleset,
            rule=rule,
            member=member,
            batch_operation=batch_operation,
            reason=command.reason,
            decided_at=decided_at,
        )
        for member in command.members
    )
    return RuleBatchReceipt(group_key=command.group_key, outcomes=outcomes)


__all__ = [
    "LOCAL_ACTOR",
    "get_local_rules_path",
    "local_assertion_id",
    "require_local_profile",
    "router",
]
