"""Catalogue-wide NSE equity momentum screening for Slack CSV exports."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import logging
from math import isfinite
from threading import Lock
from time import monotonic, sleep
from typing import Protocol
from zoneinfo import ZoneInfo

from tracker.config import TrackerEvaluationSettings
from tracker.evaluator import (
    IndicatorCalculationError,
    calculate_momentum_indicators,
)
from upstox.assets import (
    AssetCatalogError,
    AssetSearchResult,
)
from upstox.client import (
    DailyCandle,
    DailyMarketQuote,
    UpstoxAPIError,
)
from upstox.store import TokenState, TokenStateError

LOGGER = logging.getLogger(__name__)

QUOTE_BATCH_SIZE = 500
DEFAULT_PROGRESS_INTERVAL = 100
MINIMUM_MOMENTUM_CANDLES = 200


class MomentumScanError(RuntimeError):
    """Raised when a catalogue-wide momentum scan cannot be completed."""


@dataclass(frozen=True, slots=True)
class MomentumStock:
    """One equity that passed the momentum screen."""

    asset_name: str
    trading_symbol: str
    ltp: float


@dataclass(frozen=True, slots=True)
class MomentumScanResult:
    """Summary and export rows produced by a catalogue-wide scan."""

    catalog_instruments: int
    equity_assets: int
    evaluated: int
    failed: int
    stocks: tuple[MomentumStock, ...]
    ineligible: int = 0


class EquityCatalog(Protocol):
    def refresh(self) -> int:
        """Refresh the NSE instrument catalogue and return its row count."""

    def list_equities(self) -> list[AssetSearchResult]:
        """Return every NSE equity instrument from the refreshed catalogue."""


class MomentumMarketClient(Protocol):
    def get_daily_market_quotes(
        self,
        access_token: str,
        instrument_keys: Sequence[str],
    ) -> dict[str, DailyMarketQuote]:
        """Return batched daily OHLC and LTP snapshots."""

    def get_historical_daily_candles(
        self,
        access_token: str,
        instrument_key: str,
        from_date: date,
        through_date: date,
    ) -> list[DailyCandle]:
        """Return daily history without making an intraday request."""


class AccessTokenStore(Protocol):
    def load(self) -> TokenState:
        """Load the current Upstox token state."""


class NSEMomentumScanner:
    """Refresh the NSE catalogue and screen all of its equity instruments."""

    def __init__(
        self,
        settings: TrackerEvaluationSettings,
        catalog: EquityCatalog,
        market_client: MomentumMarketClient,
        token_store: AccessTokenStore,
        *,
        request_interval_seconds: float | None = None,
        progress_interval: int = DEFAULT_PROGRESS_INTERVAL,
        sleep_function: Callable[[float], None] = sleep,
    ):
        configured_request_interval = (
            settings.momentum_scan_request_interval_seconds
            if request_interval_seconds is None
            else request_interval_seconds
        )
        if (
            not isfinite(configured_request_interval)
            or configured_request_interval < 0
        ):
            raise ValueError(
                "request_interval_seconds must be finite and non-negative"
            )
        if progress_interval <= 0:
            raise ValueError("progress_interval must be positive")
        self.settings = settings
        self.catalog = catalog
        self.market_client = market_client
        self.token_store = token_store
        self.request_interval_seconds = configured_request_interval
        self.progress_interval = progress_interval
        self._sleep = sleep_function
        self._scan_lock = Lock()

    def scan(
        self,
        *,
        now: datetime | None = None,
    ) -> MomentumScanResult:
        """Run one complete, non-overlapping NSE equity momentum scan."""
        if not self._scan_lock.acquire(blocking=False):
            LOGGER.warning("Rejected NSE momentum scan because one is running")
            raise MomentumScanError("An NSE momentum scan is already running.")

        started_at = monotonic()
        current = now or datetime.now(UTC)
        local_timezone = ZoneInfo(self.settings.timezone_name)
        local_date = current.astimezone(local_timezone).date()
        from_date = local_date - timedelta(
            days=self.settings.momentum_scan_lookback_days - 1
        )
        historical_through_date = local_date - timedelta(days=1)
        LOGGER.info(
            "Starting NSE equity momentum scan local_date=%s from_date=%s "
            "historical_through_date=%s ema_angle_threshold=%.2f "
            "sma_angle_threshold=%.2f minimum_candles=%d "
            "request_interval_seconds=%.3f",
            local_date,
            from_date,
            historical_through_date,
            self.settings.ema_angle_threshold,
            self.settings.sma_angle_threshold,
            MINIMUM_MOMENTUM_CANDLES,
            self.request_interval_seconds,
        )
        try:
            catalog_instruments, equities = self._refresh_equities()
            token_state = self._valid_token(current)
            quotes = self._load_quotes(token_state.access_token, equities)
            if equities and not quotes:
                raise MomentumScanError(
                    "Unable to fetch NSE equity market quotes from Upstox."
                )

            stocks, evaluated, ineligible, failed = self._evaluate_equities(
                token_state.access_token,
                equities,
                quotes,
                from_date,
                historical_through_date,
                local_date,
                local_timezone,
                started_at,
            )
            stocks.sort(key=lambda stock: stock.trading_symbol.casefold())
            result = MomentumScanResult(
                catalog_instruments=catalog_instruments,
                equity_assets=len(equities),
                evaluated=evaluated,
                failed=failed,
                stocks=tuple(stocks),
                ineligible=ineligible,
            )
            LOGGER.info(
                "Completed NSE equity momentum scan catalog_instruments=%d "
                "equity_assets=%d evaluated=%d momentum=%d ineligible=%d "
                "failed=%d "
                "elapsed_seconds=%.2f",
                result.catalog_instruments,
                result.equity_assets,
                result.evaluated,
                len(result.stocks),
                result.ineligible,
                result.failed,
                monotonic() - started_at,
            )
            return result
        except MomentumScanError:
            LOGGER.exception(
                "NSE equity momentum scan did not complete "
                "elapsed_seconds=%.2f",
                monotonic() - started_at,
            )
            raise
        except Exception as error:
            LOGGER.exception(
                "Unexpected NSE equity momentum scan failure "
                "elapsed_seconds=%.2f",
                monotonic() - started_at,
            )
            raise MomentumScanError(
                "Unable to complete the NSE equity momentum scan."
            ) from error
        finally:
            self._scan_lock.release()

    def _refresh_equities(self) -> tuple[int, list[AssetSearchResult]]:
        try:
            catalog_instruments = self.catalog.refresh()
            equities = self.catalog.list_equities()
        except AssetCatalogError as error:
            raise MomentumScanError(str(error)) from error
        if not equities:
            raise MomentumScanError(
                "The refreshed catalogue contains no NSE equity instruments."
            )
        LOGGER.info(
            "Refreshed catalogue for NSE equity momentum scan "
            "catalog_instruments=%d equity_assets=%d",
            catalog_instruments,
            len(equities),
        )
        return catalog_instruments, equities

    def _valid_token(self, current: datetime) -> TokenState:
        try:
            token_state = self.token_store.load()
        except TokenStateError as error:
            raise MomentumScanError(
                "Upstox token state cannot be read."
            ) from error
        if not token_state.is_valid(current):
            LOGGER.warning("NSE equity momentum scan requires a valid token")
            raise MomentumScanError(
                "A valid Upstox token is required. Use "
                "`/swingengine auth set <token>`."
            )
        return token_state

    def _load_quotes(
        self,
        access_token: str,
        equities: Sequence[AssetSearchResult],
    ) -> dict[str, DailyMarketQuote]:
        instrument_keys = tuple(
            dict.fromkeys(
                asset.instrument_key
                for asset in equities
                if asset.instrument_key
            )
        )
        quotes: dict[str, DailyMarketQuote] = {}
        batch_count = (
            (len(instrument_keys) + QUOTE_BATCH_SIZE - 1)
            // QUOTE_BATCH_SIZE
        )
        for offset in range(0, len(instrument_keys), QUOTE_BATCH_SIZE):
            batch_number = offset // QUOTE_BATCH_SIZE + 1
            batch = instrument_keys[offset : offset + QUOTE_BATCH_SIZE]
            LOGGER.info(
                "Fetching NSE equity daily quote batch batch=%d/%d "
                "instrument_count=%d",
                batch_number,
                batch_count,
                len(batch),
            )
            try:
                batch_quotes = self.market_client.get_daily_market_quotes(
                    access_token,
                    batch,
                )
            except UpstoxAPIError:
                LOGGER.warning(
                    "Failed NSE equity daily quote batch batch=%d/%d "
                    "instrument_count=%d",
                    batch_number,
                    batch_count,
                    len(batch),
                    exc_info=True,
                )
                continue
            quotes.update(batch_quotes)
            LOGGER.info(
                "Completed NSE equity daily quote batch batch=%d/%d "
                "requested=%d received=%d cumulative_received=%d",
                batch_number,
                batch_count,
                len(batch),
                len(batch_quotes),
                len(quotes),
            )
        return quotes

    def _evaluate_equities(
        self,
        access_token: str,
        equities: Sequence[AssetSearchResult],
        quotes: dict[str, DailyMarketQuote],
        from_date: date,
        historical_through_date: date,
        local_date: date,
        local_timezone: ZoneInfo,
        started_at: float,
    ) -> tuple[list[MomentumStock], int, int, int]:
        stocks: list[MomentumStock] = []
        evaluated = ineligible = failed = historical_requests = 0
        total = len(equities)
        seen_instrument_keys: set[str] = set()

        for index, asset in enumerate(equities, start=1):
            instrument_key = asset.instrument_key
            if not instrument_key:
                failed += 1
                LOGGER.warning(
                    "Skipping NSE equity without instrument key index=%d/%d "
                    "trading_symbol=%r asset_name=%r",
                    index,
                    total,
                    asset.trading_symbol,
                    asset.name,
                )
                self._log_progress(
                    index,
                    total,
                    evaluated,
                    len(stocks),
                    ineligible,
                    failed,
                    started_at,
                )
                continue
            if instrument_key in seen_instrument_keys:
                failed += 1
                LOGGER.warning(
                    "Skipping duplicate NSE equity instrument key index=%d/%d "
                    "trading_symbol=%r instrument_key=%r",
                    index,
                    total,
                    asset.trading_symbol,
                    instrument_key,
                )
                self._log_progress(
                    index,
                    total,
                    evaluated,
                    len(stocks),
                    ineligible,
                    failed,
                    started_at,
                )
                continue
            seen_instrument_keys.add(instrument_key)

            quote = quotes.get(instrument_key)
            if quote is None:
                failed += 1
                LOGGER.warning(
                    "Skipping NSE equity without market quote index=%d/%d "
                    "trading_symbol=%r instrument_key=%r",
                    index,
                    total,
                    asset.trading_symbol,
                    instrument_key,
                )
                self._log_progress(
                    index,
                    total,
                    evaluated,
                    len(stocks),
                    ineligible,
                    failed,
                    started_at,
                )
                continue

            if historical_requests:
                self._sleep(self.request_interval_seconds)
            historical_requests += 1
            try:
                candles = self.market_client.get_historical_daily_candles(
                    access_token,
                    instrument_key,
                    from_date,
                    historical_through_date,
                )
                candles = _include_quote_candle(
                    candles,
                    quote,
                    local_date,
                    local_timezone,
                )
                if len(candles) < MINIMUM_MOMENTUM_CANDLES:
                    ineligible += 1
                    LOGGER.info(
                        "Skipping NSE equity with insufficient daily history "
                        "index=%d/%d trading_symbol=%r instrument_key=%r "
                        "candles=%d required=%d",
                        index,
                        total,
                        asset.trading_symbol,
                        instrument_key,
                        len(candles),
                        MINIMUM_MOMENTUM_CANDLES,
                    )
                    self._log_progress(
                        index,
                        total,
                        evaluated,
                        len(stocks),
                        ineligible,
                        failed,
                        started_at,
                    )
                    continue
                indicators = calculate_momentum_indicators(candles)
                has_momentum = (
                    indicators.ema_21_angle
                    > self.settings.ema_angle_threshold
                    and indicators.sma_50_angle
                    > self.settings.sma_angle_threshold
                )
            except (IndicatorCalculationError, UpstoxAPIError, ValueError):
                failed += 1
                LOGGER.warning(
                    "NSE equity momentum evaluation failed index=%d/%d "
                    "trading_symbol=%r instrument_key=%r",
                    index,
                    total,
                    asset.trading_symbol,
                    instrument_key,
                    exc_info=True,
                )
                self._log_progress(
                    index,
                    total,
                    evaluated,
                    len(stocks),
                    ineligible,
                    failed,
                    started_at,
                )
                continue

            evaluated += 1
            if has_momentum:
                stocks.append(
                    MomentumStock(
                        asset_name=asset.name,
                        trading_symbol=asset.trading_symbol,
                        ltp=quote.last_price,
                    )
                )
            LOGGER.debug(
                "Evaluated NSE equity momentum index=%d/%d "
                "trading_symbol=%r instrument_key=%r ltp=%.4f "
                "ema_21=%.4f sma_50=%.4f ema_21_angle=%.2f "
                "sma_50_angle=%.2f has_momentum=%r",
                index,
                total,
                asset.trading_symbol,
                instrument_key,
                quote.last_price,
                indicators.ema_21,
                indicators.sma_50,
                indicators.ema_21_angle,
                indicators.sma_50_angle,
                has_momentum,
            )
            self._log_progress(
                index,
                total,
                evaluated,
                len(stocks),
                ineligible,
                failed,
                started_at,
            )
        return stocks, evaluated, ineligible, failed

    def _log_progress(
        self,
        processed: int,
        total: int,
        evaluated: int,
        momentum: int,
        ineligible: int,
        failed: int,
        started_at: float,
    ) -> None:
        if processed % self.progress_interval and processed != total:
            return
        LOGGER.info(
            "NSE equity momentum scan progress processed=%d/%d "
            "evaluated=%d momentum=%d ineligible=%d failed=%d "
            "elapsed_seconds=%.2f",
            processed,
            total,
            evaluated,
            momentum,
            ineligible,
            failed,
            monotonic() - started_at,
        )


def _include_quote_candle(
    candles: Sequence[DailyCandle],
    quote: DailyMarketQuote,
    local_date: date,
    local_timezone: ZoneInfo,
) -> list[DailyCandle]:
    """Append the quote candle only when it represents a newer session."""
    combined = list(candles)
    quote_date = quote.candle.timestamp.astimezone(local_timezone).date()
    latest_date = (
        combined[-1].timestamp.astimezone(local_timezone).date()
        if combined
        else None
    )
    if quote_date <= local_date and (
        latest_date is None or quote_date > latest_date
    ):
        combined.append(quote.candle)
    return combined
