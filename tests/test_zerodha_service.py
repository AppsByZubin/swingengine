from dataclasses import replace
from datetime import UTC, datetime

from zerodha.client import KiteAPIError
from zerodha.config import ZerodhaSettings
from zerodha.service import ZerodhaTokenService
from zerodha.store import TokenStore


class FakeAuthClient:
    def __init__(self) -> None:
        self.verified_tokens: list[str] = []
        self.verification_error: KiteAPIError | None = None

    def verify_access_token(self, token: str) -> None:
        self.verified_tokens.append(token)
        if self.verification_error is not None:
            raise self.verification_error


def enabled_settings(tmp_path) -> ZerodhaSettings:
    return ZerodhaSettings.from_env(
        {
            "ZERODHA_TOKEN_MANAGEMENT_ENABLED": "true",
            "ZERODHA_API_KEY": "kite-key",
            "ZERODHA_EXPECTED_USER_ID": "ZD1234",
            "ZERODHA_TOKEN_FILE": str(tmp_path / "token.json"),
        }
    )


def test_manual_token_is_verified_and_persisted(tmp_path) -> None:
    now = datetime(2026, 7, 27, 4, 0, tzinfo=UTC)
    settings = enabled_settings(tmp_path)
    client = FakeAuthClient()
    store = TokenStore(settings.token_file)
    service = ZerodhaTokenService(settings, store, client)  # type: ignore[arg-type]

    result = service.set_token("manual-token", now=now)

    assert result.ok
    assert client.verified_tokens == ["manual-token"]
    state = store.load()
    assert state.access_token == "manual-token"
    assert state.validation_status == "valid"
    assert "valid" in service.status_message()


def test_manual_token_is_not_saved_when_zerodha_rejects_it(tmp_path) -> None:
    settings = enabled_settings(tmp_path)
    client = FakeAuthClient()
    client.verification_error = KiteAPIError(
        "verification returned 403", status_code=403
    )
    store = TokenStore(settings.token_file)
    service = ZerodhaTokenService(settings, store, client)  # type: ignore[arg-type]

    result = service.set_token("bad-token")

    assert not result.ok
    assert not store.load().access_token


def test_disabled_service_reports_disabled(tmp_path) -> None:
    settings = replace(enabled_settings(tmp_path), enabled=False)
    client = FakeAuthClient()
    service = ZerodhaTokenService(
        settings, TokenStore(settings.token_file), client  # type: ignore[arg-type]
    )

    assert "disabled" in service.status_message()
    assert not service.set_token("token").ok


def test_status_message_reports_no_token_stored(tmp_path) -> None:
    settings = enabled_settings(tmp_path)
    client = FakeAuthClient()
    service = ZerodhaTokenService(
        settings, TokenStore(settings.token_file), client  # type: ignore[arg-type]
    )

    assert "No Zerodha trading token is stored" in service.status_message()
