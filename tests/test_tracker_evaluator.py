from datetime import UTC, date, datetime, timedelta, timezone

from database.repository import MomentumCandidate
from tracker.config import TrackerEvaluationSettings
from tracker.evaluator import (
    TrackerMomentumEvaluator,
    calculate_momentum_indicators,
)
from upstox.client import DailyCandle
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


def test_indicator_calculation_matches_notebook_price_slope_angles() -> None:
    indicators = calculate_momentum_indicators(
        candles([100 + index * 20 for index in range(60)])
    )

    assert indicators.ema_21 > indicators.sma_50
    assert indicators.ema_21_angle > 84
    assert indicators.sma_50_angle > 84


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
                return candles([100 + index * 20 for index in range(60)])
            return candles([100.0] * 60)

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
        (date(2026, 1, 12), date(2026, 7, 30)),
        (date(2026, 1, 12), date(2026, 7, 30)),
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
