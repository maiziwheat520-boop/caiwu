"""Disabled-by-default read-only PayrollVerification publication route."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Coroutine, Mapping
from typing import Annotated, Any, Literal, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.internal_payroll_assertion import (
    HmacPayrollProviderAssertionSigner,
    PayrollAction,
    PayrollUserAssertionClaims,
    PayrollUserAssertionError,
    verify_payroll_user_assertion,
)
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import (
    AuthenticationDenied,
    AuthorizationDenied,
    Capability,
    WorkloadPrincipal,
    require_capability,
)
from ledgerbridge.payroll_integration import (
    LIVE_PROJECTION_PATH,
    TEST_WORKSPACES_PATH,
    HttpPayrollLiveSource,
    HttpPayrollPublicationSource,
    HttpPayrollTestWorkspaceSource,
    PayrollHttpTransport,
    PayrollIntegrationError,
    PayrollLiveHttpTransport,
    PayrollLiveRead,
    PayrollPublicationSource,
    PayrollTestWorkspaceResult,
    UrllibPayrollHttpTransport,
    UrllibPayrollLiveHttpTransport,
)


class PayrollPublicationReadResponse(BaseModel):
    """Source-faithful accounting projection with no payment operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["ledgerbridge.payroll-publication-read.v1"] = (
        "ledgerbridge.payroll-publication-read.v1"
    )
    entity_ref: UUID
    company_id: str
    publication_id: str
    publication: dict[str, object]


