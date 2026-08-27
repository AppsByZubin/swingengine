from datetime import UTC, datetime, timedelta, timezone

from database.repository import (
    PendingGttOrder,
    PendingLimitOrder,
    TradeAwaitingGtt,
    TradeEntryCandidate,
    TradeOrderIds,
)
from tracker.evaluator import _true_range, _wilder_smoothed
from trade.config import TradeExecutionSettings
from trade.executor import TradeExecutionService
from upstox.client import DailyCandle
from upstox.store import TokenState as UpstoxTokenState
from zerodha.client import KiteGtt, KiteOrder
from zerodha.store import TokenState as ZerodhaTokenState

IST = timezone(timedelta(hours=5, minutes=30))
WITHIN_WINDOW = datetime(2026, 7, 27, 11, 0, tzinfo=IST)
BEFORE_WINDOW = datetime(2026, 7, 27, 9, 0, tzinfo=IST)
PAST_WINDOW = datetime(2026, 7, 27, 15, 30, tzinfo=IST)


class FakeRepository:
    def __init__(self) -> None:
        self.candidates: list[TradeEntryCandidate] = []
        self.created_trades: list[dict] = []
        self.pending_limit_orders: list[PendingLimitOrder] = []
        self.limit_fills: list[tuple] = []
        self.limit_cancellations: list[tuple] = []
        self.trades_awaiting_gtt: list[TradeAwaitingGtt] = []
        self.created_gtts: list[tuple] = []
        self.pending_gtt_orders: list[PendingGttOrder] = []
        self.gtt_results: list[tuple] = []
        self.list_candidates_calls = 0
        self._next_id = 1

    def list_trade_entry_candidates(self, minimum_amount_allocated):
        self.list_candidates_calls += 1
        return [
            c
            for c in self.candidates
            if c.amount_allocated >= minimum_amount_allocated
        ]

    def create_trade_and_limit_order(self, **kwargs):
        self.created_trades.append(kwargs)
        trade_id, order_id = self._next_id, self._next_id + 1
        self._next_id += 2
        return TradeOrderIds(trade_id=trade_id, order_id=order_id)

    def list_pending_limit_orders(self):
        return self.pending_limit_orders

    def record_limit_order_fill(self, order_id, tracker_details_id, executed_at):
        self.limit_fills.append((order_id, tracker_details_id, executed_at))

    def record_limit_order_cancellation(self, order_id, trade_id):
        self.limit_cancellations.append((order_id, trade_id))

    def list_trades_awaiting_gtt(self):
        return self.trades_awaiting_gtt

    def create_gtt_order(
        self, trade_id, broker_order_id, quantity, target_price, stoploss_price
    ):
        self.created_gtts.append(
            (trade_id, broker_order_id, quantity, target_price, stoploss_price)
        )
        order_id = self._next_id
        self._next_id += 1
        return order_id

    def list_pending_gtt_orders(self):
        return self.pending_gtt_orders

    def record_gtt_order_result(self, order_id, trade_id, exit_price, executed_at):
        self.gtt_results.append((order_id, trade_id, exit_price, executed_at))


class FakeKiteClient:
    def __init__(self) -> None:
        self.placed_orders: list[dict] = []
        self.cancelled_orders: list[str] = []
        self.placed_gtts: list[dict] = []
        self.orders_response: list[KiteOrder] = []
        self.gtts_response: list[KiteGtt] = []
        self._next_id = 100

    def place_limit_order(
        self,
        access_token,
        *,
        exchange,
        tradingsymbol,
        transaction_type,
        quantity,
        price,
        product,
    ):
        order_id = f"BROKER{self._next_id}"
        self._next_id += 1
        self.placed_orders.append(
            {
                "tradingsymbol": tradingsymbol,
                "transaction_type": transaction_type,
                "quantity": quantity,
                "price": price,
                "product": product,
            }
        )
        return order_id

    def get_orders(self, access_token):
        return self.orders_response

    def cancel_order(self, access_token, order_id):
        self.cancelled_orders.append(order_id)

    def place_gtt(
        self,
        access_token,
        *,
        exchange,
        tradingsymbol,
        quantity,
        last_price,
        target_price,
        stoploss_price,
        product,
    ):
        trigger_id = f"GTT{self._next_id}"
        self._next_id += 1
        self.placed_gtts.append(
            {
                "tradingsymbol": tradingsymbol,
                "quantity": quantity,
                "last_price": last_price,
                "target_price": target_price,
                "stoploss_price": stoploss_price,
            }
        )
        return trigger_id

    def get_gtts(self, access_token):
        return self.gtts_response


