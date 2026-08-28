"""Orchestrate limit-entry placement, fill polling, GTT exit placement, and
GTT poll-to-close — the four steps of automated trade execution."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import logging
from math import floor
from threading import Lock
from typing import Protocol
from zoneinfo import ZoneInfo

from database.repository import (
    PendingGttOrder,
    PendingLimitOrder,
    RepositoryError,
    TradeAwaitingGtt,
    TradeEntryCandidate,
    TradeOrderIds,
)
# Reuse the existing Wilder's-smoothing true-range helpers rather than
# re-deriving ATR math; they already back this codebase's ADX(8) indicator.
from tracker.evaluator import _true_range, _wilder_smoothed
from trade.config import TradeExecutionSettings
from upstox.client import DailyCandle, UpstoxAPIError
from upstox.store import TokenState as UpstoxTokenState
from upstox.store import TokenStateError as UpstoxTokenStateError
from zerodha.client import KiteAPIError, KiteGtt, KiteOrder
from zerodha.store import TokenState as ZerodhaTokenState
from zerodha.store import TokenStateError as ZerodhaTokenStateError

LOGGER = logging.getLogger(__name__)

EXCHANGE = "NSE"
TRANSACTION_TYPE_BUY = "BUY"
# NSE equity orders (and GTT trigger/leg prices) must be a multiple of this
# tick size, or Kite rejects the request with InputException.
NSE_TICK_SIZE = 0.05


class TradeExecutionError(ValueError):
    """Raised when a trade cannot be sized or priced from available data."""


class TradeRepository(Protocol):
    def list_trade_entry_candidates(
        self, minimum_amount_allocated: float
    ) -> list[TradeEntryCandidate]:
        """List buy-side tracker entries ready for a new limit entry order."""

    def create_trade_and_limit_order(
        self,
        *,
        asset_id: int,
        tracker_details_id: int,
        asset_name: str,
        trading_symbol: str,
        instrument_key: str | None,
        allocated_amount: float,
        quantity: int,
        price: float,
        broker_order_id: str,
    ) -> TradeOrderIds:
        """Open a trade and its limit entry order in one transaction."""

    def list_pending_limit_orders(self) -> list[PendingLimitOrder]:
        """List limit entry orders still awaiting a broker fill."""

    def record_limit_order_fill(
        self, order_id: int, tracker_details_id: int, executed_at: datetime
    ) -> None:
        """Mark a limit order filled and flag its tracker entry as traded."""

    def record_limit_order_cancellation(
        self, order_id: int, trade_id: int
    ) -> None:
        """Cancel a stale/broker-rejected limit order and close its trade."""

    def list_trades_awaiting_gtt(self) -> list[TradeAwaitingGtt]:
        """List filled trades that still need a GTT exit order placed."""

    def create_gtt_order(
        self,
        trade_id: int,
        broker_order_id: str,
        quantity: int,
        target_price: float,
        stoploss_price: float,
    ) -> int:
        """Record a placed GTT exit order and return its order_id."""

    def list_pending_gtt_orders(self) -> list[PendingGttOrder]:
        """List GTT exit orders still awaiting a broker trigger."""

    def record_gtt_order_result(
        self,
        order_id: int,
        trade_id: int,
        exit_price: float,
        executed_at: datetime,
    ) -> None:
        """Record a triggered GTT's fill price and close its trade."""


class KiteOrderService(Protocol):
    def place_limit_order(
        self,
        access_token: str,
        *,
        exchange: str,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
        price: float,
        product: str,
    ) -> str:
        """Place a regular limit order and return its broker order_id."""

    def get_orders(self, access_token: str) -> list[KiteOrder]:
        """Return the current status of every order placed today."""

    def cancel_order(self, access_token: str, order_id: str) -> None:
        """Cancel a regular order by its broker order_id."""

    def place_gtt(
        self,
        access_token: str,
        *,
        exchange: str,
        tradingsymbol: str,
        quantity: int,
        last_price: float,
        target_price: float,
        stoploss_price: float,
        product: str,
    ) -> str:
        """Place a two-leg GTT and return its trigger_id."""

    def get_gtts(self, access_token: str) -> list[KiteGtt]:
        """Return the current status of every GTT trigger."""


