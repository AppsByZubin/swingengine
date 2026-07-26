"""Environment-backed configuration for Upstox token rotation."""

from dataclasses import dataclass
from datetime import time
from os import environ
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class UpstoxConfigurationError(ValueError):
    """Raised when Upstox runtime configuration is invalid."""


def _parse_bool(
    values: Mapping[str, str],
    name: str,
    default: bool,
    errors: list[str],
) -> bool:
    raw_value = values.get(name)
    if raw_value is None or not raw_value.strip():
        return default

    normalized = raw_value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    errors.append(f"{name} must be true or false")
    return default


def _parse_positive_int(
    values: Mapping[str, str],
    name: str,
    default: int,
    errors: list[str],
) -> int:
    raw_value = values.get(name, str(default)).strip()
    try:
        parsed = int(raw_value)
    except ValueError:
        errors.append(f"{name} must be a positive integer")
        return default
    if parsed <= 0:
        errors.append(f"{name} must be a positive integer")
        return default
    return parsed


def _parse_time(raw_value: str, name: str, errors: list[str]) -> time:
    try:
        return time.fromisoformat(raw_value)
    except ValueError:
        errors.append(f"{name} must use HH:MM or HH:MM:SS")
        return time(hour=7, minute=30)


@dataclass(frozen=True)
class UpstoxSettings:
    """Settings for manual token management and optional future rotation."""

    enabled: bool
    monitor_enabled: bool
    monitor_interval_seconds: int
    rotation_enabled: bool
    api_key: str
    api_secret: str
    expected_user_id: str
    api_base_url: str
    token_file: Path
    request_time: time
    timezone_name: str
    request_timeout_seconds: int
    retry_interval_seconds: int
    scheduler_poll_interval_seconds: int
    verify_webhook_token: bool
    webhook_enabled: bool
    webhook_host: str
    webhook_port: int
    webhook_path: str
    credential_errors: tuple[str, ...]

    @property
    def configured(self) -> bool:
        return self.enabled and not self.credential_errors

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "UpstoxSettings":
        values = environ if env is None else env
        errors: list[str] = []

        management_enabled = _parse_bool(
            values, "UPSTOX_TOKEN_MANAGEMENT_ENABLED", False, errors
        )
        monitor_enabled = _parse_bool(
            values,
            "UPSTOX_TOKEN_MONITOR_ENABLED",
            management_enabled,
            errors,
        )
        rotation_enabled = _parse_bool(
            values, "UPSTOX_TOKEN_ROTATION_ENABLED", False, errors
        )
        webhook_enabled = _parse_bool(
            values, "UPSTOX_WEBHOOK_ENABLED", rotation_enabled, errors
        )
        verify_webhook_token = _parse_bool(
            values, "UPSTOX_VERIFY_WEBHOOK_TOKEN", True, errors
        )

        api_key = values.get("UPSTOX_API_KEY", "").strip()
        api_secret = values.get("UPSTOX_API_SECRET", "").strip()
        expected_user_id = values.get("UPSTOX_EXPECTED_USER_ID", "").strip()
        enabled = management_enabled or monitor_enabled or rotation_enabled
        credential_errors: list[str] = []
        if enabled:
            if not expected_user_id:
                credential_errors.append("UPSTOX_EXPECTED_USER_ID is required")
        if rotation_enabled:
            if not api_key:
                credential_errors.append("UPSTOX_API_KEY is required")
            if not api_secret:
                credential_errors.append("UPSTOX_API_SECRET is required")

        api_base_url = values.get(
            "UPSTOX_API_BASE_URL", "https://api.upstox.com"
        ).strip().rstrip("/")
        parsed_url = urlparse(api_base_url)
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            errors.append("UPSTOX_API_BASE_URL must be an absolute HTTPS URL")

        token_file = Path(
            values.get(
                "UPSTOX_TOKEN_FILE",
                "/var/lib/swingengine/upstox-token.json",
            ).strip()
        )
        if not token_file.is_absolute():
            errors.append("UPSTOX_TOKEN_FILE must be an absolute path")

        request_time = _parse_time(
            values.get("UPSTOX_TOKEN_REQUEST_TIME", "07:30").strip(),
            "UPSTOX_TOKEN_REQUEST_TIME",
            errors,
        )
        timezone_name = values.get(
            "UPSTOX_TOKEN_TIMEZONE", "Asia/Kolkata"
        ).strip()
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            errors.append("UPSTOX_TOKEN_TIMEZONE must be a valid IANA timezone")

        request_timeout_seconds = _parse_positive_int(
            values, "UPSTOX_TOKEN_REQUEST_TIMEOUT_SECONDS", 15, errors
        )
        monitor_interval_seconds = _parse_positive_int(
            values, "UPSTOX_TOKEN_CHECK_INTERVAL_SECONDS", 10_800, errors
        )
        retry_interval_seconds = _parse_positive_int(
            values, "UPSTOX_TOKEN_RETRY_INTERVAL_SECONDS", 300, errors
        )
        scheduler_poll_interval_seconds = _parse_positive_int(
            values, "UPSTOX_SCHEDULER_POLL_INTERVAL_SECONDS", 30, errors
        )

        webhook_host = values.get(
            "UPSTOX_WEBHOOK_HOST", "0.0.0.0"  # nosec B104
        ).strip()
        if not webhook_host:
            errors.append("UPSTOX_WEBHOOK_HOST cannot be empty")

        webhook_port = _parse_positive_int(
            values, "UPSTOX_WEBHOOK_PORT", 8080, errors
        )
        if webhook_port > 65535:
            errors.append("UPSTOX_WEBHOOK_PORT must be at most 65535")

        webhook_path = values.get(
            "UPSTOX_WEBHOOK_PATH", "/webhooks/upstox/token"
        ).strip()
        if (
            not webhook_path.startswith("/")
            or any(char.isspace() for char in webhook_path)
            or "?" in webhook_path
            or "#" in webhook_path
        ):
            errors.append(
                "UPSTOX_WEBHOOK_PATH must be an absolute path without "
                "whitespace, query parameters, or fragments"
            )

        if errors:
            raise UpstoxConfigurationError("; ".join(errors))

        return cls(
            enabled=enabled,
            monitor_enabled=monitor_enabled,
            monitor_interval_seconds=monitor_interval_seconds,
            rotation_enabled=rotation_enabled,
            api_key=api_key,
            api_secret=api_secret,
            expected_user_id=expected_user_id,
            api_base_url=api_base_url,
            token_file=token_file,
            request_time=request_time,
            timezone_name=timezone_name,
            request_timeout_seconds=request_timeout_seconds,
            retry_interval_seconds=retry_interval_seconds,
            scheduler_poll_interval_seconds=scheduler_poll_interval_seconds,
            verify_webhook_token=verify_webhook_token,
            webhook_enabled=webhook_enabled,
            webhook_host=webhook_host,
            webhook_port=webhook_port,
            webhook_path=webhook_path,
            credential_errors=tuple(credential_errors),
        )
