import logging
from datetime import UTC, date, datetime, timedelta, timezone
from math import sin
from typing import Any

import pytest

from tracker.config import TrackerEvaluationSettings
from tracker.momentum_scanner import (
    MomentumScanError,
    NSEEmaRibbonScanner,
    _include_quote_candle,
)
from upstox.assets import AssetSearchResult
from upstox.client import (
    DailyCandle,
    DailyMarketQuote,
    UpstoxAPIError,
)
from upstox.store import TokenState

IST = timezone(timedelta(hours=5, minutes=30))


def _regime_closes(base_bars: int = 174, rally_bars: int = 25) -> list[float]:
    """A flat/noisy base followed by a sustained rally.

    A smooth monotonic ramp pins ADX(8) at 100 (never "rising"), so the base
    needs some noise and the rally needs to be recent enough that ADX is
    still climbing through the 30 threshold rather than already saturated.
    """
    closes = [100 + 3.0 * sin(index * 0.9) for index in range(base_bars)]
    close = closes[-1]
    for _ in range(rally_bars):
        close *= 1.02
        closes.append(close)
    return closes


RISING = _regime_closes()


def asset(symbol: str) -> AssetSearchResult:
    return AssetSearchResult(
        trading_symbol=symbol,
        name=f"{symbol} LIMITED",
        segment="NSE_EQ",
        instrument_type="EQ",
        instrument_key=f"NSE_EQ|{symbol}",
    )


def candles(closes: list[float]) -> list[DailyCandle]:
    first_date = date(2026, 1, 1)
    return [
        DailyCandle(
            timestamp=datetime.combine(
                first_date + timedelta(days=index),
                datetime.min.time(),
                tzinfo=IST,
            ),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1000,
            open_interest=0,
        )
        for index, close in enumerate(closes)
    ]


def quote(symbol: str, price: float) -> DailyMarketQuote:
    return DailyMarketQuote(
        instrument_key=f"NSE_EQ|{symbol}",
        last_price=price,
        candle=DailyCandle(
            timestamp=datetime(2026, 7, 30, tzinfo=IST),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=1000,
            open_interest=0,
        ),
    )


class Catalog:
    def __init__(self, events: list[str], equities: list[AssetSearchResult]):
        self.events = events
        self.equities = equities

    def refresh(self) -> int:
        self.events.append("refresh")
        return 12_345

    def list_equities(self) -> list[AssetSearchResult]:
        self.events.append("list_equities")
        return self.equities


class Store:
    def __init__(self, events: list[str], valid: bool = True):
        self.events = events
        self.valid = valid

    def load(self) -> TokenState:
        self.events.append("load_token")
        return TokenState(
            access_token="token" if self.valid else "",
            validation_status="valid" if self.valid else "unchecked",
        )


class Client:
    def __init__(self, events: list[str]):
        self.events = events
        self.ranges: list[tuple[str, date, date]] = []

    def get_daily_market_quotes(
        self,
        access_token: str,
        instrument_keys: tuple[str, ...],
    ) -> dict[str, DailyMarketQuote]:
        assert access_token == "token"
        self.events.append("quotes")
        return {
            key: quote(
                key.removeprefix("NSE_EQ|"),
                (
                    RISING[-1] * 1.02
                    if key.endswith("RISING")
                    else 100
                ),
            )
            for key in instrument_keys
        }

    def get_historical_daily_candles(
        self,
        access_token: str,
        instrument_key: str,
        from_date: date,
        through_date: date,
    ) -> list[DailyCandle]:
        assert access_token == "token"
        symbol = instrument_key.removeprefix("NSE_EQ|")
        self.events.append(f"history:{symbol}")
        self.ranges.append((symbol, from_date, through_date))
        if symbol == "BAD":
            raise UpstoxAPIError("historical request failed", 500)
        if symbol == "RISING":
            return candles(RISING)
        if symbol == "SHORT":
            return candles([100.0] * 198)
        return candles([100.0] * 199)


def settings(
    env: dict[str, str] | None = None,
) -> TrackerEvaluationSettings:
    return TrackerEvaluationSettings.from_env(env or {})


def test_scanner_refreshes_first_and_exports_only_momentum(
    caplog: Any,
) -> None:
    events: list[str] = []
    sleep_calls: list[float] = []
    client = Client(events)
    scanner = NSEEmaRibbonScanner(
        settings(),
        Catalog(events, [asset("RISING"), asset("FLAT")]),
        client,
        Store(events),
        sleep_function=sleep_calls.append,
        progress_interval=1,
    )

    with caplog.at_level(logging.INFO, logger="tracker.momentum_scanner"):
        result = scanner.scan(
            now=datetime(2026, 7, 30, 11, tzinfo=UTC)
        )

    assert events[:4] == [
        "refresh",
        "list_equities",
        "load_token",
        "quotes",
    ]
    assert result.catalog_instruments == 12_345
    assert result.equity_assets == 2
    assert result.evaluated == 2
    assert result.ineligible == 0
    assert result.failed == 0
    assert [stock.trading_symbol for stock in result.stocks] == ["RISING"]
    assert result.stocks[0].ltp == pytest.approx(RISING[-1] * 1.02)
    assert sleep_calls == [1.0]
    assert client.ranges == [
        ("RISING", date(2025, 7, 31), date(2026, 7, 29)),
        ("FLAT", date(2025, 7, 31), date(2026, 7, 29)),
    ]
    assert "Starting NSE equity momentum scan" in caplog.text
    assert "NSE equity momentum scan progress" in caplog.text
    assert "Completed NSE equity momentum scan" in caplog.text