class HourlyCandleClient(Protocol):
    def get_hourly_candles(
        self,
        access_token: str,
        instrument_key: str,
        from_date: date,
        through_date: date,
    ) -> list[DailyCandle]:
        """Return hourly candles plus the current, still-forming hour."""


class UpstoxAccessTokenStore(Protocol):
    def load(self) -> UpstoxTokenState:
        """Load the current Upstox token state."""


class ZerodhaAccessTokenStore(Protocol):
    def load(self) -> ZerodhaTokenState:
        """Load the current Zerodha token state."""


@dataclass(frozen=True, slots=True)
class CycleResult:
    ok: bool
    message: str
    entries_placed: int = 0
    entries_failed: int = 0
    limits_filled: int = 0
    limits_expired: int = 0
    gtts_placed: int = 0
    gtts_failed: int = 0
    exits_completed: int = 0
    exits_failed: int = 0


class TradeExecutionService:
    """Run one entry/exit execution cycle against Zerodha and Upstox."""

    def __init__(
        self,
        settings: TradeExecutionSettings,
        repository: TradeRepository,
        kite_client: KiteOrderService,
        candle_client: HourlyCandleClient,
        zerodha_token_store: ZerodhaAccessTokenStore,
        upstox_token_store: UpstoxAccessTokenStore,
    ):
        self.settings = settings
        self.repository = repository
        self.kite_client = kite_client
        self.candle_client = candle_client
        self.zerodha_token_store = zerodha_token_store
        self.upstox_token_store = upstox_token_store
        self._lock = Lock()

    def run_cycle_message(self) -> str:
        return self.run_cycle().message

    def run_cycle(self, *, now: datetime | None = None) -> CycleResult:
        current = now or datetime.now(UTC)
        if not self.settings.enabled:
            return CycleResult(False, "Trade execution is disabled.")

        with self._lock:
            local_time = current.astimezone(
                ZoneInfo(self.settings.timezone_name)
            ).time()
            entries_placed = entries_failed = 0
            if (
                self.settings.entry_window_start
                <= local_time
                <= self.settings.entry_window_end
            ):
                entries_placed, entries_failed = self._run_entry_scan(current)
            limits_filled, limits_expired = self._poll_limit_orders(current)
            gtts_placed, gtts_failed = self._place_pending_gtts(current)
            exits_completed, exits_failed = self._poll_gtt_orders(current)

        failed_total = entries_failed + gtts_failed + exits_failed
        prefix = ":warning:" if failed_total else ":white_check_mark:"
        message = (
            f"{prefix} Trade execution cycle completed. Entries placed: "
            f"{entries_placed:,} (failed: {entries_failed:,}); limit "
            f"fills: {limits_filled:,}; expired: {limits_expired:,}; GTTs "
            f"placed: {gtts_placed:,} (failed: {gtts_failed:,}); exits "
            f"recorded: {exits_completed:,} (failed: {exits_failed:,})."
        )
        return CycleResult(
            True,
            message,
            entries_placed=entries_placed,
            entries_failed=entries_failed,
            limits_filled=limits_filled,
            limits_expired=limits_expired,
            gtts_placed=gtts_placed,
            gtts_failed=gtts_failed,
            exits_completed=exits_completed,
            exits_failed=exits_failed,
        )

    def _run_entry_scan(self, current: datetime) -> tuple[int, int]:
        placed = failed = 0
        zerodha_token = self._zerodha_access_token()
        upstox_token = self._upstox_access_token(current)
        if zerodha_token is None or upstox_token is None:
            return placed, failed

        try:
            candidates = self.repository.list_trade_entry_candidates(
                self.settings.minimum_amount_allocated
            )
        except RepositoryError as error:
            LOGGER.warning("Trade entry candidate lookup failed: %s", error)
            return placed, failed

        for candidate in candidates:
            if not candidate.instrument_key:
                failed += 1
                LOGGER.warning(
                    "Skipping trade entry without instrument key "
                    "trading_symbol=%r",
                    candidate.trading_symbol,
                )
                continue
            try:
                close_price = self._latest_close(
                    upstox_token, candidate.instrument_key, current
                )
                price = _round_entry_price(
                    close_price, self.settings.price_rounding_increment
                )
                quantity = (
                    floor(candidate.amount_allocated / price)
                    if price > 0
                    else 0
                )
                if quantity <= 0:
                    raise TradeExecutionError(
                        "amount_allocated is too small for one share at "
                        "the rounded price"
                    )
                broker_order_id = self.kite_client.place_limit_order(
                    zerodha_token,
                    exchange=EXCHANGE,
                    tradingsymbol=candidate.trading_symbol,
                    transaction_type=TRANSACTION_TYPE_BUY,
                    quantity=quantity,
                    price=price,
                    product=self.settings.product,
                )
                self.repository.create_trade_and_limit_order(
                    asset_id=candidate.asset_id,
                    tracker_details_id=candidate.tracker_details_id,
                    asset_name=candidate.asset_name,
                    trading_symbol=candidate.trading_symbol,
                    instrument_key=candidate.instrument_key,
                    allocated_amount=candidate.amount_allocated,
                    quantity=quantity,
                    price=price,
                    broker_order_id=broker_order_id,
                )
            except (
                UpstoxAPIError,
                KiteAPIError,
                RepositoryError,
                TradeExecutionError,
            ) as error:
                failed += 1
                LOGGER.warning(
                    "Trade entry failed trading_symbol=%r: %s",
                    candidate.trading_symbol,
                    error,
                )
                continue
            placed += 1
            LOGGER.info(
                "Placed Zerodha limit entry trading_symbol=%r price=%.2f "
                "quantity=%d broker_order_id=%s",
                candidate.trading_symbol,
                price,
                quantity,
                broker_order_id,
            )
        return placed, failed

    def _poll_limit_orders(self, current: datetime) -> tuple[int, int]:
        filled = expired = 0
        zerodha_token = self._zerodha_access_token()
        if zerodha_token is None:
            return filled, expired

        try:
            pending = self.repository.list_pending_limit_orders()
        except RepositoryError as error:
            LOGGER.warning("Pending limit order lookup failed: %s", error)
            return filled, expired
        if not pending:
            return filled, expired

        try:
            fetched_orders = self.kite_client.get_orders(zerodha_token)
        except KiteAPIError as error:
            LOGGER.warning("Kite order list fetch failed: %s", error)
            return filled, expired
        LOGGER.info(
            "Fetched Kite order list count=%d pending=%d",
            len(fetched_orders),
            len(pending),
        )
        broker_orders = {order.order_id: order for order in fetched_orders}

        local_time = current.astimezone(
            ZoneInfo(self.settings.timezone_name)
        ).time()
        past_entry_window = local_time > self.settings.entry_window_end

        for order in pending:
            broker_order = broker_orders.get(order.broker_order_id)
            if broker_order is None:
                continue

            if broker_order.status == "COMPLETE":
                try:
                    self.repository.record_limit_order_fill(
                        order.order_id, order.tracker_details_id, current
                    )
                except RepositoryError as error:
                    LOGGER.warning(
                        "Failed to record limit fill trading_symbol=%r: %s",
                        order.trading_symbol,
                        error,
                    )
                    continue
                filled += 1
                LOGGER.info(
                    "Zerodha limit order filled trading_symbol=%r "
                    "order_id=%d",
                    order.trading_symbol,
                    order.order_id,
                )
                continue

            if broker_order.status in {"CANCELLED", "REJECTED"}:
                try:
                    self.repository.record_limit_order_cancellation(
                        order.order_id, order.trade_id
                    )
                except RepositoryError as error:
                    LOGGER.warning(
                        "Failed to record limit order outcome trading_"
                        "symbol=%r: %s",
                        order.trading_symbol,
                        error,
                    )
                    continue
                expired += 1
                continue

            if past_entry_window:
                try:
                    self.kite_client.cancel_order(
                        zerodha_token, order.broker_order_id
                    )
                    self.repository.record_limit_order_cancellation(
                        order.order_id, order.trade_id
                    )
                except (KiteAPIError, RepositoryError) as error:
                    LOGGER.warning(
                        "Failed to cancel stale limit order trading_"
                        "symbol=%r: %s",
                        order.trading_symbol,
                        error,
                    )
                    continue
                expired += 1
                LOGGER.info(
                    "Cancelled stale Zerodha limit order trading_symbol=%r "
                    "order_id=%d",
                    order.trading_symbol,
                    order.order_id,
                )
        return filled, expired

    def _place_pending_gtts(self, current: datetime) -> tuple[int, int]:
        placed = failed = 0
        zerodha_token = self._zerodha_access_token()
        upstox_token = self._upstox_access_token(current)
        if zerodha_token is None or upstox_token is None:
            return placed, failed

        try:
            awaiting = self.repository.list_trades_awaiting_gtt()
        except RepositoryError as error:
            LOGGER.warning("Trades-awaiting-GTT lookup failed: %s", error)
            return placed, failed

        for trade in awaiting:
            if not trade.instrument_key:
                failed += 1
                LOGGER.warning(
                    "Skipping GTT placement without instrument key "
                    "trading_symbol=%r",
                    trade.trading_symbol,
                )
                continue
            try:
                close_price, atr = self._atr(
                    upstox_token, trade.instrument_key, current
                )
                target_price = _round_to_tick_size(
                    close_price + self.settings.target_atr_multiple * atr
                )
                stoploss_price = _round_to_tick_size(
                    close_price - self.settings.stoploss_atr_multiple * atr
                )
                broker_order_id = self.kite_client.place_gtt(
                    zerodha_token,
                    exchange=EXCHANGE,
                    tradingsymbol=trade.trading_symbol,
                    quantity=trade.quantity,
                    last_price=close_price,
                    target_price=target_price,
                    stoploss_price=stoploss_price,
                    product=self.settings.product,
                )
                self.repository.create_gtt_order(
                    trade.trade_id,
                    broker_order_id,
                    trade.quantity,
                    target_price,
                    stoploss_price,
                )
            except (
                UpstoxAPIError,
                KiteAPIError,
                RepositoryError,
                TradeExecutionError,
            ) as error:
                failed += 1
                LOGGER.warning(
                    "GTT placement failed trading_symbol=%r: %s",
                    trade.trading_symbol,
                    error,
                )
                continue
            placed += 1
            LOGGER.info(
                "Placed Zerodha GTT trading_symbol=%r target=%.2f "
                "stoploss=%.2f",
                trade.trading_symbol,
                target_price,
                stoploss_price,
            )
        return placed, failed

    def _poll_gtt_orders(self, current: datetime) -> tuple[int, int]:
        completed = failed = 0
        zerodha_token = self._zerodha_access_token()
        if zerodha_token is None:
            return completed, failed

        try:
            pending = self.repository.list_pending_gtt_orders()
        except RepositoryError as error:
            LOGGER.warning("Pending GTT lookup failed: %s", error)
            return completed, failed
        if not pending:
            return completed, failed

        try:
            fetched_gtts = self.kite_client.get_gtts(zerodha_token)
        except KiteAPIError as error:
            LOGGER.warning("Kite GTT list fetch failed: %s", error)
            return completed, failed
        try:
            fetched_orders = self.kite_client.get_orders(zerodha_token)
        except KiteAPIError as error:
            LOGGER.warning("Kite order list fetch failed: %s", error)
            return completed, failed
        LOGGER.info(
            "Fetched Kite GTT/order lists gtt_count=%d order_count=%d "
            "pending=%d",
            len(fetched_gtts),
            len(fetched_orders),
            len(pending),
        )
        gtts = {gtt.trigger_id: gtt for gtt in fetched_gtts}
        orders = {order.order_id: order for order in fetched_orders}

        for pending_order in pending:
            gtt = gtts.get(pending_order.broker_order_id)
            if gtt is None or gtt.status != "triggered":
                continue
            leg_order_id = gtt.target_order_id or gtt.stoploss_order_id
            filled_order = orders.get(leg_order_id) if leg_order_id else None
            if filled_order is None or filled_order.status != "COMPLETE":
                continue

            exit_price = filled_order.average_price or (
                pending_order.target_price
                if leg_order_id == gtt.target_order_id
                else pending_order.stoploss_price
            )
            try:
                self.repository.record_gtt_order_result(
                    pending_order.order_id,
                    pending_order.trade_id,
                    exit_price,
                    current,
                )
            except RepositoryError as error:
                LOGGER.warning("Failed to record GTT exit: %s", error)
                failed += 1
                continue
            completed += 1
            LOGGER.info(
                "Zerodha GTT exit recorded trade_id=%d exit_price=%.2f",
                pending_order.trade_id,
                exit_price,
            )
        return completed, failed

    def _fetch_hourly_candles(
        self, access_token: str, instrument_key: str, current: datetime
    ) -> list[DailyCandle]:
        local_date = current.astimezone(
            ZoneInfo(self.settings.timezone_name)
        ).date()
        from_date = local_date - timedelta(
            days=self.settings.hourly_lookback_days - 1
        )
        return self.candle_client.get_hourly_candles(
            access_token, instrument_key, from_date, local_date
        )

    def _latest_close(
        self, access_token: str, instrument_key: str, current: datetime
    ) -> float:
        candles = self._fetch_hourly_candles(
            access_token, instrument_key, current
        )
        if not candles:
            raise TradeExecutionError("No hourly candles were returned")
        return float(candles[-1].close)

    def _atr(
        self, access_token: str, instrument_key: str, current: datetime
    ) -> tuple[float, float]:
        """Return (latest hourly close, ATR) using the same Wilder's
        smoothing already used for ADX(8) in tracker.evaluator.

        ``_wilder_smoothed`` returns a running (unnormalized) sum rather
        than a per-bar average, so the final ATR value is that sum divided
        by the period.
        """
        candles = self._fetch_hourly_candles(
            access_token, instrument_key, current
        )
        highs = [float(candle.high) for candle in candles]
        lows = [float(candle.low) for candle in candles]
        closes = [float(candle.close) for candle in candles]
        period = self.settings.atr_period
        if len(closes) < period + 1:
            raise TradeExecutionError(
                f"At least {period + 1} hourly candles are required for "
                f"ATR({period})"
            )
        true_ranges = _true_range(highs, lows, closes)
        smoothed = _wilder_smoothed(true_ranges, period)
        atr = smoothed[-1] / period
        return closes[-1], atr

    def _zerodha_access_token(self) -> str | None:
        try:
            state = self.zerodha_token_store.load()
        except ZerodhaTokenStateError as error:
            LOGGER.warning("Zerodha token state unreadable: %s", error)
            return None
        if not state.is_valid():
            LOGGER.warning(
                "Skipping trade execution: Zerodha token is not valid "
                "(status=%r)",
                state.validation_status,
            )
            return None
        return state.access_token

    def _upstox_access_token(self, current: datetime) -> str | None:
        try:
            state = self.upstox_token_store.load()
        except UpstoxTokenStateError as error:
            LOGGER.warning("Upstox token state unreadable: %s", error)
            return None
        if not state.is_valid(current):
            LOGGER.warning(
                "Skipping trade execution: Upstox token is not valid "
                "(status=%r)",
                state.validation_status,
            )
            return None
        return state.access_token


def _round_entry_price(close: float, increment: float) -> float:
    """Round a buy entry down to the nearest price increment (e.g. 347 ->
    345, 344 -> 340 for the default increment of 5)."""
    if increment <= 0:
        return close
    return floor(close / increment) * increment


def _round_to_tick_size(
    price: float, tick_size: float = NSE_TICK_SIZE
) -> float:
    """Round a GTT target/stoploss price to the nearest exchange tick size
    so Kite does not reject it with InputException."""
    return round(round(price / tick_size) * tick_size, 2)
