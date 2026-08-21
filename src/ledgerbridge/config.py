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
    database_role: str | None = "ledgerbridge_app"
    artifact_root: Path = Path("/var/lib/ledgerbridge/artifacts")

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