class PayrollLiveReadResponse(BaseModel):
    """Company-scoped safe view split from one validated provider projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["ledgerbridge.payroll-read.v1"] = "ledgerbridge.payroll-read.v1"
    entity_ref: UUID
    company_id: str
    data: dict[str, object]


class PayrollVerificationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["ledgerbridge.payroll-receipt-verification-command.v1"]
    expected_revision: int = Field(strict=True, ge=1)
    explicitly_confirmed: Literal[True]
    source_artifact_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    reason_code: Literal["MANUAL_DISBURSEMENT_VERIFICATION"]

    @model_validator(mode="after")
    def evidence_is_unique_and_live(self) -> PayrollVerificationCommand:
        if len(set(self.source_artifact_ids)) != len(self.source_artifact_ids):
            raise ValueError("source_artifact_ids must be unique")
        if any(
            not value
            or value != value.strip()
            or len(value) > 128
            or value.startswith(("artifact_demo_", "receipt_demo_"))
            for value in self.source_artifact_ids
        ):
            raise ValueError("source_artifact_ids must be live stable identifiers")
        return self


class PayrollCommandReceiptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["ledgerbridge.payroll-command-result.v1"] = (
        "ledgerbridge.payroll-command-result.v1"
    )
    entity_ref: UUID
    company_id: str
    action: Literal["payroll.batch.verify-receipts"]
    resource_ref: str
    replayed: bool
    data: dict[str, object]


class PayrollTestWorkspaceReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal["ledgerbridge.payroll-test-workspace-read.v1"] = (
        "ledgerbridge.payroll-test-workspace-read.v1"
    )
    entity_ref: UUID
    company_id: str
    data: dict[str, object]


class PayrollTestMaterialPreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal["ledgerbridge.payroll-test-material-preview-read.v1"] = (
        "ledgerbridge.payroll-test-material-preview-read.v1"
    )
    entity_ref: UUID
    company_id: str
    material_id: str
    data: dict[str, object]


class PayrollTestWorkspaceCommandResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal["ledgerbridge.payroll-test-workspace-command-result.v1"] = (
        "ledgerbridge.payroll-test-workspace-command-result.v1"
    )
    entity_ref: UUID
    company_id: str
    action: Literal[
        "payroll.test_workspace.create",
        "payroll.test_workspace.organize",
        "payroll.test_workspace.validate",
        "payroll.test_workspace.clear",
    ]
    resource_ref: str
    replayed: bool
    data: dict[str, object]


class PayrollLegacyFeatureReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal["ledgerbridge.payroll-legacy-feature-read.v1"] = (
        "ledgerbridge.payroll-legacy-feature-read.v1"
    )
    entity_ref: UUID
    company_id: str
    data: dict[str, object]


class PayrollLegacyFeatureCommandResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal["ledgerbridge.payroll-legacy-feature-command-result.v1"] = (
        "ledgerbridge.payroll-legacy-feature-command-result.v1"
    )
    entity_ref: UUID
    company_id: str
    action: Literal["payroll.test_workspace.legacy.command"]
    resource_ref: str
    replayed: bool
    data: dict[str, object]


class PayrollTestWorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["payroll-test-workspace-create-request/v1"]
    test_batch_id: str = Field(min_length=3, max_length=128)
    expected_store_revision: int = Field(strict=True, ge=0)
    cutoff_date: Literal["2026-08-31"]
    idempotency_key: UUID


class PayrollTestWorkspaceClear(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["payroll-test-workspace-clear-request/v1"]
    expected_workspace_revision: int = Field(strict=True, ge=0)
    idempotency_key: UUID
    explicitly_confirmed: Literal[True]


class PayrollTestMaterialOrganize(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["payroll-test-material-organize-request/v1"]
    expected_workspace_revision: int = Field(strict=True, ge=1)
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    material_type: Literal[
        "PAYROLL_SHEET",
        "RELEASE_LIST",
        "CASH_LIST",
        "ATTENDANCE_SHEET",
        "ADJUSTMENT_SOURCE",
        "PAYROLL_SUMMARY",
        "SUPPORTING_SCAN",
        "BACKUP",
        "OBSOLETE",
    ]
    idempotency_key: UUID
    explicitly_confirmed: Literal[True]


class PayrollTestBatchValidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["payroll-test-batch-validation-request/v1"]
    expected_workspace_revision: int = Field(strict=True, ge=1)
    idempotency_key: UUID
    explicitly_confirmed: Literal[True]


class PayrollLegacyFeatureCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["payroll-legacy-feature-command-request/v1"]
    action: Literal[
        "FILL_MAIN",
        "GENERATE_NORMAL_DRAFT",
        "GENERATE_SUPPLEMENTAL_DRAFT",
        "UPDATE_SUMMARY",
        "SAVE_RULES",
        "CHECK_RULES_AND_HISTORY",
        "VERIFY_CURRENT_PAID",
        "CHECK_PREVIOUS_PENDING",
    ]
    expected_revision: int = Field(strict=True, ge=0)
    idempotency_key: UUID
    explicitly_confirmed: Literal[True]
    payload: dict[str, object]


class PayrollLiveSource(Protocol):
    def read_status(self, **kwargs: object) -> PayrollLiveRead: ...
    def read_dashboard(self, **kwargs: object) -> PayrollLiveRead: ...
    def list_materials(self, **kwargs: object) -> PayrollLiveRead: ...
    def list_batches(self, **kwargs: object) -> PayrollLiveRead: ...
    def list_verification_results(self, **kwargs: object) -> PayrollLiveRead: ...
    def verify_receipts(self, **kwargs: object) -> PayrollLiveRead: ...


class PayrollTestWorkspaceSource(Protocol):
    def read_workspace(
        self, *, entity_ref: UUID, test_batch_id: str, provider_headers: Mapping[str, str]
    ) -> PayrollTestWorkspaceResult: ...
    def create_workspace(
        self,
        *,
        entity_ref: UUID,
        test_batch_id: str,
        provider_headers: Mapping[str, str],
        body: bytes,
    ) -> PayrollTestWorkspaceResult: ...
    def organize_material(
        self,
        *,
        entity_ref: UUID,
        test_batch_id: str,
        material_id: str,
        expected_workspace_revision: int,
        expected_period: str,
        expected_material_type: str,
        provider_headers: Mapping[str, str],
        body: bytes,
    ) -> PayrollTestWorkspaceResult: ...
    def preview_material(
        self,
        *,
        entity_ref: UUID,
        test_batch_id: str,
        material_id: str,
        provider_headers: Mapping[str, str],
    ) -> PayrollTestWorkspaceResult: ...
    def validate_batches(
        self,
        *,
        entity_ref: UUID,
        test_batch_id: str,
        expected_workspace_revision: int,
        provider_headers: Mapping[str, str],
        body: bytes,
    ) -> PayrollTestWorkspaceResult: ...
    def clear_workspace(
        self,
        *,
        entity_ref: UUID,
        test_batch_id: str,
        provider_headers: Mapping[str, str],
        body: bytes,
    ) -> PayrollTestWorkspaceResult: ...
    def read_legacy_features(
        self, *, entity_ref: UUID, test_batch_id: str, provider_headers: Mapping[str, str]
    ) -> PayrollTestWorkspaceResult: ...
    def execute_legacy_feature(
        self,
        *,
        entity_ref: UUID,
        test_batch_id: str,
        expected_workspace_revision: int,
        expected_action: str,
        provider_headers: Mapping[str, str],
        body: bytes,
    ) -> PayrollTestWorkspaceResult: ...


class PayrollAssertionReplayStore(Protocol):
    def consume(self, claims: PayrollUserAssertionClaims) -> bool: ...


class InMemoryPayrollAssertionReplayStore:
    def __init__(self) -> None:
        self._expires: dict[UUID, int] = {}
        self._lock = threading.Lock()

    def consume(self, claims: PayrollUserAssertionClaims) -> bool:
        now = int(time.time())
        with self._lock:
            self._expires = {key: expiry for key, expiry in self._expires.items() if expiry > now}
            if claims.jti in self._expires:
                return False
            self._expires[claims.jti] = claims.expires_at
            return True


_payroll_assertion_replay_store = InMemoryPayrollAssertionReplayStore()


class InternalPayrollProblem(RuntimeError):
    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


def _problem_response(status_code: int, code: str) -> JSONResponse:
    titles = {
        status.HTTP_400_BAD_REQUEST: "Bad Request",
        status.HTTP_401_UNAUTHORIZED: "Unauthorized",
        status.HTTP_403_FORBIDDEN: "Forbidden",
        status.HTTP_404_NOT_FOUND: "Not Found",
        status.HTTP_409_CONFLICT: "Conflict",
        status.HTTP_422_UNPROCESSABLE_CONTENT: "Unprocessable Content",
        status.HTTP_503_SERVICE_UNAVAILABLE: "Service Unavailable",
    }
    return JSONResponse(
        status_code=status_code,
        media_type="application/problem+json",
        headers={"Cache-Control": "no-store"},
        content={
            "type": f"urn:ledgerbridge:problem:{code.lower().replace('_', '-')}",
            "title": titles[status_code],
            "status": status_code,
            "code": code,
        },
    )


class InternalPayrollRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                response = await original(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            except InternalPayrollProblem as exc:
                return _problem_response(exc.status_code, exc.code)
            except AuthenticationDenied:
                return _problem_response(status.HTTP_401_UNAUTHORIZED, "AUTH_REQUIRED")
            except AuthorizationDenied:
                return _problem_response(status.HTTP_403_FORBIDDEN, "CAPABILITY_REQUIRED")
            except PayrollUserAssertionError:
                return _problem_response(
                    status.HTTP_401_UNAUTHORIZED,
                    "PAYROLL_USER_ASSERTION_INVALID",
                )
            except PayrollIntegrationError as exc:
                return _payroll_error_response(exc)

        return route_handler


def _payroll_error_response(error: PayrollIntegrationError) -> JSONResponse:
    if error.error_code == "PAYROLL_PUBLICATION_ID_INVALID":
        status_code = status.HTTP_400_BAD_REQUEST
    elif error.error_code in {
        "PAYROLL_PUBLICATION_NOT_FOUND",
        "PAYROLL_TEST_WORKSPACE_NOT_FOUND",
    }:
        status_code = status.HTTP_404_NOT_FOUND
    elif error.error_code in {
        "PAYROLL_IDEMPOTENCY_CONFLICT",
        "PAYROLL_VERSION_CONFLICT",
    }:
        status_code = status.HTTP_409_CONFLICT
    elif error.error_code in {
        "PAYROLL_PROVIDER_TIMEOUT",
        "PAYROLL_PROVIDER_UNAVAILABLE",
        "PAYROLL_PROVIDER_REJECTED",
        "PAYROLL_PROVIDER_RESPONSE",
        "PAYROLL_COMPANY_MAPPING_INVALID",
        "PAYROLL_COMPANY_MAPPING_MISSING",
        "PAYROLL_COMPANY_MAPPING_CONFLICT",
    }:
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    return _problem_response(status_code, error.error_code)


def require_internal_payroll_api(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if not settings.enable_payroll_integration:
        raise InternalPayrollProblem(
            status.HTTP_404_NOT_FOUND,
            "PAYROLL_INTEGRATION_DISABLED",
        )


def require_payroll_receipt_verification(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if (
        not settings.enable_payroll_commands
        or "VERIFY_RECEIPTS" not in settings.payroll_command_allowlist
    ):
        raise InternalPayrollProblem(
            status.HTTP_404_NOT_FOUND,
            "PAYROLL_COMMAND_DISABLED",
        )


def require_payroll_test_workspaces(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if not settings.enable_payroll_test_workspaces:
        raise InternalPayrollProblem(status.HTTP_404_NOT_FOUND, "PAYROLL_TEST_WORKSPACE_DISABLED")


def require_payroll_publication_read(
    principal: Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)],
) -> WorkloadPrincipal:
    require_capability(principal, Capability.PAYROLL_PUBLICATION_READ)
    return principal


def require_payroll_live_read(
    principal: Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)],
) -> WorkloadPrincipal:
    require_capability(principal, Capability.PAYROLL_LIVE_READ)
    return principal


def require_payroll_command(
    principal: Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)],
) -> WorkloadPrincipal:
    require_capability(principal, Capability.PAYROLL_COMMAND)
    return principal


def get_payroll_http_transport() -> PayrollHttpTransport:
    """Deployment composition may replace this with an authenticated transport."""

    return UrllibPayrollHttpTransport()


def get_payroll_live_http_transport() -> PayrollLiveHttpTransport:
    return UrllibPayrollLiveHttpTransport()


def get_payroll_live_source(
    settings: Annotated[Settings, Depends(get_settings)],
    transport: Annotated[
        PayrollLiveHttpTransport,
        Depends(get_payroll_live_http_transport),
    ],
) -> PayrollLiveSource:
    if settings.payroll_base_url is None:
        raise PayrollIntegrationError(
            "PAYROLL_PROVIDER_UNAVAILABLE",
            "payroll provider configuration is unavailable",
        )
    return cast(
        PayrollLiveSource,
        HttpPayrollLiveSource(
            base_url=settings.payroll_base_url,
            timeout_seconds=settings.payroll_timeout_seconds,
            company_mapping=settings.payroll_company_mapping,
            enabled=settings.enable_payroll_integration,
            transport=transport,
        ),
    )


def get_payroll_test_workspace_source(
    settings: Annotated[Settings, Depends(get_settings)],
    transport: Annotated[PayrollLiveHttpTransport, Depends(get_payroll_live_http_transport)],
) -> PayrollTestWorkspaceSource:
    if settings.payroll_base_url is None:
        raise PayrollIntegrationError(
            "PAYROLL_PROVIDER_UNAVAILABLE", "payroll provider configuration is unavailable"
        )
    return HttpPayrollTestWorkspaceSource(
        base_url=settings.payroll_base_url,
        timeout_seconds=settings.payroll_timeout_seconds,
        company_mapping=settings.payroll_company_mapping,
        enabled=settings.enable_payroll_test_workspaces,
        transport=transport,
    )


def get_payroll_provider_signer(
    settings: Annotated[Settings, Depends(get_settings)],
) -> HmacPayrollProviderAssertionSigner:
    workload_key = settings.payroll_provider_workload_assertion_key
    user_key = settings.payroll_provider_user_assertion_key
    issuer = settings.payroll_provider_assertion_issuer
    audience = settings.payroll_provider_assertion_audience
    service_subject = settings.payroll_provider_service_subject
    if (
        workload_key is None
        or user_key is None
        or issuer is None
        or audience is None
        or service_subject is None
    ):
        raise PayrollIntegrationError(
            "PAYROLL_PROVIDER_AUTH_UNAVAILABLE",
            "payroll provider assertion configuration is unavailable",
        )
    return HmacPayrollProviderAssertionSigner(
        workload_key=workload_key.get_secret_value().encode("utf-8"),
        user_key=user_key.get_secret_value().encode("utf-8"),
        issuer=issuer,
        audience=audience,
        service_subject=service_subject,
    )


def get_payroll_assertion_replay_store() -> PayrollAssertionReplayStore:
    return _payroll_assertion_replay_store


def get_payroll_publication_source(
    settings: Annotated[Settings, Depends(get_settings)],
    transport: Annotated[PayrollHttpTransport, Depends(get_payroll_http_transport)],
) -> PayrollPublicationSource:
    base_url = settings.payroll_base_url
    if base_url is None:
        raise PayrollIntegrationError(
            "PAYROLL_PROVIDER_UNAVAILABLE",
            "payroll provider configuration is unavailable",
        )
    return HttpPayrollPublicationSource(
        base_url=base_url,
        timeout_seconds=settings.payroll_timeout_seconds,
        company_mapping=settings.payroll_company_mapping,
        enabled=settings.enable_payroll_integration,
        transport=transport,
    )


def _entity_from_principal(principal: WorkloadPrincipal) -> UUID:
    entities = frozenset(grant.entity_ref for grant in principal.grants)
    if len(entities) != 1:
        raise InternalPayrollProblem(
            status.HTTP_404_NOT_FOUND,
            "PAYROLL_COMPANY_SCOPE_UNAVAILABLE",
        )
    return next(iter(entities))


def _server_idempotency_key(entity_ref: UUID, publication_id: str) -> str:
    digest = hashlib.sha256(f"{entity_ref}:{publication_id}".encode("ascii")).hexdigest()
    return f"payroll-read-{digest}"


def _company_for_entity(settings: Settings, entity_ref: UUID) -> str:
    matches = [
        company_id
        for company_id, mapped_entity in settings.payroll_company_mapping.items()
        if mapped_entity == entity_ref
    ]
    if len(matches) != 1:
        raise InternalPayrollProblem(
            status.HTTP_404_NOT_FOUND,
            "PAYROLL_COMPANY_SCOPE_UNAVAILABLE",
        )
    return matches[0]


def _verify_user_for_grant(
    token: str,
    *,
    settings: Settings,
    principal: WorkloadPrincipal,
    method: Literal["GET", "POST"],
    path: str,
    body: bytes,
    action: PayrollAction,
    resource_ref: str,
    expected_revision: int | None = None,
    operation_id: UUID | None = None,
) -> PayrollUserAssertionClaims:
    key = settings.payroll_bff_user_assertion_key
    issuer = settings.payroll_bff_user_assertion_issuer
    audience = settings.payroll_bff_user_assertion_audience
    if key is None or issuer is None or audience is None:
        raise PayrollUserAssertionError("payroll BFF assertion configuration is unavailable")
    matches: list[PayrollUserAssertionClaims] = []
    for entity_ref in frozenset(grant.entity_ref for grant in principal.grants):
        try:
            matches.append(
                verify_payroll_user_assertion(
                    token,
                    key=key.get_secret_value().encode("utf-8"),
                    issuer=issuer,
                    audience=audience,
                    method=method,
                    canonical_path=path,
                    body=body,
                    entity_ref=entity_ref,
                    action=action,
                    resource_ref=resource_ref,
                    expected_revision=expected_revision,
                    operation_id=operation_id,
                    workload_principal=principal,
                )
            )
        except PayrollUserAssertionError:
            continue
    if len(matches) != 1:
        if len(principal.grants) > 1:
            raise InternalPayrollProblem(
                status.HTTP_409_CONFLICT,
                "PAYROLL_ENTITY_SELECTION_REQUIRED",
            )
        raise PayrollUserAssertionError("payroll assertion does not select an entity grant")
    return matches[0]


def _live_read(
    *,
    request: Request,
    assertion: str,
    action: PayrollAction,
    resource_ref: str,
    principal: WorkloadPrincipal,
    settings: Settings,
    source: PayrollLiveSource,
    signer: HmacPayrollProviderAssertionSigner,
    replay_store: PayrollAssertionReplayStore,
    source_method: str,
) -> PayrollLiveReadResponse:
    if request.url.query:
        raise InternalPayrollProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    claims = _verify_user_for_grant(
        assertion,
        settings=settings,
        principal=principal,
        method="GET",
        path=request.url.path,
        body=b"",
        action=action,
        resource_ref=resource_ref,
    )
    if not replay_store.consume(claims):
        raise PayrollUserAssertionError("payroll assertion replayed")
    company_id = _company_for_entity(settings, claims.entity_ref)
    provider_headers = signer.headers(
        user=claims,
        company_id=company_id,
        provider_action="payroll.projection.read",
        method="GET",
        path=LIVE_PROJECTION_PATH,
        body=b"",
    )
    kwargs: dict[str, object] = {
        "entity_ref": claims.entity_ref,
        "provider_headers": provider_headers,
    }
    if source_method == "read_status":
        allowed_actions: tuple[str, ...] = ()
        roles = settings.payroll_role_bindings.get(company_id, {}).get(claims.subject, frozenset())
        if (
            settings.enable_payroll_commands
            and "VERIFY_RECEIPTS" in settings.payroll_command_allowlist
            and "checker" in roles
        ):
            allowed_actions = ("VERIFY_RECEIPTS",)
        kwargs["allowed_actions"] = allowed_actions
    result = cast(Callable[..., PayrollLiveRead], getattr(source, source_method))(**kwargs)
    if result.entity_ref != claims.entity_ref or result.company_id != company_id:
        raise PayrollIntegrationError(
            "PAYROLL_IDENTITY_SCOPE_MISMATCH",
            "payroll view crosses the selected company scope",
        )
    return PayrollLiveReadResponse(
        entity_ref=result.entity_ref,
        company_id=result.company_id,
        data=result.payload_copy(),
    )


router = APIRouter(
    prefix="/internal/v1",
    tags=["internal-payroll-read"],
    dependencies=[Depends(require_internal_payroll_api)],
    route_class=InternalPayrollRoute,
)


@router.get("/payroll/status", response_model=PayrollLiveReadResponse)
def get_payroll_status(
    request: Request,
    assertion: Annotated[str, Header(alias="X-LedgerBridge-User-Assertion")],
    principal: Annotated[WorkloadPrincipal, Depends(require_payroll_live_read)],
    settings: Annotated[Settings, Depends(get_settings)],
    source: Annotated[PayrollLiveSource, Depends(get_payroll_live_source)],
    signer: Annotated[
        HmacPayrollProviderAssertionSigner,
        Depends(get_payroll_provider_signer),
    ],
    replay_store: Annotated[
        PayrollAssertionReplayStore,
        Depends(get_payroll_assertion_replay_store),
    ],
) -> PayrollLiveReadResponse:
    return _live_read(
        request=request,
        assertion=assertion,
        action="payroll.status.read",
        resource_ref="payroll-status",
        principal=principal,
        settings=settings,
        source=source,
        signer=signer,
        replay_store=replay_store,
        source_method="read_status",
    )


@router.get("/payroll/dashboard", response_model=PayrollLiveReadResponse)
def get_payroll_dashboard(
    request: Request,
    assertion: Annotated[str, Header(alias="X-LedgerBridge-User-Assertion")],
    principal: Annotated[WorkloadPrincipal, Depends(require_payroll_live_read)],
    settings: Annotated[Settings, Depends(get_settings)],
    source: Annotated[PayrollLiveSource, Depends(get_payroll_live_source)],
    signer: Annotated[
        HmacPayrollProviderAssertionSigner,
        Depends(get_payroll_provider_signer),
    ],
    replay_store: Annotated[
        PayrollAssertionReplayStore,
        Depends(get_payroll_assertion_replay_store),
    ],
) -> PayrollLiveReadResponse:
    return _live_read(
        request=request,
        assertion=assertion,
        action="payroll.dashboard.read",
        resource_ref="payroll-dashboard",
        principal=principal,
        settings=settings,
        source=source,
        signer=signer,
        replay_store=replay_store,
        source_method="read_dashboard",
    )


@router.get("/payroll/materials", response_model=PayrollLiveReadResponse)
def get_payroll_materials(
    request: Request,
    assertion: Annotated[str, Header(alias="X-LedgerBridge-User-Assertion")],
    principal: Annotated[WorkloadPrincipal, Depends(require_payroll_live_read)],
    settings: Annotated[Settings, Depends(get_settings)],
    source: Annotated[PayrollLiveSource, Depends(get_payroll_live_source)],
    signer: Annotated[
        HmacPayrollProviderAssertionSigner,
        Depends(get_payroll_provider_signer),
    ],
    replay_store: Annotated[
        PayrollAssertionReplayStore,
        Depends(get_payroll_assertion_replay_store),
    ],
) -> PayrollLiveReadResponse:
    return _live_read(
        request=request,
        assertion=assertion,
        action="payroll.materials.list",
        resource_ref="payroll-materials",
        principal=principal,
        settings=settings,
        source=source,
        signer=signer,
        replay_store=replay_store,
        source_method="list_materials",
    )


@router.get("/payroll/batches", response_model=PayrollLiveReadResponse)
def get_payroll_batches(
    request: Request,
    assertion: Annotated[str, Header(alias="X-LedgerBridge-User-Assertion")],
    principal: Annotated[WorkloadPrincipal, Depends(require_payroll_live_read)],
    settings: Annotated[Settings, Depends(get_settings)],
    source: Annotated[PayrollLiveSource, Depends(get_payroll_live_source)],
    signer: Annotated[
        HmacPayrollProviderAssertionSigner,
        Depends(get_payroll_provider_signer),
    ],
    replay_store: Annotated[
        PayrollAssertionReplayStore,
        Depends(get_payroll_assertion_replay_store),
    ],
) -> PayrollLiveReadResponse:
    return _live_read(
        request=request,
        assertion=assertion,
        action="payroll.batches.list",
        resource_ref="payroll-batches",
        principal=principal,
        settings=settings,
        source=source,
        signer=signer,
        replay_store=replay_store,
        source_method="list_batches",
    )


@router.get("/payroll/verification", response_model=PayrollLiveReadResponse)
def get_payroll_verification(
    request: Request,
    assertion: Annotated[str, Header(alias="X-LedgerBridge-User-Assertion")],
    principal: Annotated[WorkloadPrincipal, Depends(require_payroll_live_read)],
    settings: Annotated[Settings, Depends(get_settings)],
    source: Annotated[PayrollLiveSource, Depends(get_payroll_live_source)],
    signer: Annotated[
        HmacPayrollProviderAssertionSigner,
        Depends(get_payroll_provider_signer),
    ],
    replay_store: Annotated[
        PayrollAssertionReplayStore,
        Depends(get_payroll_assertion_replay_store),
    ],
) -> PayrollLiveReadResponse:
    return _live_read(
        request=request,
        assertion=assertion,
        action="payroll.verification.list",
        resource_ref="payroll-verification",
        principal=principal,
        settings=settings,
        source=source,
        signer=signer,
        replay_store=replay_store,
        source_method="list_verification_results",
    )


@router.post(
    "/payroll/batches/{batch_id}/verify-receipts",
    response_model=PayrollCommandReceiptResponse,
    dependencies=[Depends(require_payroll_receipt_verification)],
)
async def verify_payroll_receipts(
    batch_id: str,
    command_payload: dict[str, object],
    request: Request,
    assertion: Annotated[str, Header(alias="X-LedgerBridge-User-Assertion")],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    principal: Annotated[WorkloadPrincipal, Depends(require_payroll_command)],
    settings: Annotated[Settings, Depends(get_settings)],
    source: Annotated[PayrollLiveSource, Depends(get_payroll_live_source)],
    signer: Annotated[
        HmacPayrollProviderAssertionSigner,
        Depends(get_payroll_provider_signer),
    ],
    replay_store: Annotated[
        PayrollAssertionReplayStore,
        Depends(get_payroll_assertion_replay_store),
    ],
) -> PayrollCommandReceiptResponse:
    if request.url.query:
        raise InternalPayrollProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    if command_payload.get("source_artifact_ids") == []:
        raise InternalPayrollProblem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "VERIFICATION_EVIDENCE_REQUIRED",
        )
    normalized_payload = dict(command_payload)
    source_artifact_ids = normalized_payload.get("source_artifact_ids")
    if isinstance(source_artifact_ids, list):
        normalized_payload["source_artifact_ids"] = tuple(source_artifact_ids)
    try:
        command = PayrollVerificationCommand.model_validate(
            normalized_payload,
            strict=True,
        )
    except ValueError as exc:
        raise InternalPayrollProblem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "PAYROLL_COMMAND_INVALID",
        ) from exc
    try:
        operation_id = UUID(idempotency_key)
    except ValueError as exc:
        raise InternalPayrollProblem(
            status.HTTP_400_BAD_REQUEST,
            "PAYROLL_IDEMPOTENCY_KEY_INVALID",
        ) from exc
    if str(operation_id) != idempotency_key:
        raise InternalPayrollProblem(
            status.HTTP_400_BAD_REQUEST,
            "PAYROLL_IDEMPOTENCY_KEY_INVALID",
        )
    body = await request.body()
    claims = _verify_user_for_grant(
        assertion,
        settings=settings,
        principal=principal,
        method="POST",
        path=request.url.path,
        body=body,
        action="payroll.batch.verify-receipts",
        resource_ref=batch_id,
        expected_revision=command.expected_revision,
        operation_id=operation_id,
    )
    if not replay_store.consume(claims):
        raise PayrollUserAssertionError("payroll assertion replayed")
    company_id = _company_for_entity(settings, claims.entity_ref)
    roles = settings.payroll_role_bindings.get(company_id, {}).get(
        claims.subject,
        frozenset(),
    )
    if "checker" not in roles:
        raise InternalPayrollProblem(status.HTTP_403_FORBIDDEN, "PAYROLL_ROLE_REQUIRED")

    capability_headers = signer.headers(
        user=claims,
        company_id=company_id,
        provider_action="payroll.projection.read",
        method="GET",
        path=LIVE_PROJECTION_PATH,
        body=b"",
    )
    capability_result = source.read_status(
        entity_ref=claims.entity_ref,
        provider_headers=capability_headers,
        allowed_actions=("VERIFY_RECEIPTS",),
    )
    capabilities = cast(
        Mapping[str, object],
        capability_result.payload_copy().get("capabilities"),
    )
    if capabilities.get("commands_enabled") is not True or capabilities.get("allowed_actions") != [
        "VERIFY_RECEIPTS"
    ]:
        raise InternalPayrollProblem(
            status.HTTP_403_FORBIDDEN,
            "PAYROLL_PROVIDER_CAPABILITY_REQUIRED",
        )
    evidence_headers = signer.headers(
        user=claims,
        company_id=company_id,
        provider_action="payroll.projection.read",
        method="GET",
        path=LIVE_PROJECTION_PATH,
        body=b"",
    )
    verification = source.list_verification_results(
        entity_ref=claims.entity_ref,
        provider_headers=evidence_headers,
    )
    available = cast(list[Mapping[str, object]], verification.payload_copy()["available_evidence"])
    available_ids = {cast(str, item["artifact_id"]) for item in available}
    if not set(command.source_artifact_ids).issubset(available_ids):
        raise InternalPayrollProblem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "VERIFICATION_EVIDENCE_NOT_AVAILABLE",
        )
    provider_payload = {
        "expected_version": command.expected_revision,
        "explicit_human_approval": True,
        "idempotency_key": idempotency_key,
        "payment_submission_allowed": False,
        "reason_code": command.reason_code,
        "source_artifact_ids": list(command.source_artifact_ids),
    }
    provider_body = json.dumps(
        provider_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    provider_path = f"/api/v1/batches/{batch_id}/verify-receipts"
    command_headers = signer.headers(
        user=claims,
        company_id=company_id,
        provider_action="payroll.receipts.verify",
        method="POST",
        path=provider_path,
        body=provider_body,
    )
    result = source.verify_receipts(
        entity_ref=claims.entity_ref,
        batch_id=batch_id,
        provider_body=provider_body,
        provider_headers=command_headers,
        idempotency_key=idempotency_key,
    )
    if result.entity_ref != claims.entity_ref or result.company_id != company_id:
        raise PayrollIntegrationError(
            "PAYROLL_IDENTITY_SCOPE_MISMATCH",
            "payroll command receipt crosses company scope",
        )
    return PayrollCommandReceiptResponse(
        entity_ref=result.entity_ref,
        company_id=result.company_id,
        action="payroll.batch.verify-receipts",
        resource_ref=batch_id,
        replayed=cast(bool, result.payload.get("replayed")),
        data=result.payload_copy(),
    )


@router.get(
    "/payroll-publications/{publication_id}",
    response_model=PayrollPublicationReadResponse,
)
def get_payroll_publication(
    publication_id: str,
    request: Request,
    principal: Annotated[WorkloadPrincipal, Depends(require_payroll_publication_read)],
    source: Annotated[PayrollPublicationSource, Depends(get_payroll_publication_source)],
) -> PayrollPublicationReadResponse:
    if request.url.query:
        raise InternalPayrollProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    entity_ref = _entity_from_principal(principal)
    publication = source.pull_publication(
        entity_ref=entity_ref,
        publication_id=publication_id,
        idempotency_key=_server_idempotency_key(entity_ref, publication_id),
    )
    return PayrollPublicationReadResponse(
        entity_ref=entity_ref,
        company_id=publication.company_id,
        publication_id=publication.publication_id,
        publication=publication.payload_copy(),
    )


@router.get(
    "/payroll/test-workspaces/{test_batch_id}",
    response_model=PayrollTestWorkspaceReadResponse,
    dependencies=[Depends(require_payroll_test_workspaces)],
)
def get_payroll_test_workspace(
    test_batch_id: str,
    request: Request,
    assertion: Annotated[str, Header(alias="X-LedgerBridge-User-Assertion")],
    principal: Annotated[WorkloadPrincipal, Depends(require_payroll_live_read)],
    settings: Annotated[Settings, Depends(get_settings)],
    source: Annotated[PayrollTestWorkspaceSource, Depends(get_payroll_test_workspace_source)],
    signer: Annotated[HmacPayrollProviderAssertionSigner, Depends(get_payroll_provider_signer)],
    replay_store: Annotated[
        PayrollAssertionReplayStore, Depends(get_payroll_assertion_replay_store)
    ],
) -> PayrollTestWorkspaceReadResponse:
    if request.url.query:
        raise InternalPayrollProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    claims = _verify_user_for_grant(
        assertion,
        settings=settings,
        principal=principal,
        method="GET",
        path=request.url.path,
        body=b"",
        action="payroll.test_workspace.read",
        resource_ref=test_batch_id,
    )
    if not replay_store.consume(claims):
        raise PayrollUserAssertionError("payroll assertion replayed")
    company_id = _company_for_entity(settings, claims.entity_ref)
    provider_path = f"{TEST_WORKSPACES_PATH}/{test_batch_id}"
    headers = signer.headers(
        user=claims,
        company_id=company_id,
        provider_action="payroll.test_workspace.read",
        method="GET",
        path=provider_path,
        body=b"",
    )
    result = source.read_workspace(
        entity_ref=claims.entity_ref, test_batch_id=test_batch_id, provider_headers=headers
    )
    if result.entity_ref != claims.entity_ref or result.company_id != company_id:
        raise PayrollIntegrationError(
            "PAYROLL_IDENTITY_SCOPE_MISMATCH", "payroll test workspace crosses company scope"
        )
    return PayrollTestWorkspaceReadResponse(
        entity_ref=result.entity_ref, company_id=result.company_id, data=result.payload_copy()
    )


@router.get(
    "/payroll/test-workspaces/{test_batch_id}/materials/{material_id}/preview",
    response_model=PayrollTestMaterialPreviewResponse,
    dependencies=[Depends(require_payroll_test_workspaces)],
)
def get_payroll_test_material_preview(
    test_batch_id: str,
    material_id: str,
    request: Request,
    assertion: Annotated[str, Header(alias="X-LedgerBridge-User-Assertion")],
    principal: Annotated[WorkloadPrincipal, Depends(require_payroll_live_read)],
    settings: Annotated[Settings, Depends(get_settings)],
    source: Annotated[PayrollTestWorkspaceSource, Depends(get_payroll_test_workspace_source)],
    signer: Annotated[HmacPayrollProviderAssertionSigner, Depends(get_payroll_provider_signer)],
    replay_store: Annotated[
        PayrollAssertionReplayStore, Depends(get_payroll_assertion_replay_store)
    ],
) -> PayrollTestMaterialPreviewResponse:
    if request.url.query:
        raise InternalPayrollProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    claims = _verify_user_for_grant(
        assertion,
        settings=settings,
        principal=principal,
        method="GET",
        path=request.url.path,
        body=b"",
        action="payroll.test_workspace.read",
        resource_ref=material_id,
    )
    if not replay_store.consume(claims):
        raise PayrollUserAssertionError("payroll assertion replayed")
    company_id = _company_for_entity(settings, claims.entity_ref)
    provider_path = f"{TEST_WORKSPACES_PATH}/{test_batch_id}/materials/{material_id}/preview"
    headers = signer.headers(
        user=claims,
        company_id=company_id,
        provider_action="payroll.test_workspace.read",
        method="GET",
        path=provider_path,
        body=b"",
    )
    result = source.preview_material(
        entity_ref=claims.entity_ref,
        test_batch_id=test_batch_id,
        material_id=material_id,
        provider_headers=headers,
    )
    if result.entity_ref != claims.entity_ref or result.company_id != company_id:
        raise PayrollIntegrationError(
            "PAYROLL_IDENTITY_SCOPE_MISMATCH", "payroll test preview crosses company scope"
        )
    return PayrollTestMaterialPreviewResponse(
        entity_ref=result.entity_ref,
        company_id=result.company_id,
        material_id=material_id,
        data=result.payload_copy(),
    )


@router.get(
    "/payroll/test-workspaces/{test_batch_id}/legacy-features",
    response_model=PayrollLegacyFeatureReadResponse,
    dependencies=[Depends(require_payroll_test_workspaces)],
)
def get_payroll_legacy_features(
    test_batch_id: str,
    request: Request,
    assertion: Annotated[str, Header(alias="X-LedgerBridge-User-Assertion")],
    principal: Annotated[WorkloadPrincipal, Depends(require_payroll_live_read)],
    settings: Annotated[Settings, Depends(get_settings)],
    source: Annotated[PayrollTestWorkspaceSource, Depends(get_payroll_test_workspace_source)],
    signer: Annotated[HmacPayrollProviderAssertionSigner, Depends(get_payroll_provider_signer)],
    replay_store: Annotated[
        PayrollAssertionReplayStore, Depends(get_payroll_assertion_replay_store)
    ],
) -> PayrollLegacyFeatureReadResponse:
    if request.url.query:
        raise InternalPayrollProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    claims = _verify_user_for_grant(
        assertion,
        settings=settings,
        principal=principal,
        method="GET",
        path=request.url.path,
        body=b"",
        action="payroll.test_workspace.legacy.read",
        resource_ref=test_batch_id,
    )
    if not replay_store.consume(claims):
        raise PayrollUserAssertionError("payroll assertion replayed")
    company_id = _company_for_entity(settings, claims.entity_ref)
    provider_path = f"{TEST_WORKSPACES_PATH}/{test_batch_id}/legacy-features"
    headers = signer.headers(
        user=claims,
        company_id=company_id,
        provider_action="payroll.test_workspace.legacy.read",
        method="GET",
        path=provider_path,
        body=b"",
    )
    result = source.read_legacy_features(
        entity_ref=claims.entity_ref,
        test_batch_id=test_batch_id,
        provider_headers=headers,
    )
    if result.entity_ref != claims.entity_ref or result.company_id != company_id:
        raise PayrollIntegrationError(
            "PAYROLL_IDENTITY_SCOPE_MISMATCH", "payroll legacy features cross company scope"
        )
    return PayrollLegacyFeatureReadResponse(
        entity_ref=result.entity_ref,
        company_id=result.company_id,
        data=result.payload_copy(),
    )


@router.post(
    "/payroll/test-workspaces/{test_batch_id}/legacy-features/commands",
    response_model=PayrollLegacyFeatureCommandResponse,
    dependencies=[Depends(require_payroll_test_workspaces)],
)
async def execute_payroll_legacy_feature(
    test_batch_id: str,
    command: PayrollLegacyFeatureCommand,
    request: Request,
    assertion: Annotated[str, Header(alias="X-LedgerBridge-User-Assertion")],
    principal: Annotated[WorkloadPrincipal, Depends(require_payroll_command)],
    settings: Annotated[Settings, Depends(get_settings)],
    source: Annotated[PayrollTestWorkspaceSource, Depends(get_payroll_test_workspace_source)],
    signer: Annotated[HmacPayrollProviderAssertionSigner, Depends(get_payroll_provider_signer)],
    replay_store: Annotated[
        PayrollAssertionReplayStore, Depends(get_payroll_assertion_replay_store)
    ],
) -> PayrollLegacyFeatureCommandResponse:
    body = await request.body()
    claims = _verify_user_for_grant(
        assertion,
        settings=settings,
        principal=principal,
        method="POST",
        path=request.url.path,
        body=body,
        action="payroll.test_workspace.legacy.command",
        resource_ref=test_batch_id,
        expected_revision=command.expected_revision,
        operation_id=command.idempotency_key,
    )
    if not replay_store.consume(claims):
        raise PayrollUserAssertionError("payroll assertion replayed")
    company_id = _company_for_entity(settings, claims.entity_ref)
    provider_path = f"{TEST_WORKSPACES_PATH}/{test_batch_id}/legacy-features/commands"
    headers = dict(
        signer.headers(
            user=claims,
            company_id=company_id,
            provider_action="payroll.test_workspace.legacy.command",
            method="POST",
            path=provider_path,
            body=body,
        )
    )
    headers["X-Payroll-Test-Intent"] = "manage-legacy-payroll-features"
    result = source.execute_legacy_feature(
        entity_ref=claims.entity_ref,
        test_batch_id=test_batch_id,
        expected_workspace_revision=command.expected_revision,
        expected_action=command.action,
        provider_headers=headers,
        body=body,
    )
    if result.entity_ref != claims.entity_ref or result.company_id != company_id:
        raise PayrollIntegrationError(
            "PAYROLL_IDENTITY_SCOPE_MISMATCH", "payroll legacy command crosses company scope"
        )
    return PayrollLegacyFeatureCommandResponse(
        entity_ref=result.entity_ref,
        company_id=result.company_id,
        action="payroll.test_workspace.legacy.command",
        resource_ref=test_batch_id,
        replayed=result.replayed,
        data=result.payload_copy(),
    )


async def _payroll_test_workspace_command(
    *,
    request: Request,
    assertion: str,
    principal: WorkloadPrincipal,
    settings: Settings,
    source: PayrollTestWorkspaceSource,
    signer: HmacPayrollProviderAssertionSigner,
    replay_store: PayrollAssertionReplayStore,
    test_batch_id: str,
    action: Literal[
        "payroll.test_workspace.create",
        "payroll.test_workspace.organize",
        "payroll.test_workspace.validate",
        "payroll.test_workspace.clear",
    ],
    expected_revision: int,
    operation_id: UUID,
    provider_path: str,
    resource_ref: str | None = None,
    provider_intent: str | None = None,
    material_id: str | None = None,
    reviewed_period: str | None = None,
    reviewed_material_type: str | None = None,
) -> PayrollTestWorkspaceCommandResponse:
    body = await request.body()
    command_resource_ref = resource_ref or test_batch_id
    claims = _verify_user_for_grant(
        assertion,
        settings=settings,
        principal=principal,
        method="POST",
        path=request.url.path,
        body=body,
        action=action,
        resource_ref=command_resource_ref,
        expected_revision=expected_revision,
        operation_id=operation_id,
    )
    if not replay_store.consume(claims):
        raise PayrollUserAssertionError("payroll assertion replayed")
    company_id = _company_for_entity(settings, claims.entity_ref)
    headers = dict(
        signer.headers(
            user=claims,
            company_id=company_id,
            provider_action=action,
            method="POST",
            path=provider_path,
            body=body,
        )
    )
    if provider_intent is not None:
        headers["X-Payroll-Test-Intent"] = provider_intent
    if action == "payroll.test_workspace.create":
        result = source.create_workspace(
            entity_ref=claims.entity_ref,
            test_batch_id=test_batch_id,
            provider_headers=headers,
            body=body,
        )
    elif action == "payroll.test_workspace.organize":
        if material_id is None or reviewed_period is None or reviewed_material_type is None:
            raise InternalPayrollProblem(status.HTTP_400_BAD_REQUEST, "INVALID_RESOURCE")
        result = source.organize_material(
            entity_ref=claims.entity_ref,
            test_batch_id=test_batch_id,
            material_id=material_id,
            expected_workspace_revision=expected_revision,
            expected_period=reviewed_period,
            expected_material_type=reviewed_material_type,
            provider_headers=headers,
            body=body,
        )
    elif action == "payroll.test_workspace.validate":
        result = source.validate_batches(
            entity_ref=claims.entity_ref,
            test_batch_id=test_batch_id,
            expected_workspace_revision=expected_revision,
            provider_headers=headers,
            body=body,
        )
    else:
        result = source.clear_workspace(
            entity_ref=claims.entity_ref,
            test_batch_id=test_batch_id,
            provider_headers=headers,
            body=body,
        )
    if result.entity_ref != claims.entity_ref or result.company_id != company_id:
        raise PayrollIntegrationError(
            "PAYROLL_IDENTITY_SCOPE_MISMATCH", "payroll test workspace crosses company scope"
        )
    return PayrollTestWorkspaceCommandResponse(
        entity_ref=result.entity_ref,
        company_id=result.company_id,
        action=action,
        resource_ref=command_resource_ref,
        replayed=result.replayed,
        data=result.payload_copy(),
    )


@router.post(
    "/payroll/test-workspaces",
    response_model=PayrollTestWorkspaceCommandResponse,
    dependencies=[Depends(require_payroll_test_workspaces)],
)
async def create_payroll_test_workspace(
    command: PayrollTestWorkspaceCreate,
    request: Request,
    assertion: Annotated[str, Header(alias="X-LedgerBridge-User-Assertion")],
    principal: Annotated[WorkloadPrincipal, Depends(require_payroll_command)],
    settings: Annotated[Settings, Depends(get_settings)],
    source: Annotated[PayrollTestWorkspaceSource, Depends(get_payroll_test_workspace_source)],
    signer: Annotated[HmacPayrollProviderAssertionSigner, Depends(get_payroll_provider_signer)],
    replay_store: Annotated[
        PayrollAssertionReplayStore, Depends(get_payroll_assertion_replay_store)
    ],
) -> PayrollTestWorkspaceCommandResponse:
    return await _payroll_test_workspace_command(
        request=request,
        assertion=assertion,
        principal=principal,
        settings=settings,
        source=source,
        signer=signer,
        replay_store=replay_store,
        test_batch_id=command.test_batch_id,
        action="payroll.test_workspace.create",
        expected_revision=command.expected_store_revision,
        operation_id=command.idempotency_key,
        provider_path=TEST_WORKSPACES_PATH,
    )


@router.post(
    "/payroll/test-workspaces/{test_batch_id}/clear",
    response_model=PayrollTestWorkspaceCommandResponse,
    dependencies=[Depends(require_payroll_test_workspaces)],
)
async def clear_payroll_test_workspace(
    test_batch_id: str,
    command: PayrollTestWorkspaceClear,
    request: Request,
    assertion: Annotated[str, Header(alias="X-LedgerBridge-User-Assertion")],
    principal: Annotated[WorkloadPrincipal, Depends(require_payroll_command)],
    settings: Annotated[Settings, Depends(get_settings)],
    source: Annotated[PayrollTestWorkspaceSource, Depends(get_payroll_test_workspace_source)],
    signer: Annotated[HmacPayrollProviderAssertionSigner, Depends(get_payroll_provider_signer)],
    replay_store: Annotated[
        PayrollAssertionReplayStore, Depends(get_payroll_assertion_replay_store)
    ],
) -> PayrollTestWorkspaceCommandResponse:
    return await _payroll_test_workspace_command(
        request=request,
        assertion=assertion,
        principal=principal,
        settings=settings,
        source=source,
        signer=signer,
        replay_store=replay_store,
        test_batch_id=test_batch_id,
        action="payroll.test_workspace.clear",
        expected_revision=command.expected_workspace_revision,
        operation_id=command.idempotency_key,
        provider_path=f"{TEST_WORKSPACES_PATH}/{test_batch_id}/clear",
        provider_intent="clear-test-workspace",
    )


@router.post(
    "/payroll/test-workspaces/{test_batch_id}/materials/{material_id}/organize",
    response_model=PayrollTestWorkspaceCommandResponse,
    dependencies=[Depends(require_payroll_test_workspaces)],
)
async def organize_payroll_test_material(
    test_batch_id: str,
    material_id: str,
    command: PayrollTestMaterialOrganize,
    request: Request,
    assertion: Annotated[str, Header(alias="X-LedgerBridge-User-Assertion")],
    principal: Annotated[WorkloadPrincipal, Depends(require_payroll_command)],
    settings: Annotated[Settings, Depends(get_settings)],
    source: Annotated[PayrollTestWorkspaceSource, Depends(get_payroll_test_workspace_source)],
    signer: Annotated[HmacPayrollProviderAssertionSigner, Depends(get_payroll_provider_signer)],
    replay_store: Annotated[
        PayrollAssertionReplayStore, Depends(get_payroll_assertion_replay_store)
    ],
) -> PayrollTestWorkspaceCommandResponse:
    return await _payroll_test_workspace_command(
        request=request,
        assertion=assertion,
        principal=principal,
        settings=settings,
        source=source,
        signer=signer,
        replay_store=replay_store,
        test_batch_id=test_batch_id,
        action="payroll.test_workspace.organize",
        expected_revision=command.expected_workspace_revision,
        operation_id=command.idempotency_key,
        provider_path=(f"{TEST_WORKSPACES_PATH}/{test_batch_id}/materials/{material_id}/organize"),
        resource_ref=material_id,
        provider_intent="organize-test-material",
        material_id=material_id,
        reviewed_period=command.period,
        reviewed_material_type=command.material_type,
    )


@router.post(
    "/payroll/test-workspaces/{test_batch_id}/validate",
    response_model=PayrollTestWorkspaceCommandResponse,
    dependencies=[Depends(require_payroll_test_workspaces)],
)
async def validate_payroll_test_batches(
    test_batch_id: str,
    command: PayrollTestBatchValidate,
    request: Request,
    assertion: Annotated[str, Header(alias="X-LedgerBridge-User-Assertion")],
    principal: Annotated[WorkloadPrincipal, Depends(require_payroll_command)],
    settings: Annotated[Settings, Depends(get_settings)],
    source: Annotated[PayrollTestWorkspaceSource, Depends(get_payroll_test_workspace_source)],
    signer: Annotated[HmacPayrollProviderAssertionSigner, Depends(get_payroll_provider_signer)],
    replay_store: Annotated[
        PayrollAssertionReplayStore, Depends(get_payroll_assertion_replay_store)
    ],
) -> PayrollTestWorkspaceCommandResponse:
    return await _payroll_test_workspace_command(
        request=request,
        assertion=assertion,
        principal=principal,
        settings=settings,
        source=source,
        signer=signer,
        replay_store=replay_store,
        test_batch_id=test_batch_id,
        action="payroll.test_workspace.validate",
        expected_revision=command.expected_workspace_revision,
        operation_id=command.idempotency_key,
        provider_path=f"{TEST_WORKSPACES_PATH}/{test_batch_id}/validate",
        provider_intent="validate-test-payroll-batches",
    )
