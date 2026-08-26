"""Evaluate daily EMA/SMA momentum and maintain tracker state."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import logging
from math import atan, degrees, isfinite
from threading import Lock
from typing import Protocol
from zoneinfo import ZoneInfo

from database.repository import (
    MomentumCandidate,
    RepositoryError,
)
from tracker.config import TrackerEvaluationSettings
from upstox.client import DailyCandle, UpstoxAPIError
from upstox.store import TokenState, TokenStateError

LOGGER = logging.getLogger(__name__)

MOMENTUM_PERIODS = (5, 8, 13, 21)
TREND_PERIOD = 144
ADX_PERIOD = 8
ADX_THRESHOLD = 30.0
MOMENTUM_ANGLE_THRESHOLD_DEGREES = 40.0
MINIMUM_CANDLES = TREND_PERIOD + 2


class MomentumRepository(Protocol):
    def list_momentum_candidates(self) -> list[MomentumCandidate]:
        """Return untracked assets and pending tracked assets."""

    def record_momentum_evaluation(
        self,
        asset_id: int,
        has_momentum: bool,
        evaluation_date: date,
    ) -> bool:
        """Insert or update eligible tracker state and report persistence."""


class DailyCandleClient(Protocol):
    def get_daily_candles(
        self,
        access_token: str,
        instrument_key: str,
        from_date: date,
        through_date: date,
    ) -> list[DailyCandle]:
        """Return historical candles plus the current trading day."""


class AccessTokenStore(Protocol):
    def load(self) -> TokenState:
        """Load the current token state."""


class IndicatorCalculationError(ValueError):
    """Raised when candles cannot produce the requested indicators."""


@dataclass(frozen=True, slots=True)
class MomentumIndicators:
    ema_5: float
    ema_8: float
    ema_13: float
    ema_21: float
    angle_ema_21: float
    angle_threshold_degrees: float
    ema_144_high: float
    ema_144_close: float
    ema_144_low: float
    adx_8: float
    adx_8_rising: bool
    latest_open: float
    latest_close: float

    @property
    def has_up_momentum(self) -> bool:
        """Notebook's bullish regime: ADX-confirmed ribbon stacked up, EMA 21
        rising steeply enough, and the candle body above the EMA 144 band."""
        return (
            self.adx_8 > ADX_THRESHOLD
            and self.adx_8_rising
            and self.ema_5 > self.ema_8 > self.ema_13 > self.ema_21
            and self.angle_ema_21 > self.angle_threshold_degrees
            and self.ema_21 > self.ema_144_high
            and self.latest_open > self.ema_144_high
            and self.latest_close > self.ema_144_high
        )

    @property
    def has_down_momentum(self) -> bool:
        """Notebook's bearish regime: ADX-confirmed ribbon stacked down, EMA
        21 falling steeply enough, and the candle body below the EMA 144 band."""
        return (
            self.adx_8 > ADX_THRESHOLD
            and self.adx_8_rising
            and self.ema_5 < self.ema_8 < self.ema_13 < self.ema_21
            and self.angle_ema_21 < -self.angle_threshold_degrees
            and self.ema_21 < self.ema_144_low
            and self.latest_open < self.ema_144_low
            and self.latest_close < self.ema_144_low
        )

    @property
    def side(self) -> str | None:
        """The tracker side implied by the EMA ribbon, if any."""
        if self.has_up_momentum:
            return "buy"
        if self.has_down_momentum:
            return "sell"
        return None


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    ok: bool
    message: str
    evaluated: int = 0
    momentum: int = 0
    inserted: int = 0
    refreshed: int = 0
    cleared: int = 0
    failed: int = 0


class TrackerMomentumEvaluator:
    """Screen all eligible saved assets using daily Upstox candles."""

    def __init__(
        self,
        settings: TrackerEvaluationSettings,
        repository: MomentumRepository,
        candle_client: DailyCandleClient,
        token_store: AccessTokenStore,
    ):
        self.settings = settings
        self.repository = repository
        self.candle_client = candle_client
        self.token_store = token_store
        self._evaluation_lock = Lock()

    def evaluate_message(self) -> str:
        return self.evaluate().message

    def evaluate(
        self, *, now: datetime | None = None
    ) -> EvaluationResult:
        current = now or datetime.now(UTC)
        local_date = current.astimezone(
            ZoneInfo(self.settings.timezone_name)
        ).date()
        from_date = local_date - timedelta(
            days=self.settings.lookback_days - 1
        )

        with self._evaluation_lock:
            try:
                token_state = self.token_store.load()
            except TokenStateError:
                return EvaluationResult(
                    False,
                    ":warning: Upstox token state cannot be read.",
                )
            if not token_state.is_valid(current):
                return EvaluationResult(
                    False,
                    ":warning: A valid Upstox token is required. Use "
                    "`/swingengine auth set <token>`.",
                )

            try:
                candidates = self.repository.list_momentum_candidates()
            except RepositoryError as error:
                return EvaluationResult(False, f":warning: {error}")

            if not candidates:
                return EvaluationResult(
                    True,
                    ":white_check_mark: Tracker asset evaluation completed; "
                    "no eligible assets were found.",
                )

            evaluated = momentum_count = inserted = refreshed = cleared = failed = 0
            momentum_symbols: list[str] = []
            for candidate in candidates:
                if not candidate.instrument_key:
                    failed += 1
                    LOGGER.warning(
                        "Skipping momentum evaluation without instrument key "
                        "trading_symbol=%r",
                        candidate.trading_symbol,
                    )
                    continue
                try:
                    candles = self.candle_client.get_daily_candles(
                        token_state.access_token,
                        candidate.instrument_key,
                        from_date,
                        local_date,
                    )
                    indicators = calculate_momentum_indicators(
                        candles,
                        angle_threshold_degrees=(
                            self.settings.momentum_angle_threshold_degrees
                        ),
                    )
                    has_momentum = indicators.has_up_momentum
                    persisted = self.repository.record_momentum_evaluation(
                        candidate.asset_id,
                        has_momentum,
                        local_date,
                    )
                except (
                    IndicatorCalculationError,
                    RepositoryError,
                    UpstoxAPIError,
                ) as error:
                    failed += 1
                    LOGGER.warning(
                        "Momentum evaluation failed trading_symbol=%r: %s",
                        candidate.trading_symbol,
                        error,
                    )
                    continue

                evaluated += 1
                if has_momentum:
                    momentum_count += 1
                    momentum_symbols.append(candidate.trading_symbol)
                    if persisted:
                        if candidate.tracker_details_id is None:
                            inserted += 1
                        else:
                            refreshed += 1
                elif persisted:
                    cleared += 1

                LOGGER.info(
                    "Evaluated tracker momentum trading_symbol=%r "
                    "ema_5=%.4f ema_8=%.4f ema_13=%.4f ema_21=%.4f "
                    "angle_ema_21=%.4f ema_144_high=%.4f ema_144_low=%.4f "
                    "adx_8=%.4f adx_8_rising=%r has_momentum=%r",
                    candidate.trading_symbol,
                    indicators.ema_5,
                    indicators.ema_8,
                    indicators.ema_13,
                    indicators.ema_21,
                    indicators.angle_ema_21,
                    indicators.ema_144_high,
                    indicators.ema_144_low,
                    indicators.adx_8,
                    indicators.adx_8_rising,
                    has_momentum,
                )

        prefix = ":warning:" if failed else ":white_check_mark:"
        message = (
            f"{prefix} Tracker asset evaluation completed for "
            f"{evaluated:,} asset(s). Momentum: {momentum_count:,}; "
            f"inserted: {inserted:,}; refreshed: {refreshed:,}; "
            f"cleared: {cleared:,}; failed: {failed:,}."
        )
        if momentum_symbols:
            shown = ", ".join(f"`{symbol}`" for symbol in momentum_symbols[:20])
            if len(momentum_symbols) > 20:
                shown += f", and {len(momentum_symbols) - 20:,} more"
            message += f"\nMomentum assets: {shown}."
        return EvaluationResult(
            True,
            message,
            evaluated=evaluated,
            momentum=momentum_count,
            inserted=inserted,
            refreshed=refreshed,
            cleared=cleared,
            failed=failed,
        )


def calculate_momentum_indicators(
    candles: Sequence[DailyCandle],
    *,
    angle_threshold_degrees: float = MOMENTUM_ANGLE_THRESHOLD_DEGREES,
) -> MomentumIndicators:
    """Match the notebook's EMA ribbons: momentum (5/8/13/21) and trend (144)
    of daily OHLC, plus the ADX(8) regime filter."""
    opens = [float(candle.open) for candle in candles]
    highs = [float(candle.high) for candle in candles]
    lows = [float(candle.low) for candle in candles]
    closes = [float(candle.close) for candle in candles]
    if len(closes) < MINIMUM_CANDLES:
        raise IndicatorCalculationError(
            f"At least {MINIMUM_CANDLES} daily candles are required"
        )
    if not all(
        isfinite(value)
        for series in (opens, highs, lows, closes)
        for value in series
    ):
        raise IndicatorCalculationError("Daily candles contain invalid prices")

    emas = {period: _ema(closes, period) for period in MOMENTUM_PERIODS}
    ema_21_series = _ema_series(closes, 21)
    ema_21_prev = ema_21_series[-2]
    angle_ema_21 = (
        degrees(atan((ema_21_series[-1] - ema_21_prev) / ema_21_prev * 100))
        if ema_21_prev
        else 0.0
    )

    adx_series = _adx_series(highs, lows, closes, ADX_PERIOD)

    return MomentumIndicators(
        ema_5=emas[5],
        ema_8=emas[8],
        ema_13=emas[13],
        ema_21=emas[21],
        angle_ema_21=angle_ema_21,
        angle_threshold_degrees=angle_threshold_degrees,
        ema_144_high=_ema(highs, TREND_PERIOD),
        ema_144_close=_ema(closes, TREND_PERIOD),
        ema_144_low=_ema(lows, TREND_PERIOD),
        adx_8=adx_series[-1],
        adx_8_rising=adx_series[-1] > adx_series[-2],
        latest_open=opens[-1],
        latest_close=closes[-1],
    )


def _ema_series(closes: Sequence[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    ema = closes[0]
    series = [ema]
    for close in closes[1:]:
        ema = alpha * close + (1.0 - alpha) * ema
        series.append(ema)
    return series


def _ema(closes: Sequence[float], period: int) -> float:
    return _ema_series(closes, period)[-1]


def _true_range(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[float]:
    return [
        max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        for i in range(1, len(closes))
    ]


def _directional_movement(
    highs: Sequence[float], lows: Sequence[float]
) -> tuple[list[float], list[float]]:
    plus_dm = []
    minus_dm = []
    for i in range(1, len(highs)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(
            down_move if down_move > up_move and down_move > 0 else 0.0
        )
    return plus_dm, minus_dm


def _wilder_smoothed(values: Sequence[float], window: int) -> list[float]:
    """Wilder's smoothing: seed with the sum of the first `window` values,
    then decay the running total by 1/window on each later value."""
    smoothed = [sum(values[:window])]
    for value in values[window:]:
        smoothed.append(smoothed[-1] - smoothed[-1] / window + value)
    return smoothed


def _adx_series(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    window: int,
) -> list[float]:
    """Average Directional Index, Wilder's method (the same inputs the
    notebook's `ta.trend.adx` uses, computed directly rather than depending
    on pandas/`ta`)."""
    smoothed_tr = _wilder_smoothed(_true_range(highs, lows, closes), window)
    plus_dm, minus_dm = _directional_movement(highs, lows)
    smoothed_plus_dm = _wilder_smoothed(plus_dm, window)
    smoothed_minus_dm = _wilder_smoothed(minus_dm, window)

    dx = []
    for smoothed_range, plus, minus in zip(
        smoothed_tr, smoothed_plus_dm, smoothed_minus_dm
    ):
        plus_di = 100 * plus / smoothed_range if smoothed_range else 0.0
        minus_di = 100 * minus / smoothed_range if smoothed_range else 0.0
        denominator = plus_di + minus_di
        dx.append(
            100 * abs(plus_di - minus_di) / denominator if denominator else 0.0
        )

    adx = [sum(dx[:window]) / window]
    for value in dx[window:]:
        adx.append((adx[-1] * (window - 1) + value) / window)
    return adx
