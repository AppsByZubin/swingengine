from typing import Any

import pytest

from zerodha.client import KiteAPIError, KiteAuthClient
from zerodha.config import ZerodhaSettings


class FakeResponse:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.last_headers: dict[str, str] | None = None

    def get(self, url: str, headers: dict[str, str], timeout: int) -> FakeResponse:
        self.last_headers = headers
        return self.response


def settings() -> ZerodhaSettings:
    return ZerodhaSettings.from_env(
        {
            "ZERODHA_TOKEN_MANAGEMENT_ENABLED": "true",
            "ZERODHA_API_KEY": "kite-key",
            "ZERODHA_EXPECTED_USER_ID": "ZD1234",
            "ZERODHA_TOKEN_FILE": "/tmp/zerodha-token.json",
        }
    )


def test_verify_access_token_accepts_matching_user() -> None:
    response = FakeResponse(
        200, {"status": "success", "data": {"user_id": "ZD1234"}}
    )
    session = FakeSession(response)
    client = KiteAuthClient(settings(), session)  # type: ignore[arg-type]

    client.verify_access_token("some-token")

    assert session.last_headers["Authorization"] == "token kite-key:some-token"


def test_verify_access_token_rejects_unexpected_user() -> None:
    response = FakeResponse(
        200, {"status": "success", "data": {"user_id": "OTHER"}}
    )
    client = KiteAuthClient(
        settings(), FakeSession(response)  # type: ignore[arg-type]
    )

    with pytest.raises(KiteAPIError, match="unexpected user"):
        client.verify_access_token("some-token")


def test_verify_access_token_rejects_non_200_response() -> None:
    response = FakeResponse(401, {"status": "error"})
    client = KiteAuthClient(
        settings(), FakeSession(response)  # type: ignore[arg-type]
    )

    with pytest.raises(KiteAPIError) as excinfo:
        client.verify_access_token("some-token")
    assert excinfo.value.status_code == 401
