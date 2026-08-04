from dataclasses import replace
from datetime import date

import pytest

from upstox.client import UpstoxAPIError, UpstoxAuthClient
from upstox.config import UpstoxSettings


class FakeResponse:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self.payload = payload

    def json(self) -> object:
        return self.payload


class RecordingSession:
    def __init__(self) -> None:
        self.post_calls: list[tuple[str, dict[str, object]]] = []
        self.get_calls: list[tuple[str, dict[str, object]]] = []
        self.post_response = FakeResponse(
            200,
            {
                "status": "success",
                "data": {"authorization_expiry": "1785209400000"},
            },
        )
        self.get_response = FakeResponse(
            200, {"status": "success", "data": {"user_id": "USER1"}}
        )

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.post_calls.append((url, kwargs))
        return self.post_response

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.get_calls.append((url, kwargs))
        return self.get_response


def settings() -> UpstoxSettings:
    return replace(
        UpstoxSettings.from_env({}),
        enabled=True,
        api_key="client/id",
        api_secret="do-not-log",
        expected_user_id="USER1",
    )


def test_access_token_request_uses_documented_endpoint_and_json_body() -> None:
    session = RecordingSession()
    client = UpstoxAuthClient(
        settings(), session  # type: ignore[arg-type]
    )

    result = client.request_access_token()

    assert result.authorization_expiry == 1785209400000
    url, request = session.post_calls[0]
    assert url.endswith("/v3/login/auth/token/request/client%2Fid")
    assert request["json"] == {"client_secret": "do-not-log"}
    assert request["timeout"] == 15


def test_access_token_verification_checks_expected_user() -> None:
    session = RecordingSession()
    client = UpstoxAuthClient(
        settings(), session  # type: ignore[arg-type]
    )

    client.verify_access_token("approved-token")

    url, request = session.get_calls[0]
    assert url.endswith("/v2/user/profile")
    headers = request["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer approved-token"


def test_access_token_verification_rejects_unexpected_user() -> None:
    session = RecordingSession()
    session.get_response = FakeResponse(
        200, {"status": "success", "data": {"user_id": "OTHER"}}
    )
    client = UpstoxAuthClient(
        settings(), session  # type: ignore[arg-type]
    )

    with pytest.raises(UpstoxAPIError, match="unexpected user"):
        client.verify_access_token("wrong-token")


def test_http_error_does_not_include_response_body_or_secret() -> None:
    session = RecordingSession()
    session.post_response = FakeResponse(
        401, {"message": "do-not-log", "client_secret": "do-not-log"}
    )
    client = UpstoxAuthClient(
        settings(), session  # type: ignore[arg-type]
    )

    with pytest.raises(UpstoxAPIError) as exception:
        client.request_access_token()

    assert "HTTP 401" in str(exception.value)
    assert "do-not-log" not in str(exception.value)


def test_historical_daily_candles_use_one_v3_request() -> None:
    session = RecordingSession()
    session.get_response = FakeResponse(
        200,
        {
            "status": "success",
            "data": {
                "candles": [
                    [
                        "2026-07-29T00:00:00+05:30",
                        100,
                        110,
                        95,
                        108,
                        1000,
                        0,
                    ]
                ]
            },
        },
    )
    client = UpstoxAuthClient(
        settings(), session  # type: ignore[arg-type]
    )

    candles = client.get_historical_daily_candles(
        "approved-token",
        "NSE_EQ|INE044A01036",
        date(2026, 1, 12),
        date(2026, 7, 29),
    )

    assert len(candles) == 1
    assert candles[0].close == 108
    assert len(session.get_calls) == 1
    url, request = session.get_calls[0]
    assert url.endswith(
        "/v3/historical-candle/"
        "NSE_EQ%7CINE044A01036/days/1/2026-07-29/2026-01-12"
    )
    assert "params" not in request


def test_daily_market_quotes_parse_ltp_and_live_daily_candle() -> None:
    session = RecordingSession()
    session.get_response = FakeResponse(
        200,
        {
            "status": "success",
            "data": {
                "NSE_EQ:SUNPHARMA": {
                    "last_price": 1789.25,
                    "instrument_token": "NSE_EQ|INE044A01036",
                    "live_ohlc": {
                        "open": 1770,
                        "high": 1800,
                        "low": 1765,
                        "close": 1789.25,
                        "volume": 123456,
                        "ts": 1785364200000,
                    },
                }
            },
        },
    )
    client = UpstoxAuthClient(
        settings(), session  # type: ignore[arg-type]
    )

    quotes = client.get_daily_market_quotes(
        "approved-token",
        ["NSE_EQ|INE044A01036"],
    )

    quote = quotes["NSE_EQ|INE044A01036"]
    assert quote.last_price == 1789.25
    assert quote.candle.close == 1789.25
    url, request = session.get_calls[0]
    assert url.endswith("/v3/market-quote/ohlc")
    assert request["params"] == {
        "instrument_key": "NSE_EQ|INE044A01036",
        "interval": "1d",
    }


def test_daily_market_quotes_reject_more_than_500_keys() -> None:
    client = UpstoxAuthClient(
        settings(), RecordingSession()  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="at most 500"):
        client.get_daily_market_quotes(
            "approved-token",
            [f"NSE_EQ|{index}" for index in range(501)],
        )
