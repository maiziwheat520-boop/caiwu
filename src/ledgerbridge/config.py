from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
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
    artifact_root: Path = Path("/var/lib/ledgerbridge/artifacts")
    artifact_max_bytes: int = Field(default=50 * 1024 * 1024, gt=0, le=2**63 - 1)
    artifact_total_max_bytes: int = Field(default=10 * 1024 * 1024 * 1024, gt=0, le=2**63 - 1)
    artifact_staging_max_bytes: int = Field(default=512 * 1024 * 1024, gt=0, le=2**63 - 1)
    artifact_staging_ttl_seconds: int = Field(default=60 * 60, gt=0, le=2**31 - 1)

    @field_validator("artifact_root")
    @classmethod
    def artifact_root_must_be_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("artifact_root must be an absolute path")
        return value


def escape_alembic_ini_value(value: str) -> str:
    return value.replace("%", "%%")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
