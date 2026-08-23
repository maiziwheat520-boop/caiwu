from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
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
    artifact_root: Path = Path("/var/lib/ledgerbridge/artifacts")
    artifact_max_bytes: int = Field(default=50 * 1024 * 1024, gt=0, le=2**63 - 1)
    artifact_total_max_bytes: int = Field(default=10 * 1024 * 1024 * 1024, gt=0, le=2**63 - 1)
    artifact_staging_max_bytes: int = Field(default=512 * 1024 * 1024, gt=0, le=2**63 - 1)
    artifact_staging_ttl_seconds: int = Field(default=60 * 60, gt=0, le=2**31 - 1)
    enable_internal_upload: bool = False
    enable_internal_async_dispatch: bool = False
    dispatch_lease_seconds: int = Field(default=120, gt=0, le=3600)
    dispatch_max_attempts: int = Field(default=5, gt=0, le=16)
    dispatch_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    runner_socket_path: str = "/run/ledgerbridge-connector/runner.sock"

    @field_validator("artifact_root")
    @classmethod
    def artifact_root_must_be_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("artifact_root must be an absolute path")
        return value

    @model_validator(mode="after")
    def production_requires_split_database_roles(self) -> "Settings":
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


def escape_alembic_ini_value(value: str) -> str:
    return value.replace("%", "%%")


@lru_cache
def get_settings() -> Settings:
    return Settings()