class FakeCandleClient:
    def __init__(self, candles: dict[str, list[DailyCandle]]) -> None:
        self.candles = candles

    def get_hourly_candles(self, access_token, instrument_key, from_date, through_date):
        return self.candles[instrument_key]


class FakeZerodhaTokenStore:
    def __init__(self, access_token: str = "zerodha-token", valid: bool = True):
        self._state = ZerodhaTokenState(
            access_token=access_token,
            validation_status="valid" if valid else "invalid",
        )

    def load(self):
        return self._state


class FakeUpstoxTokenStore:
    def __init__(self, access_token: str = "upstox-token", valid: bool = True):
        self._state = UpstoxTokenState(
            access_token=access_token,
            validation_status="valid" if valid else "invalid",
        )

    def load(self):
        return self._state


def build_service(
    repository: FakeRepository | None = None,
    kite_client: FakeKiteClient | None = None,
    candle_client: FakeCandleClient | None = None,
    zerodha_store: FakeZerodhaTokenStore | None = None,
    upstox_store: FakeUpstoxTokenStore | None = None,
    **settings_overrides,
) -> tuple[TradeExecutionService, FakeRepository, FakeKiteClient]:
    settings = TradeExecutionSettings.from_env(
        {"SWINGENGINE_TRADE_EXECUTION_ENABLED": "true", **settings_overrides}
    )
    repository = repository or FakeRepository()
    kite_client = kite_client or FakeKiteClient()
    service = TradeExecutionService(
        settings,
        repository,
        kite_client,
        candle_client or FakeCandleClient({}),
        zerodha_store or FakeZerodhaTokenStore(),
        upstox_store or FakeUpstoxTokenStore(),
    )
    return service, repository, kite_client


def hourly_candle(hour: int, high: float, low: float, close: float) -> DailyCandle:
    return DailyCandle(
        timestamp=datetime(2026, 7, 27, hour, tzinfo=UTC),
        open=close,
        high=high,
        low=low,
        close=close,
        volume=0.0,
        open_interest=0.0,
    )


def test_entry_scan_places_rounded_limit_order_and_persists_trade() -> None:
    repository = FakeRepository()
    repository.candidates = [
        TradeEntryCandidate(
            tracker_details_id=20,
            asset_id=7,
            asset_name="Sun Pharma",
            trading_symbol="SUNPHARMA",
            instrument_key="NSE_EQ|KEY1",
            amount_allocated=10_000.0,
        )
    ]
    candles = FakeCandleClient(
        {"NSE_EQ|KEY1": [hourly_candle(10, 348, 346, 347)]}
    )
    service, repository, kite = build_service(
        repository=repository, candle_client=candles
    )

    result = service.run_cycle(now=WITHIN_WINDOW)

    assert result.entries_placed == 1
    assert result.entries_failed == 0
    assert kite.placed_orders[0]["price"] == 345.0
    assert kite.placed_orders[0]["quantity"] == 28  # floor(10000 / 345)
    assert repository.created_trades[0]["price"] == 345.0
    assert repository.created_trades[0]["quantity"] == 28
    assert repository.created_trades[0]["broker_order_id"] == "BROKER100"


