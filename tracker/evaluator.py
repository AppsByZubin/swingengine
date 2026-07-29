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

EMA_PERIOD = 21
SMA_PERIOD = 50
ANGLE_LOOKBACK = 3
MINIMUM_CANDLES = SMA_PERIOD + ANGLE_LOOKBACK


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
    ema_21: float
    sma_50: float
    ema_21_angle: float
    sma_50_angle: float


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
                    indicators = calculate_momentum_indicators(candles)
                    has_momentum = (
                        indicators.ema_21_angle
                        > self.settings.ema_angle_threshold
                        and indicators.sma_50_angle
                        > self.settings.sma_angle_threshold
                    )
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
                    "ema_21=%.4f sma_50=%.4f ema_21_angle=%.2f "
                    "sma_50_angle=%.2f has_momentum=%r",
                    candidate.trading_symbol,
                    indicators.ema_21,
                    indicators.sma_50,
                    indicators.ema_21_angle,
                    indicators.sma_50_angle,
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
) -> MomentumIndicators:
    """Match the notebook's EMA, SMA, and three-bar price-slope angles."""
    closes = [float(candle.close) for candle in candles]
    if len(closes) < MINIMUM_CANDLES:
        raise IndicatorCalculationError(
            f"At least {MINIMUM_CANDLES} daily candles are required"
        )
    if not all(isfinite(close) for close in closes):
        raise IndicatorCalculationError("Daily candles contain invalid closes")

    alpha = 2.0 / (EMA_PERIOD + 1.0)
    ema_values: list[float] = []
    ema = closes[0]
    for close in closes:
        ema = alpha * close + (1.0 - alpha) * ema
        ema_values.append(ema)

    sma_values = [
        sum(closes[index - SMA_PERIOD + 1 : index + 1]) / SMA_PERIOD
        for index in range(SMA_PERIOD - 1, len(closes))
    ]
    ema_21 = ema_values[-1]
    sma_50 = sma_values[-1]
    ema_21_angle = _angle(ema_21, ema_values[-1 - ANGLE_LOOKBACK])
    sma_50_angle = _angle(sma_50, sma_values[-1 - ANGLE_LOOKBACK])
    return MomentumIndicators(
        ema_21=ema_21,
        sma_50=sma_50,
        ema_21_angle=ema_21_angle,
        sma_50_angle=sma_50_angle,
    )


def _angle(current: float, previous: float) -> float:
    slope = (current - previous) / ANGLE_LOOKBACK
    return degrees(atan(max(-10.0, min(10.0, slope))))
