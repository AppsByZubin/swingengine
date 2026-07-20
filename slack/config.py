"""Environment-backed runtime configuration."""

from dataclasses import dataclass
from os import environ
from typing import Mapping


class ConfigurationError(ValueError):
    """Raised when required application configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    bot_token: str
    app_token: str
    slash_command: str = "/swingengine"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        values = environ if env is None else env
        bot_token = values.get("SLACK_BOT_TOKEN", "").strip()
        app_token = values.get("SLACK_APP_TOKEN", "").strip()
        slash_command = values.get("SLACK_COMMAND", "/swingengine").strip()
        log_level = values.get("LOG_LEVEL", "INFO").strip().upper()

        errors: list[str] = []
        if not bot_token:
            errors.append("SLACK_BOT_TOKEN is required")
        elif not bot_token.startswith("xoxb-"):
            errors.append("SLACK_BOT_TOKEN must be a bot token beginning with xoxb-")

        if not app_token:
            errors.append("SLACK_APP_TOKEN is required")
        elif not app_token.startswith("xapp-"):
            errors.append("SLACK_APP_TOKEN must be an app token beginning with xapp-")

        if not slash_command.startswith("/") or any(
            char.isspace() for char in slash_command
        ):
            errors.append("SLACK_COMMAND must start with / and contain no whitespace")

        if errors:
            raise ConfigurationError("; ".join(errors))

        return cls(
            bot_token=bot_token,
            app_token=app_token,
            slash_command=slash_command,
            log_level=log_level,
        )
