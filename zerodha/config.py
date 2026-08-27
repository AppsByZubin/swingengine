"""Environment-backed configuration for Zerodha (Kite Connect) token storage."""

from dataclasses import dataclass
from os import environ
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse


class ZerodhaConfigurationError(ValueError):
    """Raised when Zerodha runtime configuration is invalid."""


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


@dataclass(frozen=True)
class ZerodhaSettings:
    """Settings for manually storing and verifying a Kite access token."""

    enabled: bool
    api_key: str
    expected_user_id: str
    api_base_url: str
    token_file: Path
    request_timeout_seconds: int
    credential_errors: tuple[str, ...]

    @property
    def configured(self) -> bool:
        return self.enabled and not self.credential_errors

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "ZerodhaSettings":
        values = environ if env is None else env
        errors: list[str] = []

        enabled = _parse_bool(
            values, "ZERODHA_TOKEN_MANAGEMENT_ENABLED", False, errors
        )

        api_key = values.get("ZERODHA_API_KEY", "").strip()
        expected_user_id = values.get("ZERODHA_EXPECTED_USER_ID", "").strip()
        credential_errors: list[str] = []
        if enabled:
            if not api_key:
                credential_errors.append("ZERODHA_API_KEY is required")
            if not expected_user_id:
                credential_errors.append(
                    "ZERODHA_EXPECTED_USER_ID is required"
                )

        api_base_url = values.get(
            "ZERODHA_API_BASE_URL", "https://api.kite.trade"
        ).strip().rstrip("/")
        parsed_url = urlparse(api_base_url)
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            errors.append("ZERODHA_API_BASE_URL must be an absolute HTTPS URL")

        token_file = Path(
            values.get(
                "ZERODHA_TOKEN_FILE",
                "/var/lib/swingengine/zerodha-token.json",
            ).strip()
        )
        if not token_file.is_absolute():
            errors.append("ZERODHA_TOKEN_FILE must be an absolute path")

        request_timeout_seconds = _parse_positive_int(
            values, "ZERODHA_TOKEN_REQUEST_TIMEOUT_SECONDS", 15, errors
        )

        if errors:
            raise ZerodhaConfigurationError("; ".join(errors))

        return cls(
            enabled=enabled,
            api_key=api_key,
            expected_user_id=expected_user_id,
            api_base_url=api_base_url,
            token_file=token_file,
            request_timeout_seconds=request_timeout_seconds,
            credential_errors=tuple(credential_errors),
        )