def test_scanner_logs_one_asset_failure_and_continues(caplog: Any) -> None:
    events: list[str] = []
    scanner = NSEEmaRibbonScanner(
        settings(),
        Catalog(events, [asset("BAD"), asset("RISING")]),
        Client(events),
        Store(events),
        request_interval_seconds=0,
        progress_interval=1,
    )

    with caplog.at_level(logging.INFO, logger="tracker.momentum_scanner"):
        result = scanner.scan(
            now=datetime(2026, 7, 30, 11, tzinfo=UTC)
        )

    assert result.evaluated == 1
    assert result.failed == 1
    assert [stock.trading_symbol for stock in result.stocks] == ["RISING"]
    assert "trading_symbol='BAD'" in caplog.text
    assert "historical request failed" in caplog.text


def test_scanner_refreshes_before_rejecting_an_invalid_token() -> None:
    events: list[str] = []
    scanner = NSEEmaRibbonScanner(
        settings(),
        Catalog(events, [asset("RISING")]),
        Client(events),
        Store(events, valid=False),
        request_interval_seconds=0,
    )

    with pytest.raises(MomentumScanError, match="valid Upstox token"):
        scanner.scan(now=datetime(2026, 7, 30, 11, tzinfo=UTC))

    assert events == ["refresh", "list_equities", "load_token"]


def test_scanner_marks_assets_below_200_candles_as_ineligible(
    caplog: Any,
) -> None:
    events: list[str] = []
    scanner = NSEEmaRibbonScanner(
        settings(),
        Catalog(events, [asset("SHORT")]),
        Client(events),
        Store(events),
        request_interval_seconds=0,
        progress_interval=1,
    )

    with caplog.at_level(logging.INFO, logger="tracker.momentum_scanner"):
        result = scanner.scan(
            now=datetime(2026, 7, 30, 11, tzinfo=UTC)
        )

    assert result.evaluated == 0
    assert result.ineligible == 1
    assert result.failed == 0
    assert result.stocks == ()
    assert "insufficient daily history" in caplog.text
    assert "candles=199 required=200" in caplog.text
    assert "Traceback" not in caplog.text


def test_scanner_uses_configured_minimum_candle_count(caplog: Any) -> None:
    events: list[str] = []
    scanner = NSEEmaRibbonScanner(
        settings({"SWINGENGINE_MOMENTUM_SCAN_MINIMUM_CANDLES": "100"}),
        Catalog(events, [asset("SHORT")]),
        Client(events),
        Store(events),
        request_interval_seconds=0,
    )

    with caplog.at_level(logging.INFO, logger="tracker.momentum_scanner"):
        result = scanner.scan(
            now=datetime(2026, 7, 30, 11, tzinfo=UTC)
        )

    assert result.evaluated == 1
    assert result.ineligible == 0
    assert result.minimum_candles == 100
    assert "minimum_candles=100" in caplog.text


def test_scanner_fails_when_no_market_quote_batch_succeeds() -> None:
    events: list[str] = []

    class FailingQuoteClient(Client):
        def get_daily_market_quotes(
            self,
            access_token: str,
            instrument_keys: tuple[str, ...],
        ) -> dict[str, DailyMarketQuote]:
            raise UpstoxAPIError("quote request failed", 500)

    scanner = NSEEmaRibbonScanner(
        settings(),
        Catalog(events, [asset("RISING")]),
        FailingQuoteClient(events),
        Store(events),
        request_interval_seconds=0,
    )

    with pytest.raises(MomentumScanError, match="market quotes"):
        scanner.scan(now=datetime(2026, 7, 30, 11, tzinfo=UTC))


def test_quote_candle_is_not_duplicated_on_a_non_trading_day() -> None:
    friday = DailyCandle(
        timestamp=datetime(2026, 7, 31, tzinfo=IST),
        open=100,
        high=100,
        low=100,
        close=100,
        volume=1000,
        open_interest=0,
    )
    friday_quote = DailyMarketQuote(
        instrument_key="NSE_EQ|FLAT",
        last_price=100,
        candle=friday,
    )

    combined = _include_quote_candle(
        [friday],
        friday_quote,
        date(2026, 8, 2),
        timezone(timedelta(hours=5, minutes=30)),  # type: ignore[arg-type]
    )

    assert combined == [friday]
