from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    """Environment-backed application settings.

    Secrets are intentionally loaded at runtime and must never be committed.
    """

    model_config = SettingsConfigDict(
        env_prefix="LEDGERBRIDGE_",
        extra="ignore",
    )

    env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    runtime_role: Literal["api", "worker", "migrate"] = "migrate"
    database_url: str | None = Field(default=None, min_length=1)
    api_database_url: str | None = Field(default=None, min_length=1)
    worker_database_url: str | None = Field(default=None, min_length=1)
    reader_database_url: str | None = Field(default=None, min_length=1)
    artifact_root: Path = Path("/var/lib/ledgerbridge/artifacts")
    artifact_max_bytes: int = Field(default=50 * 1024 * 1024, gt=0, le=2**63 - 1)
    artifact_total_max_bytes: int = Field(default=10 * 1024 * 1024 * 1024, gt=0, le=2**63 - 1)
    artifact_staging_max_bytes: int = Field(default=512 * 1024 * 1024, gt=0, le=2**63 - 1)
    artifact_staging_ttl_seconds: int = Field(default=60 * 60, gt=0, le=2**31 - 1)
    upload_read_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    upload_concurrency: int = Field(default=2, gt=0, le=64)
    enable_internal_upload: bool = False
    enable_internal_async_dispatch: bool = False
    enable_internal_read_api: bool = False
    internal_read_backend: Literal["synthetic", "database"] = "synthetic"
    internal_read_cursor_key: str | None = Field(default=None, min_length=32, max_length=256)
    internal_read_evidence_key_file: Path | None = None
    enable_internal_read_persistent_audit: bool = False
    enable_internal_read_persistent_receipt: bool = False
    internal_read_operational_gate: Literal["closed", "r1-production-v1"] = "closed"
    internal_read_transport: Literal["disabled", "unix-mtls-proxy"] = "disabled"
    internal_read_mtls_policy_path: Path | None = None
    enable_internal_candidate_command_api: bool = False
    internal_candidate_command_backend: Literal["synthetic", "database"] = "synthetic"
    internal_candidate_command_operational_gate: Literal["closed", "d1-production-v1"] = "closed"
    internal_command_assertion_key: SecretStr | None = Field(
        default=None,
        min_length=32,
        max_length=256,
    )
    internal_command_assertion_issuer: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )
    internal_command_assertion_audience: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )
    enable_review_api: bool = False
    enable_real_ingest: bool = False
    enable_payroll_integration: bool = False
    payroll_base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    payroll_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    payroll_company_mapping: dict[str, UUID] = Field(default_factory=dict)
    internal_read_policy_generation: int | None = Field(default=None, ge=1)
    dispatch_lease_seconds: int = Field(default=120, gt=0, le=3600)
    dispatch_max_attempts: int = Field(default=5, gt=0, le=16)
    dispatch_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    runner_socket_path: str = "/run/ledgerbridge-connector/runner.sock"
    runner_manifest_path: Path | None = None
    runner_verification_keys_path: Path | None = None
    runner_manifest_generation: str | None = Field(default=None, min_length=1, max_length=100)
    auth_provider: Literal["disabled", "trusted_gateway", "test"] = "disabled"
    auth_policy_generation: str | None = Field(default=None, min_length=1, max_length=100)
    auth_clock_skew_seconds: int = Field(default=30, ge=0, le=300)
    mail_provider: Literal["disabled", "microsoft_graph"] = "disabled"
    mailbox_id: str | None = Field(default=None, min_length=1, max_length=500)
    mail_folder: str = Field(default="inbox", min_length=1, max_length=500)
    mail_max_messages: int = Field(default=100, gt=0, le=100)
    mail_graph_page_size: int = Field(default=20, gt=0, le=50)
    mail_timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    @field_validator("artifact_root")
    @classmethod
    def artifact_root_must_be_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("artifact_root must be an absolute path")
        return value

    @model_validator(mode="after")
    def production_requires_split_database_roles(self) -> "Settings":
        if self.enable_real_ingest:
            raise ValueError(
                "real ingest remains unavailable until the S1 operational and I1 gates pass"
            )
        if self.enable_internal_read_api:
            if self.env == "production":
                if self.internal_read_operational_gate != "r1-production-v1":
                    raise ValueError(
                        "production internal read API remains unavailable until the R1 "
                        "operational gate"
                    )
                if self.internal_read_backend != "database":
                    raise ValueError(
                        "production internal read API requires the database reader backend"
                    )
                if (
                    self.internal_read_transport != "unix-mtls-proxy"
                    or self.internal_read_mtls_policy_path is None
                ):
                    raise ValueError(
                        "production internal read API requires the Unix-socket mTLS policy"
                    )
                if (
                    not self.enable_internal_read_persistent_audit
                    or not self.enable_internal_read_persistent_receipt
                ):
                    raise ValueError(
                        "production internal read API requires persistent audit and receipt sinks"
                    )
                if self.internal_read_evidence_key_file is None:
                    raise ValueError(
                        "production internal read API requires the encrypted evidence key file"
                    )
            if self.internal_read_policy_generation is None:
                raise ValueError(
                    "internal_read_policy_generation is required when the internal "
                    "read API is enabled"
                )
            if self.internal_read_backend == "database" and self.reader_database_url is None:
                raise ValueError(
                    "reader_database_url is required when internal_read_backend=database"
                )
            if self.internal_read_backend == "database" and self.internal_read_cursor_key is None:
                raise ValueError(
                    "internal_read_cursor_key is required when internal_read_backend=database"
                )
        if self.enable_internal_read_persistent_audit:
            if (
                self.env == "production"
                and self.internal_read_operational_gate != "r1-production-v1"
            ):
                raise ValueError(
                    "production internal read persistent audit remains unavailable "
                    "until the R1 operational gate"
                )
            if not self.enable_internal_read_api:
                raise ValueError("persistent internal read audit requires the internal read API")
        if self.enable_internal_read_persistent_receipt:
            if (
                self.env == "production"
                and self.internal_read_operational_gate != "r1-production-v1"
            ):
                raise ValueError(
                    "production internal read persistent receipt remains unavailable "
                    "until the R1 operational gate"
                )
            if not self.enable_internal_read_api:
                raise ValueError("persistent internal read receipt requires the internal read API")
            if self.internal_read_backend != "database":
                raise ValueError(
                    "persistent internal read receipt requires the database reader backend"
                )
        if self.enable_internal_candidate_command_api:
            if self.env == "production":
                if self.internal_candidate_command_operational_gate != "d1-production-v1":
                    raise ValueError(
                        "production candidate command API remains unavailable until the D1 gate"
                    )
                if self.internal_candidate_command_backend != "database":
                    raise ValueError(
                        "production candidate command API requires the database command backend"
                    )
            if not self.enable_internal_read_api:
                raise ValueError("candidate command API requires the internal read API")
            if self.internal_candidate_command_backend == "synthetic" and (
                self.internal_read_backend != "synthetic" or self.env == "production"
            ):
                raise ValueError(
                    "synthetic candidate commands require the synthetic read backend "
                    "outside production"
                )
            if self.internal_candidate_command_backend == "database" and (
                self.internal_read_backend != "database" or self.api_database_url is None
            ):
                raise ValueError(
                    "database candidate commands require database reads and the API database role"
                )
            if (
                self.internal_command_assertion_key is None
                or self.internal_command_assertion_issuer is None
                or self.internal_command_assertion_audience is None
            ):
                raise ValueError(
                    "candidate command API requires assertion key, issuer, and audience"
                )
        if self.mail_provider == "microsoft_graph" and not self.mailbox_id:
            raise ValueError("mailbox_id is required when mail_provider=microsoft_graph")
        if self.enable_payroll_integration:
            if not self.enable_internal_read_api:
                raise ValueError("payroll integration requires the internal read API")
            if self.payroll_base_url is None:
                raise ValueError("payroll_base_url is required when payroll integration is enabled")
            if not self.payroll_company_mapping:
                raise ValueError(
                    "payroll_company_mapping is required when payroll integration is enabled"
                )

        if (self.runner_manifest_path is None) != (self.runner_verification_keys_path is None):
            raise ValueError(
                "runner_manifest_path and runner_verification_keys_path must be configured together"
            )
        for field_name in ("runner_manifest_path", "runner_verification_keys_path"):
            value = getattr(self, field_name)
            if value is not None and not value.is_absolute():
                raise ValueError(f"{field_name} must be an absolute path")
        if (
            self.internal_read_mtls_policy_path is not None
            and not self.internal_read_mtls_policy_path.is_absolute()
        ):
            raise ValueError("internal_read_mtls_policy_path must be an absolute path")
        if (
            self.internal_read_evidence_key_file is not None
            and not self.internal_read_evidence_key_file.is_absolute()
        ):
            raise ValueError("internal_read_evidence_key_file must be an absolute path")
        if (
            self.env == "production"
            and self.runner_manifest_path is not None
            and self.runner_manifest_generation is None
        ):
            raise ValueError("production runner manifest generation must be explicit")
        if self.auth_provider != "disabled" and self.auth_policy_generation is None:
            raise ValueError("auth_policy_generation is required when authentication is enabled")

        if self.runtime_role == "migrate":
            if not self.database_url:
                raise ValueError("database_url is required for the migrate runtime role")
        elif self.runtime_role == "api" and not self.api_database_url:
            raise ValueError("api_database_url is required for the api runtime role")
        elif self.runtime_role == "worker" and not self.worker_database_url:
            raise ValueError("worker_database_url is required for the worker runtime role")

        if (
            self.api_database_url
            and self.worker_database_url
            and self.api_database_url == self.worker_database_url
        ):
            raise ValueError("production API and worker database URLs must differ")

        if self.env == "production":
            if self.mail_provider != "disabled":
                raise ValueError(
                    "production mail provider remains disabled until auth and manifest gates"
                )
            if "runtime_role" not in self.model_fields_set:
                raise ValueError("production runtime_role must be explicit")
            role_urls: list[tuple[str, str | None]] = []
            if self.runtime_role == "api":
                role_urls.append(("ledgerbridge_api", self.api_database_url))
            elif self.runtime_role == "worker":
                role_urls.append(("ledgerbridge_worker", self.worker_database_url))
            else:
                role_urls.append(("ledgerbridge_owner", self.database_url))
                role_urls.extend(
                    [
                        ("ledgerbridge_api", self.api_database_url),
                        ("ledgerbridge_worker", self.worker_database_url),
                    ]
                )
            for expected_user, value in role_urls:
                if value is None:
                    continue
                try:
                    username = make_url(value).username
                except (TypeError, ValueError) as exc:
                    raise ValueError("production runtime database URL must be valid") from exc
                if username != expected_user:
                    raise ValueError(
                        "production runtime database URL must use dedicated runtime roles: "
                        f"{expected_user}"
                    )
        return self

    def resolved_api_database_url(self) -> str:
        if self.env == "production" and self.runtime_role != "api":
            raise ValueError("production API requires runtime_role=api")
        value = self.api_database_url or self.database_url
        if value is None:
            raise ValueError("an API database URL is required")
        return value

    def resolved_worker_database_url(self) -> str:
        if self.env == "production" and self.runtime_role != "worker":
            raise ValueError("production worker requires runtime_role=worker")
        value = self.worker_database_url or self.database_url
        if value is None:
            raise ValueError("a worker database URL is required")
        return value

    def resolved_reader_database_url(self) -> str:
        if self.reader_database_url is None:
            raise ValueError("a reader database URL is required")
        return self.reader_database_url


def escape_alembic_ini_value(value: str) -> str:
    return value.replace("%", "%%")


@lru_cache
def get_settings() -> Settings:
    return Settings()