def test_entry_scan_skips_when_allocation_cannot_buy_one_share() -> None:
    repository = FakeRepository()
    repository.candidates = [
        TradeEntryCandidate(
            tracker_details_id=20,
            asset_id=7,
            asset_name="Sun Pharma",
            trading_symbol="SUNPHARMA",
            instrument_key="NSE_EQ|KEY1",
            amount_allocated=1_000.0,
        )
    ]
    candles = FakeCandleClient(
        {"NSE_EQ|KEY1": [hourly_candle(10, 3480, 3460, 3470)]}
    )
    service, repository, kite = build_service(
        repository=repository, candle_client=candles
    )

    result = service.run_cycle(now=WITHIN_WINDOW)

    assert result.entries_placed == 0
    assert result.entries_failed == 1
    assert kite.placed_orders == []
    assert repository.created_trades == []


def test_entry_scan_does_not_run_outside_the_entry_window() -> None:
    repository = FakeRepository()
    repository.candidates = [
        TradeEntryCandidate(
            tracker_details_id=20,
            asset_id=7,
            asset_name="Sun Pharma",
            trading_symbol="SUNPHARMA",
            instrument_key="NSE_EQ|KEY1",
            amount_allocated=10_000.0,
        )
    ]
    service, repository, kite = build_service(repository=repository)

    result = service.run_cycle(now=BEFORE_WINDOW)

    assert result.entries_placed == 0
    assert repository.list_candidates_calls == 0
    assert kite.placed_orders == []


def test_entry_scan_skips_when_zerodha_token_is_invalid() -> None:
    repository = FakeRepository()
    repository.candidates = [
        TradeEntryCandidate(
            tracker_details_id=20,
            asset_id=7,
            asset_name="Sun Pharma",
            trading_symbol="SUNPHARMA",
            instrument_key="NSE_EQ|KEY1",
            amount_allocated=10_000.0,
        )
    ]
    service, repository, kite = build_service(
        repository=repository,
        zerodha_store=FakeZerodhaTokenStore(valid=False),
    )

    result = service.run_cycle(now=WITHIN_WINDOW)

    assert result.entries_placed == 0
    assert repository.list_candidates_calls == 0


def test_limit_order_fill_is_recorded() -> None:
    repository = FakeRepository()
    repository.pending_limit_orders = [
        PendingLimitOrder(
            order_id=1,
            trade_id=10,
            tracker_details_id=20,
            trading_symbol="SUNPHARMA",
            broker_order_id="BROKER1",
        )
    ]
    kite = FakeKiteClient()
    kite.orders_response = [
        KiteOrder(
            order_id="BROKER1",
            status="COMPLETE",
            tradingsymbol="SUNPHARMA",
            transaction_type="BUY",
            quantity=5,
            filled_quantity=5,
            average_price=345.0,
        )
    ]
    service, repository, kite = build_service(repository=repository, kite_client=kite)

    result = service.run_cycle(now=WITHIN_WINDOW)

    assert result.limits_filled == 1
    assert repository.limit_fills == [(1, 20, WITHIN_WINDOW)]


def test_stale_limit_order_is_cancelled_past_the_entry_window() -> None:
    repository = FakeRepository()
    repository.pending_limit_orders = [
        PendingLimitOrder(
            order_id=1,
            trade_id=10,
            tracker_details_id=20,
            trading_symbol="SUNPHARMA",
            broker_order_id="BROKER1",
        )
    ]
    kite = FakeKiteClient()
    kite.orders_response = [
        KiteOrder(
            order_id="BROKER1",
            status="OPEN",
            tradingsymbol="SUNPHARMA",
            transaction_type="BUY",
            quantity=5,
            filled_quantity=0,
            average_price=0.0,
        )
    ]
    service, repository, kite = build_service(repository=repository, kite_client=kite)

    result = service.run_cycle(now=PAST_WINDOW)

    assert result.limits_expired == 1
    assert kite.cancelled_orders == ["BROKER1"]
    assert repository.limit_cancellations == [(1, 10)]


