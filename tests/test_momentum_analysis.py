from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from database.repository import AssetRecord, MomentumCandidate
from tracker.config import TrackerEvaluationSettings
from tracker.momentum_analysis import MomentumAnalysisError, MomentumAnalyzer
from upstox.client import DailyCandle, UpstoxAPIError
from upstox.store import TokenState

IST = timezone(timedelta(hours=5, minutes=30))


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


def settings() -> TrackerEvaluationSettings:
    return TrackerEvaluationSettings.from_env({})


def asset_record(symbol: str, asset_id: int = 42) -> AssetRecord:
    return AssetRecord(
        asset_id=asset_id,
        asset_name=f"{symbol} LIMITED",
        trading_symbol=symbol,
        instrument_key=f"NSE_EQ|{symbol}",
    )


class Repository:
    def __init__(
        self,
        assets: dict[str, AssetRecord] | None = None,
        tracker_assets: list[MomentumCandidate] | None = None,
    ) -> None:
        self.assets = assets or {}
        self.tracker_assets = tracker_assets or []
        self.recorded: list[tuple[int, bool, date, str | None]] = []

    def find_asset_by_trading_symbol(self, trading_symbol: str) -> AssetRecord | None:
        return self.assets.get(trading_symbol.upper())

    def list_assets(self) -> list[AssetRecord]:
        return list(self.assets.values())

    def list_tracker_assets(self) -> list[MomentumCandidate]:
        return self.tracker_assets

    def record_momentum_evaluation(
        self,
        asset_id: int,
        has_momentum: bool,
        evaluation_date: date,
        side: str | None = None,
    ) -> bool:
        self.recorded.append((asset_id, has_momentum, evaluation_date, side))
        return True


class Client:
    def __init__(self, closes_by_symbol: dict[str, list[float]]) -> None:
        self.closes_by_symbol = closes_by_symbol
        self.requests: list[str] = []

    def get_daily_candles(
        self,
        access_token: str,
        instrument_key: str,
        from_date: date,
        through_date: date,
    ) -> list[DailyCandle]:
        assert access_token == "token"
        self.requests.append(instrument_key)
        symbol = instrument_key.removeprefix("NSE_EQ|")
        closes = self.closes_by_symbol.get(symbol)
        if closes is None:
            raise UpstoxAPIError("no data", status_code=404)
        return candles(closes)


class Store:
    def __init__(self, valid: bool = True) -> None:
        self.valid = valid

    def load(self) -> TokenState:
        return TokenState(
            access_token="token" if self.valid else "",
            validation_status="valid" if self.valid else "invalid",
        )


RISING = [100 + index * 20 for index in range(60)]
FALLING = [100 + (60 - index) * 20 for index in range(60)]
FLAT = [100.0] * 60


def test_analyze_symbol_requires_a_saved_asset() -> None:
    analyzer = MomentumAnalyzer(settings(), Repository(), Client({}), Store())

    with pytest.raises(MomentumAnalysisError, match="not saved"):
        analyzer.analyze_symbol("MISSING")


def test_analyze_symbol_reports_momentum_and_side_without_writing() -> None:
    repository = Repository({"RISING": asset_record("RISING")})
    client = Client({"RISING": RISING})
    analyzer = MomentumAnalyzer(settings(), repository, client, Store())

    result = analyzer.analyze_symbol(
        "rising", now=datetime(2026, 7, 30, 11, 0, tzinfo=UTC)
    )

    assert result.has_momentum is True
    assert result.side == "buy"
    assert result.tracker_updated is False
    assert repository.recorded == []


def test_analyze_symbol_can_update_the_tracker() -> None:
    repository = Repository({"FALLING": asset_record("FALLING", asset_id=7)})
    client = Client({"FALLING": FALLING})
    analyzer = MomentumAnalyzer(settings(), repository, client, Store())

    result = analyzer.analyze_symbol(
        "FALLING",
        update_tracker=True,
        now=datetime(2026, 7, 30, 11, 0, tzinfo=UTC),
    )

    assert result.side == "sell"
    assert result.tracker_updated is True
    assert repository.recorded == [(7, True, date(2026, 7, 30), "sell")]


def test_analyze_symbol_requires_a_valid_token() -> None:
    repository = Repository({"FLAT": asset_record("FLAT")})
    analyzer = MomentumAnalyzer(settings(), repository, Client({}), Store(valid=False))

    with pytest.raises(MomentumAnalysisError, match="valid Upstox token"):
        analyzer.analyze_symbol("FLAT")


def test_analyze_assets_screens_every_saved_asset_and_counts_failures() -> None:
    repository = Repository(
        {
            "RISING": asset_record("RISING", asset_id=1),
            "FLAT": asset_record("FLAT", asset_id=2),
            "MISSING": asset_record("MISSING", asset_id=3),
        }
    )
    client = Client({"RISING": RISING, "FLAT": FLAT})
    analyzer = MomentumAnalyzer(settings(), repository, client, Store())

    batch = analyzer.analyze_assets(
        now=datetime(2026, 7, 30, 11, 0, tzinfo=UTC)
    )

    assert batch.failed == 1
    assert len(batch.results) == 2
    by_symbol = {result.trading_symbol: result for result in batch.results}
    assert by_symbol["RISING"].side == "buy"
    assert by_symbol["FLAT"].side is None
    assert repository.recorded == []


def test_analyze_assets_can_update_the_tracker_for_qualifying_assets() -> None:
    repository = Repository({"RISING": asset_record("RISING", asset_id=1)})
    client = Client({"RISING": RISING})
    analyzer = MomentumAnalyzer(settings(), repository, client, Store())

    batch = analyzer.analyze_assets(
        update_tracker=True,
        now=datetime(2026, 7, 30, 11, 0, tzinfo=UTC),
    )

    assert batch.results[0].tracker_updated is True
    assert repository.recorded == [(1, True, date(2026, 7, 30), "buy")]


def test_analyze_tracker_clears_momentum_and_side_that_lapsed() -> None:
    repository = Repository(
        tracker_assets=[
            MomentumCandidate(1, "RISING LIMITED", "RISING", "NSE_EQ|RISING", 7),
            MomentumCandidate(2, "FLAT LIMITED", "FLAT", "NSE_EQ|FLAT", 9),
        ]
    )
    client = Client({"RISING": RISING, "FLAT": FLAT})
    analyzer = MomentumAnalyzer(settings(), repository, client, Store())

    batch = analyzer.analyze_tracker(
        now=datetime(2026, 7, 30, 11, 0, tzinfo=UTC)
    )

    assert batch.failed == 0
    assert repository.recorded == [
        (1, True, date(2026, 7, 30), "buy"),
        (2, False, date(2026, 7, 30), None),
    ]
