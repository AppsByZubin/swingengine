from datetime import UTC, date, datetime, timedelta, timezone
from math import sin

from database.repository import MomentumCandidate
from tracker.config import TrackerEvaluationSettings
from tracker.evaluator import (
    DualTimeframeMomentum,
    TrackerMomentumEvaluator,
    calculate_daily_close_momentum,
    calculate_momentum_indicators,
)
from upstox.client import DailyCandle
from upstox.store import TokenState

IST = timezone(timedelta(hours=5, minutes=30))


def regime_closes(
    direction: int, base_bars: int = 140, rally_bars: int = 20
) -> list[float]:
    """A flat/noisy base followed by a sustained rally.

    A smooth monotonic ramp pins ADX(8) at 100 (never "rising"), so the base
    needs some noise and the rally needs to be recent enough that ADX is
    still climbing through the 30 threshold rather than already saturated.
    """
    closes = [100 + 3.0 * sin(index * 0.9) for index in range(base_bars)]
    close = closes[-1]
    for _ in range(rally_bars):
        close *= 1 + direction * 0.02
        closes.append(close)
    return closes


RISING = regime_closes(1)
FALLING = regime_closes(-1)
FLAT = [100.0] * len(RISING)


def candles(closes: list[float]) -> list[DailyCandle]:
    first_date = date(2026, 1, 1)
    return [
        DailyCandle(
            timestamp=datetime.combine(
                first_date + timedelta(days=index),
                datetime.min.time(),
                tzinfo=IST,
            ),
            open=close - 1,
            high=close + 1,
            low=close - 2,
            close=close,
            volume=1000,
            open_interest=0,
        )
        for index, close in enumerate(closes)
    ]


def settings() -> TrackerEvaluationSettings:
    return TrackerEvaluationSettings.from_env({})


def test_indicator_calculation_matches_notebook_momentum_ribbon() -> None:
    rising = calculate_momentum_indicators(candles(RISING))
    assert rising.ema_5 > rising.ema_8 > rising.ema_13 > rising.ema_21
    assert rising.has_up_momentum is True

    flat = calculate_momentum_indicators(candles(FLAT))
    assert flat.ema_5 == flat.ema_8 == flat.ema_13 == flat.ema_21
    assert flat.has_up_momentum is False


def test_indicator_side_reflects_the_ema_ribbon_direction() -> None:
    rising = calculate_momentum_indicators(candles(RISING))
    assert rising.side == "buy"

    falling = calculate_momentum_indicators(candles(FALLING))
    assert falling.has_down_momentum is True
    assert falling.side == "sell"

    flat = calculate_momentum_indicators(candles(FLAT))
    assert flat.side is None


def test_shallow_ema_21_angle_disqualifies_a_stacked_ribbon() -> None:
    shallow_rise = calculate_momentum_indicators(
        candles([100 + index * 20 for index in range(199)])
    )
    assert (
        shallow_rise.ema_5
        > shallow_rise.ema_8
        > shallow_rise.ema_13
        > shallow_rise.ema_21
    )
    assert shallow_rise.angle_ema_21 <= 40
    assert shallow_rise.has_up_momentum is False
    assert shallow_rise.side is None


def test_price_below_the_ema_144_band_disqualifies_a_steep_ribbon() -> None:
    # A rebound off a much higher base: fast EMAs already stack up and slope
    # steeply, but the slow EMA 144 trend band hasn't caught down yet, so the
    # rebound is still trading underneath it.
    decline_pct = (100 / 150) ** (1 / 150) - 1
    closes = []
    price = 150.0
    for index in range(150):
        price *= 1 + decline_pct
        closes.append(price)
    for _ in range(10):
        price *= 1.02
        closes.append(price)

    rebound = calculate_momentum_indicators(candles(closes))
    assert (
        rebound.ema_5 > rebound.ema_8 > rebound.ema_13 > rebound.ema_21
    )
    assert rebound.angle_ema_21 > 40
    assert rebound.adx_8 > 30
    assert rebound.adx_8_rising is True
    assert rebound.ema_21 < rebound.ema_144_high
    assert rebound.has_up_momentum is False
    assert rebound.side is None


def test_weak_adx_disqualifies_a_steep_ribbon_above_the_trend_band() -> None:
    # A brand-new spike: only two rally bars is enough for the fast EMAs to
    # stack up and clear the trend band, but ADX(8) hasn't built up enough
    # directional strength yet to confirm the move.
    closes = [100 + 3.0 * sin(index * 0.9) for index in range(146)]
    price = closes[-1]
    for _ in range(2):
        price *= 1.08
        closes.append(price)

    spike = calculate_momentum_indicators(candles(closes))
    assert spike.ema_5 > spike.ema_8 > spike.ema_13 > spike.ema_21
    assert spike.angle_ema_21 > 40
    assert spike.ema_21 > spike.ema_144_high
    assert spike.latest_open > spike.ema_144_high
    assert spike.latest_close > spike.ema_144_high
    assert spike.adx_8 <= 30
    assert spike.has_up_momentum is False
    assert spike.side is None