def test_stale_limit_order_is_left_open_within_the_entry_window() -> None:
    repository = FakeRepository()
    repository.pending_limit_orders = [
        PendingLimitOrder(
            order_id=1,
            trade_id=10,
            tracker_details_id=20,
            trading_symbol="SUNPHARMA",
            broker_order_id="BROKER1",
        )
    ]
    kite = FakeKiteClient()
    kite.orders_response = [
        KiteOrder(
            order_id="BROKER1",
            status="OPEN",
            tradingsymbol="SUNPHARMA",
            transaction_type="BUY",
            quantity=5,
            filled_quantity=0,
            average_price=0.0,
        )
    ]
    service, repository, kite = build_service(repository=repository, kite_client=kite)

    result = service.run_cycle(now=WITHIN_WINDOW)

    assert result.limits_expired == 0
    assert kite.cancelled_orders == []
    assert repository.limit_cancellations == []


def test_gtt_placement_uses_atr_of_hourly_candles() -> None:
    repository = FakeRepository()
    repository.trades_awaiting_gtt = [
        TradeAwaitingGtt(
            trade_id=10,
            trading_symbol="SUNPHARMA",
            instrument_key="NSE_EQ|KEY1",
            quantity=5,
        )
    ]
    raw_candles = [
        hourly_candle(9 + i, 350 + i, 345 + i, 348 + i) for i in range(9)
    ]
    candles = FakeCandleClient({"NSE_EQ|KEY1": raw_candles})
    service, repository, kite = build_service(
        repository=repository, candle_client=candles
    )

    result = service.run_cycle(now=WITHIN_WINDOW)

    highs = [c.high for c in raw_candles]
    lows = [c.low for c in raw_candles]
    closes = [c.close for c in raw_candles]
    expected_atr = _wilder_smoothed(_true_range(highs, lows, closes), 8)[-1] / 8
    expected_close = closes[-1]

    assert result.gtts_placed == 1
    placed = kite.placed_gtts[0]
    assert placed["target_price"] == expected_close + 3 * expected_atr
    assert placed["stoploss_price"] == expected_close - 2 * expected_atr
    assert repository.created_gtts[0][0] == 10


def test_gtt_exit_is_recorded_when_triggered() -> None:
    repository = FakeRepository()
    repository.pending_gtt_orders = [
        PendingGttOrder(
            order_id=5,
            trade_id=10,
            broker_order_id="TRIGGER1",
            target_price=360.0,
            stoploss_price=340.0,
        )
    ]
    kite = FakeKiteClient()
    kite.gtts_response = [
        KiteGtt(
            trigger_id="TRIGGER1",
            status="triggered",
            stoploss_order_id=None,
            target_order_id="ORDX",
        )
    ]
    kite.orders_response = [
        KiteOrder(
            order_id="ORDX",
            status="COMPLETE",
            tradingsymbol="SUNPHARMA",
            transaction_type="SELL",
            quantity=5,
            filled_quantity=5,
            average_price=361.5,
        )
    ]
    service, repository, kite = build_service(repository=repository, kite_client=kite)

    result = service.run_cycle(now=WITHIN_WINDOW)

    assert result.exits_completed == 1
    assert repository.gtt_results == [(5, 10, 361.5, WITHIN_WINDOW)]


def test_run_cycle_reports_disabled() -> None:
    settings = TradeExecutionSettings.from_env({})
    service = TradeExecutionService(
        settings,
        FakeRepository(),
        FakeKiteClient(),
        FakeCandleClient({}),
        FakeZerodhaTokenStore(),
        FakeUpstoxTokenStore(),
    )

    result = service.run_cycle(now=WITHIN_WINDOW)

    assert not result.ok
    assert "disabled" in result.message
