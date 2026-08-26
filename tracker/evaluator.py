"""Evaluate daily EMA/SMA momentum and maintain tracker state."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import logging
from math import isfinite
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
MINIMUM_CANDLES = max(MOMENTUM_PERIODS)


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

    @property
    def has_up_momentum(self) -> bool:
        """EMA ribbon momentum: fastest to slowest EMA fully stacked up."""
        return self.ema_5 > self.ema_8 > self.ema_13 > self.ema_21

    @property
    def has_down_momentum(self) -> bool:
        """EMA ribbon momentum: fastest to slowest EMA fully stacked down."""
        return self.ema_5 < self.ema_8 < self.ema_13 < self.ema_21

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
                    indicators = calculate_momentum_indicators(candles)
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
                    "has_momentum=%r",
                    candidate.trading_symbol,
                    indicators.ema_5,
                    indicators.ema_8,
                    indicators.ema_13,
                    indicators.ema_21,
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
    """Match the notebook's momentum ribbon: EMA 5/8/13/21 of daily closes."""
    closes = [float(candle.close) for candle in candles]
    if len(closes) < MINIMUM_CANDLES:
        raise IndicatorCalculationError(
            f"At least {MINIMUM_CANDLES} daily candles are required"
        )
    if not all(isfinite(close) for close in closes):
        raise IndicatorCalculationError("Daily candles contain invalid closes")

    emas = {period: _ema(closes, period) for period in MOMENTUM_PERIODS}
    return MomentumIndicators(
        ema_5=emas[5],
        ema_8=emas[8],
        ema_13=emas[13],
        ema_21=emas[21],
    )


def _ema(closes: Sequence[float], period: int) -> float:
    alpha = 2.0 / (period + 1.0)
    ema = closes[0]
    for close in closes:
        ema = alpha * close + (1.0 - alpha) * ema
    return ema
