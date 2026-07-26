from pathlib import Path

import pytest

from upstox.config import UpstoxConfigurationError, UpstoxSettings


def test_rotation_is_disabled_by_default() -> None:
    settings = UpstoxSettings.from_env({})

    assert settings.enabled is False
    assert settings.configured is False
    assert settings.credential_errors == ()


def test_enabled_rotation_reports_missing_credentials_without_crashing() -> None:
    settings = UpstoxSettings.from_env(
        {"UPSTOX_TOKEN_ROTATION_ENABLED": "true"}
    )

    assert settings.enabled is True
    assert settings.configured is False
    assert settings.credential_errors == (
        "UPSTOX_API_KEY is required",
        "UPSTOX_API_SECRET is required",
        "UPSTOX_EXPECTED_USER_ID is required",
    )


def test_enabled_rotation_loads_credentials_and_schedule() -> None:
    settings = UpstoxSettings.from_env(
        {
            "UPSTOX_TOKEN_ROTATION_ENABLED": "true",
            "UPSTOX_API_KEY": "client",
            "UPSTOX_API_SECRET": "secret",
            "UPSTOX_EXPECTED_USER_ID": "USER1",
            "UPSTOX_TOKEN_FILE": "/tmp/upstox-token.json",
            "UPSTOX_TOKEN_REQUEST_TIME": "08:05",
        }
    )

    assert settings.configured is True
    assert settings.request_time.hour == 8
    assert settings.request_time.minute == 5
    assert settings.token_file == Path("/tmp/upstox-token.json")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("UPSTOX_API_BASE_URL", "http://api.upstox.test"),
        ("UPSTOX_TOKEN_FILE", "relative.json"),
        ("UPSTOX_TOKEN_REQUEST_TIME", "7:30 tomorrow"),
        ("UPSTOX_WEBHOOK_PORT", "70000"),
        ("UPSTOX_WEBHOOK_PATH", "webhook"),
    ],
)
def test_invalid_runtime_configuration_is_rejected(
    name: str, value: str
) -> None:
    with pytest.raises(UpstoxConfigurationError):
        UpstoxSettings.from_env({name: value})

