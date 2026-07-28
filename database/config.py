"""Environment-backed PostgreSQL connection configuration."""

from dataclasses import dataclass, field
import os
from typing import Mapping


class DatabaseConfigurationError(ValueError):
    """Raised when PostgreSQL configuration is missing or invalid."""


@dataclass(frozen=True)
class DatabaseSettings:
    database_url: str = field(repr=False)
    connect_timeout_seconds: int = 10

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "DatabaseSettings":
        values = os.environ if env is None else env
        database_url = values.get("SWINGENGINE_DATABASE_URL", "").strip()
        errors: list[str] = []

        if not database_url:
            errors.append("SWINGENGINE_DATABASE_URL is required")

        try:
            connect_timeout_seconds = int(
                values.get(
                    "SWINGENGINE_DATABASE_CONNECT_TIMEOUT_SECONDS", "10"
                ).strip()
            )
        except ValueError:
            connect_timeout_seconds = 0
        if connect_timeout_seconds <= 0:
            errors.append(
                "SWINGENGINE_DATABASE_CONNECT_TIMEOUT_SECONDS must be "
                "a positive integer"
            )

        if errors:
            raise DatabaseConfigurationError("; ".join(errors))

        return cls(
            database_url=database_url,
            connect_timeout_seconds=connect_timeout_seconds,
        )