def test_daily_close_momentum_requires_close_and_angle_to_agree() -> None:
    rising = calculate_daily_close_momentum(candles(RISING))
    assert rising.close > rising.previous_close
    assert rising.angle_ema_21 > 30
    assert rising.side == "buy"

    falling = calculate_daily_close_momentum(candles(FALLING))
    assert falling.close < falling.previous_close
    assert falling.angle_ema_21 < -30
    assert falling.side == "sell"

    flat = calculate_daily_close_momentum(candles(FLAT))
    assert flat.close == flat.previous_close
    assert flat.side is None


def test_dual_timeframe_momentum_requires_both_timeframes_to_agree() -> None:
    daily_buy = calculate_daily_close_momentum(candles(RISING))
    daily_sell = calculate_daily_close_momentum(candles(FALLING))
    hourly_buy = calculate_momentum_indicators(candles(RISING))
    hourly_sell = calculate_momentum_indicators(candles(FALLING))

    agree_buy = DualTimeframeMomentum(daily_buy, hourly_buy)
    assert agree_buy.side == "buy"
    assert agree_buy.has_momentum is True

    disagree = DualTimeframeMomentum(daily_buy, hourly_sell)
    assert disagree.side is None
    assert disagree.has_momentum is False

    agree_sell = DualTimeframeMomentum(daily_sell, hourly_sell)
    assert agree_sell.side == "sell"
    assert agree_sell.has_momentum is True


def test_evaluator_inserts_momentum_and_clears_pending_nonmomentum() -> None:
    candidates = [
        MomentumCandidate(
            42,
            "RISING LIMITED",
            "RISING",
            "NSE_EQ|RISING",
            None,
        ),
        MomentumCandidate(
            43,
            "FLAT LIMITED",
            "FLAT",
            "NSE_EQ|FLAT",
            9,
        ),
    ]

    class Repository:
        def __init__(self) -> None:
            self.records: list[tuple[int, bool, date]] = []

        def list_momentum_candidates(self) -> list[MomentumCandidate]:
            return candidates

        def record_momentum_evaluation(
            self,
            asset_id: int,
            has_momentum: bool,
            evaluation_date: date,
        ) -> bool:
            self.records.append((asset_id, has_momentum, evaluation_date))
            return True

    class Client:
        def __init__(self) -> None:
            self.ranges: list[tuple[date, date]] = []

        def get_daily_candles(
            self,
            access_token: str,
            instrument_key: str,
            from_date: date,
            through_date: date,
        ) -> list[DailyCandle]:
            assert access_token == "token"
            self.ranges.append((from_date, through_date))
            if instrument_key.endswith("RISING"):
                return candles(RISING)
            return candles(FLAT)

        def get_hourly_candles(
            self,
            access_token: str,
            instrument_key: str,
            from_date: date,
            through_date: date,
        ) -> list[DailyCandle]:
            assert access_token == "token"
            if instrument_key.endswith("RISING"):
                return candles(RISING)
            return candles(FLAT)

    class Store:
        def load(self) -> TokenState:
            return TokenState(
                access_token="token",
                validation_status="valid",
            )

    repository = Repository()
    client = Client()
    evaluator = TrackerMomentumEvaluator(
        settings(),
        repository,
        client,
        Store(),
    )

    result = evaluator.evaluate(
        now=datetime(2026, 7, 30, 11, 0, tzinfo=UTC)
    )

    assert result.ok
    assert result.evaluated == 2
    assert result.momentum == 1
    assert result.inserted == 1
    assert result.cleared == 1
    assert repository.records == [
        (42, True, date(2026, 7, 30)),
        (43, False, date(2026, 7, 30)),
    ]
    assert client.ranges == [
        (date(2025, 10, 4), date(2026, 7, 30)),
        (date(2025, 10, 4), date(2026, 7, 30)),
    ]


def test_evaluator_requires_a_valid_stored_token() -> None:
    class Store:
        def load(self) -> TokenState:
            return TokenState()

    evaluator = TrackerMomentumEvaluator(
        settings(),
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        Store(),
    )

    result = evaluator.evaluate(
        now=datetime(2026, 7, 30, 11, 0, tzinfo=UTC)
    )

    assert not result.ok
    assert "valid Upstox token" in result.message


def test_evaluator_reports_missing_keys_without_aborting_batch() -> None:
    class Repository:
        def list_momentum_candidates(self) -> list[MomentumCandidate]:
            return [
                MomentumCandidate(42, "MISSING", "MISSING", None, None)
            ]

        def record_momentum_evaluation(
            self, asset_id: int, has_momentum: bool, evaluation_date: date
        ) -> bool:
            raise AssertionError("missing-key asset must not be persisted")

    class Store:
        def load(self) -> TokenState:
            return TokenState(
                access_token="token",
                validation_status="valid",
            )

    evaluator = TrackerMomentumEvaluator(
        settings(),
        Repository(),
        object(),  # type: ignore[arg-type]
        Store(),
    )

    result = evaluator.evaluate(
        now=datetime(2026, 7, 30, 11, 0, tzinfo=UTC)
    )

    assert result.ok
    assert result.failed == 1
    assert "failed: 1" in result.message
