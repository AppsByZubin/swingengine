from dataclasses import replace
from datetime import UTC, datetime, timedelta

from upstox.client import TokenRequest, UpstoxAPIError
from upstox.config import UpstoxSettings
from upstox.service import TokenRotationService
from upstox.store import TokenStore


class FakeAuthClient:
    def __init__(self, authorization_expiry: int):
        self.authorization_expiry = authorization_expiry
        self.request_count = 0
        self.verified_tokens: list[str] = []
        self.verification_error: UpstoxAPIError | None = None

    def request_access_token(self) -> TokenRequest:
        self.request_count += 1
        return TokenRequest(self.authorization_expiry)

    def verify_access_token(self, token: str) -> None:
        self.verified_tokens.append(token)
        if self.verification_error is not None:
            raise self.verification_error


def enabled_settings(tmp_path) -> UpstoxSettings:
    return UpstoxSettings.from_env(
        {
            "UPSTOX_TOKEN_ROTATION_ENABLED": "true",
            "UPSTOX_API_KEY": "client",
            "UPSTOX_API_SECRET": "secret",
            "UPSTOX_EXPECTED_USER_ID": "USER1",
            "UPSTOX_TOKEN_FILE": str(tmp_path / "token.json"),
        }
    )


def test_scheduled_request_is_idempotent_per_local_day(tmp_path) -> None:
    now = datetime(2026, 7, 27, 3, 0, tzinfo=UTC)
    settings = enabled_settings(tmp_path)
    client = FakeAuthClient(
        int((now + timedelta(hours=20)).timestamp() * 1000)
    )
    service = TokenRotationService(
        settings, TokenStore(settings.token_file), client  # type: ignore[arg-type]
    )

    first = service.request_token(force=False, now=now)
    second = service.request_token(force=False, now=now)

    assert first.ok
    assert second.ok
    assert "already" in second.message
    assert client.request_count == 1


def test_valid_webhook_token_is_verified_and_stored(tmp_path) -> None:
    now = datetime(2026, 7, 27, 4, 0, tzinfo=UTC)
    settings = enabled_settings(tmp_path)
    client = FakeAuthClient(
        int((now + timedelta(hours=20)).timestamp() * 1000)
    )
    store = TokenStore(settings.token_file)
    service = TokenRotationService(
        settings, store, client  # type: ignore[arg-type]
    )
    payload = {
        "client_id": "client",
        "user_id": "USER1",
        "access_token": "approved-token",
        "token_type": "Bearer",
        "issued_at": str(int(now.timestamp() * 1000)),
        "expires_at": str(
            int((now + timedelta(hours=20)).timestamp() * 1000)
        ),
        "message_type": "access_token",
    }

    result = service.accept_webhook(payload, now=now)

    assert result.ok
    assert client.verified_tokens == ["approved-token"]
    assert store.load().access_token == "approved-token"
    assert "valid until" in service.status_message(now)


def test_webhook_rejects_wrong_account_without_verification(tmp_path) -> None:
    now = datetime(2026, 7, 27, 4, 0, tzinfo=UTC)
    settings = enabled_settings(tmp_path)
    client = FakeAuthClient(
        int((now + timedelta(hours=20)).timestamp() * 1000)
    )
    service = TokenRotationService(
        settings,
        TokenStore(settings.token_file),
        client,  # type: ignore[arg-type]
    )
    payload = {
        "client_id": "client",
        "user_id": "OTHER",
        "access_token": "wrong-account-token",
        "token_type": "Bearer",
        "issued_at": int(now.timestamp() * 1000),
        "expires_at": int(
            (now + timedelta(hours=20)).timestamp() * 1000
        ),
        "message_type": "access_token",
    }

    result = service.accept_webhook(payload, now=now)

    assert result.status_code == 403
    assert client.verified_tokens == []


def test_webhook_rejects_token_that_upstox_cannot_verify(tmp_path) -> None:
    now = datetime(2026, 7, 27, 4, 0, tzinfo=UTC)
    settings = enabled_settings(tmp_path)
    client = FakeAuthClient(
        int((now + timedelta(hours=20)).timestamp() * 1000)
    )
    client.verification_error = UpstoxAPIError("verification failed")
    service = TokenRotationService(
        settings,
        TokenStore(settings.token_file),
        client,  # type: ignore[arg-type]
    )
    payload = {
        "client_id": "client",
        "user_id": "USER1",
        "access_token": "unverifiable-token",
        "token_type": "Bearer",
        "issued_at": int(now.timestamp() * 1000),
        "expires_at": int(
            (now + timedelta(hours=20)).timestamp() * 1000
        ),
        "message_type": "access_token",
    }

    result = service.accept_webhook(payload, now=now)

    assert result.status_code == 502
    assert not TokenStore(settings.token_file).load().access_token


def test_disabled_service_reports_disabled(tmp_path) -> None:
    settings = replace(enabled_settings(tmp_path), enabled=False)
    client = FakeAuthClient(0)
    service = TokenRotationService(
        settings,
        TokenStore(settings.token_file),
        client,  # type: ignore[arg-type]
    )

    assert "disabled" in service.status_message()
    assert not service.request_token().ok

