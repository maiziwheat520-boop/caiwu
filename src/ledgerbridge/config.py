from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed application settings.

    Secrets are intentionally loaded at runtime and must never be committed.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LEDGERBRIDGE_",
        extra="ignore",
    )

    env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = Field(
        default="postgresql+psycopg://ledgerbridge:change-me@localhost:5432/ledgerbridge"
    )
    artifact_root: Path = Path("var/artifacts")


@lru_cache
def get_settings() -> Settings:
    return Settings()
