from dataclasses import replace
from datetime import date
from typing import Any

from upstox.client import UpstoxAuthClient
from upstox.config import UpstoxSettings


class FakeResponse:
    status_code = 200

    def __init__(self, candles: list[list[Any]]) -> None:
        self._candles = candles

    def json(self) -> dict[str, Any]:
        return {
            "status": "success",
            "data": {"candles": self._candles},
        }


class RecordingSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses = [
            FakeResponse(
                [
                    [
                        "2026-07-29T00:00:00+05:30",
                        100,
                        110,
                        90,
                        105,
                        1000,
                        0,
                    ],
                    [
                        "2026-07-28T00:00:00+05:30",
                        90,
                        101,
                        85,
                        100,
                        900,
                        0,
                    ],
                ]
            ),
            FakeResponse(
                [
                    [
                        "2026-07-30T00:00:00+05:30",
                        105,
                        120,
                        100,
                        118,
                        1200,
                        0,
                    ]
                ]
            ),
        ]

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_daily_candles_merge_history_and_current_intraday_day() -> None:
    session = RecordingSession()
    settings = replace(
        UpstoxSettings.from_env({}),
        api_base_url="https://api.upstox.test",
    )
    client = UpstoxAuthClient(
        settings,
        session,  # type: ignore[arg-type]
    )

    result = client.get_daily_candles(
        "token",
        "NSE_EQ|INE044A01036",
        date(2026, 1, 12),
        date(2026, 7, 30),
    )

    assert [candle.timestamp.date() for candle in result] == [
        date(2026, 7, 28),
        date(2026, 7, 29),
        date(2026, 7, 30),
    ]
    assert result[-1].close == 118
    assert session.calls[0][0].endswith(
        "/v3/historical-candle/NSE_EQ%7CINE044A01036/days/1/"
        "2026-07-29/2026-01-12"
    )
    assert session.calls[1][0].endswith(
        "/v3/historical-candle/intraday/"
        "NSE_EQ%7CINE044A01036/days/1"
    )
    assert session.calls[0][1]["headers"]["Authorization"] == "Bearer token"


class HourlyRecordingSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses = [
            FakeResponse(
                [
                    [
                        "2026-07-29T10:15:00+05:30",
                        100,
                        110,
                        90,
                        105,
                        1000,
                        0,
                    ],
                    [
                        "2026-07-29T09:15:00+05:30",
                        90,
                        101,
                        85,
                        100,
                        900,
                        0,
                    ],
                ]
            ),
            FakeResponse(
                [
                    [
                        "2026-07-30T10:15:00+05:30",
                        105,
                        120,
                        100,
                        118,
                        1200,
                        0,
                    ],
                    [
                        "2026-07-30T09:15:00+05:30",
                        102,
                        108,
                        99,
                        105,
                        1100,
                        0,
                    ],
                ]
            ),
        ]

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_hourly_candles_merge_history_and_current_intraday_hours() -> None:
    session = HourlyRecordingSession()
    settings = replace(
        UpstoxSettings.from_env({}),
        api_base_url="https://api.upstox.test",
    )
    client = UpstoxAuthClient(
        settings,
        session,  # type: ignore[arg-type]
    )

    result = client.get_hourly_candles(
        "token",
        "NSE_EQ|INE044A01036",
        date(2026, 7, 29),
        date(2026, 7, 30),
    )

    assert [candle.close for candle in result] == [100, 105, 105, 118]
    assert session.calls[0][0].endswith(
        "/v3/historical-candle/NSE_EQ%7CINE044A01036/hours/1/"
        "2026-07-29/2026-07-29"
    )
    assert session.calls[1][0].endswith(
        "/v3/historical-candle/intraday/"
        "NSE_EQ%7CINE044A01036/hours/1"
    )
