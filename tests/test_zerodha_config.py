from pathlib import Path

import pytest

from zerodha.config import ZerodhaConfigurationError, ZerodhaSettings


def test_token_management_is_disabled_by_default() -> None:
    settings = ZerodhaSettings.from_env({})

    assert settings.enabled is False
    assert settings.configured is False
    assert settings.credential_errors == ()


def test_enabled_management_reports_missing_credentials_without_crashing() -> None:
    settings = ZerodhaSettings.from_env(
        {"ZERODHA_TOKEN_MANAGEMENT_ENABLED": "true"}
    )

    assert settings.enabled is True
    assert settings.configured is False
    assert settings.credential_errors == (
        "ZERODHA_API_KEY is required",
        "ZERODHA_EXPECTED_USER_ID is required",
    )


def test_enabled_management_loads_credentials() -> None:
    settings = ZerodhaSettings.from_env(
        {
            "ZERODHA_TOKEN_MANAGEMENT_ENABLED": "true",
            "ZERODHA_API_KEY": "kite-key",
            "ZERODHA_EXPECTED_USER_ID": "ZD1234",
            "ZERODHA_TOKEN_FILE": "/tmp/zerodha-token.json",
        }
    )

    assert settings.configured is True
    assert settings.api_key == "kite-key"
    assert settings.expected_user_id == "ZD1234"
    assert settings.token_file == Path("/tmp/zerodha-token.json")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ZERODHA_API_BASE_URL", "http://api.kite.test"),
        ("ZERODHA_TOKEN_FILE", "relative.json"),
        ("ZERODHA_TOKEN_REQUEST_TIMEOUT_SECONDS", "-1"),
    ],
)
def test_invalid_runtime_configuration_is_rejected(
    name: str, value: str
) -> None:
    with pytest.raises(ZerodhaConfigurationError):
        ZerodhaSettings.from_env({name: value})
