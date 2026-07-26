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
    assert settings.rotation_enabled is True
    assert settings.configured is False
    assert settings.credential_errors == (
        "UPSTOX_EXPECTED_USER_ID is required",
        "UPSTOX_API_KEY is required",
        "UPSTOX_API_SECRET is required",
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
    assert settings.rotation_enabled is True
    assert settings.request_time.hour == 8
    assert settings.request_time.minute == 5
    assert settings.token_file == Path("/tmp/upstox-token.json")


def test_manual_monitoring_only_requires_expected_user_id() -> None:
    settings = UpstoxSettings.from_env(
        {
            "UPSTOX_TOKEN_MANAGEMENT_ENABLED": "true",
            "UPSTOX_TOKEN_MONITOR_ENABLED": "true",
            "UPSTOX_EXPECTED_USER_ID": "USER1",
            "UPSTOX_TOKEN_CHECK_INTERVAL_SECONDS": "10800",
        }
    )

    assert settings.configured
    assert settings.monitor_enabled
    assert not settings.rotation_enabled
    assert settings.monitor_interval_seconds == 10800


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
