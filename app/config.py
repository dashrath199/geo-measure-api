"""Application configuration.

Settings are defined once with Pydantic and loaded from (in precedence order):

1. Real environment variables (highest precedence)
2. A local ``.env`` file (see ``.env.example``)
3. Field defaults defined below (lowest precedence)

Usage::

    from app.config import get_settings

    settings = get_settings()
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings backed by environment variables.

    All fields can be overridden via environment variables or a ``.env``
    file using their uppercase names (e.g. ``LOG_LEVEL=DEBUG``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = Field(
        default="Geo Measure API",
        description="Application name used in docs and logs.",
    )
    environment: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Runtime environment identifier.",
    )
    debug: bool = Field(
        default=False,
        description="Whether to expose verbose error details.",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Root logger verbosity.",
    )
    host: str = Field(
        default="127.0.0.1",
        description="Interface the server binds to when run locally.",
    )
    port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        description="TCP port the server listens on.",
    )
    api_prefix: str = Field(
        default="/api",
        description="Prefix under which versioned API routes are mounted.",
    )
    database_url: str = Field(
        default="sqlite:///./geo_measure.db",
        description=(
            "SQLAlchemy database URL. SQLite file used by default; any "
            "SQLAlchemy-supported URL can be substituted."
        ),
    )
    max_upload_mb: int = Field(
        default=10,
        ge=1,
        le=1024,
        description="Maximum accepted upload size, in megabytes.",
    )
    max_features: int = Field(
        default=50_000,
        ge=1,
        le=10_000_000,
        description=(
            "Maximum features accepted per uploaded dataset. Bounds "
            "processing time and measurements-response size for files "
            "that are within the byte limit but densely packed."
        ),
    )
    storage_dir: str = Field(
        default="uploads",
        description=(
            "Directory where uploaded files are stored. Created on demand; "
            "internal names are UUIDs, never client-supplied."
        ),
    )


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached :class:`Settings` instance.

    The cache avoids re-reading the environment on every request while
    keeping a single source of truth for configuration values.

    Returns:
        The cached settings instance.
    """
    return Settings()
