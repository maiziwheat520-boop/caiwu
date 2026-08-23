from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    database_url: str = Field(min_length=1)
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
        if self.env == "production":
            if not self.api_database_url or not self.worker_database_url:
                raise ValueError(
                    "production requires explicit api_database_url and worker_database_url"
                )
            if self.api_database_url == self.worker_database_url:
                raise ValueError("production API and worker database URLs must differ")
        return self

    def resolved_api_database_url(self) -> str:
        return self.api_database_url or self.database_url

    def resolved_worker_database_url(self) -> str:
        return self.worker_database_url or self.database_url


def escape_alembic_ini_value(value: str) -> str:
    return value.replace("%", "%%")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
