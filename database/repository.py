"""Database operations for saved assets and tracker membership."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import psycopg

from database.config import DatabaseSettings
from upstox.assets import AssetSearchResult

LOGGER = logging.getLogger(__name__)


class RepositoryError(RuntimeError):
    """Raised when an asset or tracker database operation fails."""


class AssetAlreadyExistsError(RepositoryError):
    """Raised when an asset trading symbol has already been saved."""


class AssetInUseError(RepositoryError):
    """Raised when a tracked asset cannot be deleted."""


class AssetNotFoundError(RepositoryError):
    """Raised when a trading symbol is not in the saved asset table."""


class TrackerAlreadyExistsError(RepositoryError):
    """Raised when an asset is already present in the tracker."""


class TrackerNotFoundError(RepositoryError):
    """Raised when an asset is not present in the tracker."""


@dataclass(frozen=True, slots=True)
class AssetRecord:
    asset_id: int
    asset_name: str
    trading_symbol: str
    instrument_key: str | None
    has_fno: bool = False


@dataclass(frozen=True, slots=True)
class TrackerEntry:
    tracker_details_id: int
    asset_id: int
    asset_name: str
    trading_symbol: str
    has_momentum: bool
    is_trade_created: bool
    is_approved_for_trade: bool
    amount_allocated: float
    added_date: date
    has_fno: bool = False
    side: str | None = None


@dataclass(frozen=True, slots=True)
class MomentumCandidate:
    """A saved asset eligible for insertion or pending-trade reevaluation."""

    asset_id: int
    asset_name: str
    trading_symbol: str
    instrument_key: str | None
    tracker_details_id: int | None


@dataclass(frozen=True, slots=True)
class TradeEntryCandidate:
    """A buy-side tracker entry ready for a new limit entry order."""

    tracker_details_id: int
    asset_id: int
    asset_name: str
    trading_symbol: str
    instrument_key: str | None
    amount_allocated: float


@dataclass(frozen=True, slots=True)
class TradeOrderIds:
    trade_id: int
    order_id: int


@dataclass(frozen=True, slots=True)
class PendingLimitOrder:
    order_id: int
    trade_id: int
    tracker_details_id: int
    trading_symbol: str
    broker_order_id: str


@dataclass(frozen=True, slots=True)
class TradeAwaitingGtt:
    """A filled trade that still needs its GTT exit order placed."""

    trade_id: int
    trading_symbol: str
    instrument_key: str | None
    quantity: int


@dataclass(frozen=True, slots=True)
class PendingGttOrder:
    order_id: int
    trade_id: int
    broker_order_id: str
    target_price: float
    stoploss_price: float


Connect = Callable[..., Any]


class AssetTrackerRepository:
    """Persist assets and their tracker membership in PostgreSQL."""

    def __init__(
        self,
        settings: DatabaseSettings,
        connect: Connect = psycopg.connect,
    ):
        self._settings = settings
        self._connect = connect

    def add_asset(
        self, asset: AssetSearchResult, has_fno: bool = False
    ) -> AssetRecord:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    INSERT INTO public.assets (
                        asset_name,
                        trading_symbol,
                        instrument_key,
                        has_fno
                    )
                    VALUES (%s, %s, %s, %s)
                    RETURNING
                        asset_id,
                        asset_name,
                        trading_symbol,
                        instrument_key,
                        has_fno
                    """,
                    (
                        asset.name,
                        asset.trading_symbol,
                        asset.instrument_key or None,
                        has_fno,
                    ),
                ).fetchone()
        except psycopg.errors.UniqueViolation as error:
            raise AssetAlreadyExistsError from error
        except psycopg.Error as error:
            LOGGER.exception(
                "Failed to add asset trading_symbol=%r",
                asset.trading_symbol,
            )
            raise RepositoryError("Unable to add the asset.") from error

        if row is None:
            LOGGER.error(
                "Asset insert returned no row trading_symbol=%r",
                asset.trading_symbol,
            )
            raise RepositoryError("Unable to add the asset.")
        return _asset_record(row)

    def delete_asset(self, trading_symbol: str) -> AssetRecord:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    DELETE FROM public.assets
                    WHERE upper(trading_symbol) = upper(%s)
                    RETURNING
                        asset_id,
                        asset_name,
                        trading_symbol,
                        instrument_key,
                        has_fno
                    """,
                    (trading_symbol,),
                ).fetchone()
        except psycopg.errors.ForeignKeyViolation as error:
            raise AssetInUseError from error
        except psycopg.Error as error:
            LOGGER.exception(
                "Failed to delete asset trading_symbol=%r",
                trading_symbol,
            )
            raise RepositoryError("Unable to delete the asset.") from error

        if row is None:
            raise AssetNotFoundError
        return _asset_record(row)

    def list_assets(self) -> list[AssetRecord]:
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT
                        asset_id,
                        asset_name,
                        trading_symbol,
                        instrument_key,
                        has_fno
                    FROM public.assets
                    ORDER BY trading_symbol, asset_id
                    """
                ).fetchall()
        except psycopg.Error as error:
            LOGGER.exception("Failed to list assets")
            raise RepositoryError("Unable to list assets.") from error
        return [_asset_record(row) for row in rows]

    def add_tracker(self, trading_symbol: str) -> TrackerEntry:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    INSERT INTO public.tracker (asset_id, added_date, has_fno)
                    SELECT asset_id, CURRENT_DATE, has_fno
                    FROM public.assets
                    WHERE upper(trading_symbol) = upper(%s)
                    ON CONFLICT (asset_id) DO NOTHING
                    RETURNING
                        tracker_details_id,
                        asset_id,
                        has_momentum,
                        is_trade_created,
                        is_approved_for_trade,
                        amount_allocated,
                        added_date,
                        has_fno,
                        side
                    """,
                    (trading_symbol,),
                ).fetchone()
                if row is None:
                    asset_exists = connection.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM public.assets
                            WHERE upper(trading_symbol) = upper(%s)
                        )
                        """,
                        (trading_symbol,),
                    ).fetchone()
                    if asset_exists and asset_exists[0]:
                        raise TrackerAlreadyExistsError
                    raise AssetNotFoundError

                asset_row = connection.execute(
                    """
                    SELECT asset_name, trading_symbol
                    FROM public.assets
                    WHERE asset_id = %s
                    """,
                    (row[1],),
                ).fetchone()
        except (AssetNotFoundError, TrackerAlreadyExistsError):
            raise
        except psycopg.Error as error:
            LOGGER.exception(
                "Failed to add tracker entry trading_symbol=%r",
                trading_symbol,
            )
            raise RepositoryError("Unable to add the tracker entry.") from error

        if asset_row is None:
            LOGGER.error(
                "Tracker insert returned an asset without a matching row "
                "trading_symbol=%r asset_id=%r",
                trading_symbol,
                row[1],
            )
            raise RepositoryError("Unable to add the tracker entry.")
        return TrackerEntry(
            tracker_details_id=int(row[0]),
            asset_id=int(row[1]),
            asset_name=str(asset_row[0]),
            trading_symbol=str(asset_row[1]),
            has_momentum=bool(row[2]),
            is_trade_created=bool(row[3]),
            is_approved_for_trade=bool(row[4]),
            amount_allocated=float(row[5]),
            added_date=row[6],
            has_fno=bool(row[7]),
            side=None if row[8] is None else str(row[8]),
        )

    def delete_tracker(self, trading_symbol: str) -> TrackerEntry:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    DELETE FROM public.tracker AS tracker
                    USING public.assets AS asset
                    WHERE tracker.asset_id = asset.asset_id
                      AND upper(asset.trading_symbol) = upper(%s)
                    RETURNING
                        tracker.tracker_details_id,
                        tracker.asset_id,
                        asset.asset_name,
                        asset.trading_symbol,
                        tracker.has_momentum,
                        tracker.is_trade_created,
                        tracker.is_approved_for_trade,
                        tracker.amount_allocated,
                        tracker.added_date,
                        tracker.has_fno,
                        tracker.side
                    """,
                    (trading_symbol,),
                ).fetchone()
        except psycopg.Error as error:
            LOGGER.exception(
                "Failed to delete tracker entry trading_symbol=%r",
                trading_symbol,
            )
            raise RepositoryError(
                "Unable to delete the tracker entry."
            ) from error

        if row is None:
            raise TrackerNotFoundError
        return _tracker_entry(row)

    def list_tracker(self) -> list[TrackerEntry]:
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT
                        tracker.tracker_details_id,
                        tracker.asset_id,
                        asset.asset_name,
                        asset.trading_symbol,
                        tracker.has_momentum,
                        tracker.is_trade_created,
                        tracker.is_approved_for_trade,
                        tracker.amount_allocated,
                        tracker.added_date,
                        tracker.has_fno,
                        tracker.side
                    FROM public.tracker AS tracker
                    JOIN public.assets AS asset
                      ON asset.asset_id = tracker.asset_id
                    ORDER BY tracker.added_date DESC, asset.trading_symbol
                    """
                ).fetchall()
        except psycopg.Error as error:
            LOGGER.exception("Failed to list tracker entries")
            raise RepositoryError("Unable to list tracker entries.") from error
        return [_tracker_entry(row) for row in rows]

    def update_tracker_trade_settings(
        self,
        trading_symbol: str,
        is_approved_for_trade: bool,
        amount_allocated: float,
    ) -> TrackerEntry:
        """Update only the admin-managed trade approval and allocation."""
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    UPDATE public.tracker AS tracker
                    SET
                        is_approved_for_trade = %s,
                        amount_allocated = %s
                    FROM public.assets AS asset
                    WHERE tracker.asset_id = asset.asset_id
                      AND upper(asset.trading_symbol) = upper(%s)
                    RETURNING
                        tracker.tracker_details_id,
                        tracker.asset_id,
                        asset.asset_name,
                        asset.trading_symbol,
                        tracker.has_momentum,
                        tracker.is_trade_created,
                        tracker.is_approved_for_trade,
                        tracker.amount_allocated,
                        tracker.added_date,
                        tracker.has_fno,
                        tracker.side
                    """,
                    (
                        is_approved_for_trade,
                        amount_allocated,
                        trading_symbol,
                    ),
                ).fetchone()
        except psycopg.Error as error:
            LOGGER.exception(
                "Failed to update tracker trade settings trading_symbol=%r",
                trading_symbol,
            )
            raise RepositoryError(
                "Unable to update the tracker entry."
            ) from error

        if row is None:
            raise TrackerNotFoundError
        return _tracker_entry(row)

    def list_momentum_candidates(self) -> list[MomentumCandidate]:
        """List untracked assets and tracked assets without a created trade."""
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT
                        asset.asset_id,
                        asset.asset_name,
                        asset.trading_symbol,
                        asset.instrument_key,
                        tracker.tracker_details_id
                    FROM public.assets AS asset
                    LEFT JOIN public.tracker AS tracker
                      ON tracker.asset_id = asset.asset_id
                    WHERE tracker.tracker_details_id IS NULL
                       OR tracker.is_trade_created = FALSE
                    ORDER BY asset.trading_symbol, asset.asset_id
                    """
                ).fetchall()
        except psycopg.Error as error:
            LOGGER.exception("Failed to list momentum evaluation candidates")
            raise RepositoryError(
                "Unable to list tracker evaluation assets."
            ) from error
        return [_momentum_candidate(row) for row in rows]

    def record_momentum_evaluation(
        self,
        asset_id: int,
        has_momentum: bool,
        evaluation_date: date,
        side: str | None = None,
    ) -> bool:
        """Apply one evaluation without changing trade-created tracker rows.

        Qualifying untracked assets are inserted. Qualifying pending entries
        are refreshed and reset to unapproved. Pending entries that no longer
        qualify have their momentum and approval flags cleared and their
        side reset to NULL regardless of the supplied ``side``.
        """
        try:
            with self._connection() as connection:
                if has_momentum:
                    row = connection.execute(
                        """
                        INSERT INTO public.tracker AS current_tracker (
                            asset_id,
                            has_momentum,
                            is_trade_created,
                            is_approved_for_trade,
                            added_date,
                            has_fno,
                            side
                        )
                        SELECT %s, TRUE, FALSE, FALSE, %s, has_fno, %s
                        FROM public.assets
                        WHERE asset_id = %s
                        ON CONFLICT (asset_id) DO UPDATE
                        SET
                            has_momentum = TRUE,
                            is_trade_created = FALSE,
                            is_approved_for_trade = FALSE,
                            added_date = EXCLUDED.added_date,
                            has_fno = EXCLUDED.has_fno,
                            side = EXCLUDED.side
                        WHERE current_tracker.is_trade_created = FALSE
                        RETURNING tracker_details_id
                        """,
                        (asset_id, evaluation_date, side, asset_id),
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        UPDATE public.tracker
                        SET
                            has_momentum = FALSE,
                            is_approved_for_trade = FALSE,
                            side = NULL
                        WHERE asset_id = %s
                          AND is_trade_created = FALSE
                        RETURNING tracker_details_id
                        """,
                        (asset_id,),
                    ).fetchone()
        except psycopg.Error as error:
            LOGGER.exception(
                "Failed to record momentum evaluation asset_id=%r",
                asset_id,
            )
            raise RepositoryError(
                "Unable to update tracker momentum."
            ) from error
        return row is not None

    def find_asset_by_trading_symbol(
        self, trading_symbol: str
    ) -> AssetRecord | None:
        """Return one saved asset by trading symbol, or None if unsaved."""
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT
                        asset_id,
                        asset_name,
                        trading_symbol,
                        instrument_key,
                        has_fno
                    FROM public.assets
                    WHERE upper(trading_symbol) = upper(%s)
                    """,
                    (trading_symbol,),
                ).fetchone()
        except psycopg.Error as error:
            LOGGER.exception(
                "Failed to look up asset trading_symbol=%r",
                trading_symbol,
            )
            raise RepositoryError("Unable to look up the asset.") from error
        return None if row is None else _asset_record(row)

    def list_tracker_assets(self) -> list[MomentumCandidate]:
        """List every currently tracked asset with its instrument key."""
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT
                        asset.asset_id,
                        asset.asset_name,
                        asset.trading_symbol,
                        asset.instrument_key,
                        tracker.tracker_details_id
                    FROM public.tracker AS tracker
                    JOIN public.assets AS asset
                      ON asset.asset_id = tracker.asset_id
                    ORDER BY asset.trading_symbol, asset.asset_id
                    """
                ).fetchall()
        except psycopg.Error as error:
            LOGGER.exception("Failed to list tracker assets")
            raise RepositoryError(
                "Unable to list tracker assets."
            ) from error
        return [_momentum_candidate(row) for row in rows]

    def list_trade_entry_candidates(
        self, minimum_amount_allocated: float
    ) -> list[TradeEntryCandidate]:
        """List buy-side tracker entries ready for a new limit entry order.

        Excludes any tracker entry that already has an open trade, since
        ``is_trade_created`` only flips to ``TRUE`` once that trade's limit
        order fills (not when it's merely placed).
        """
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT
                        tracker.tracker_details_id,
                        asset.asset_id,
                        asset.asset_name,
                        asset.trading_symbol,
                        asset.instrument_key,
                        tracker.amount_allocated
                    FROM public.tracker AS tracker
                    JOIN public.assets AS asset
                      ON asset.asset_id = tracker.asset_id
                    LEFT JOIN public.trade AS trade
                      ON trade.tracker_details_id = tracker.tracker_details_id
                     AND trade.status = 'open'
                    WHERE tracker.is_trade_created = FALSE
                      AND tracker.is_approved_for_trade = TRUE
                      AND tracker.amount_allocated >= %s
                      AND tracker.side = 'buy'
                      AND trade.trade_id IS NULL
                    ORDER BY asset.trading_symbol, tracker.tracker_details_id
                    """,
                    (minimum_amount_allocated,),
                ).fetchall()
        except psycopg.Error as error:
            LOGGER.exception("Failed to list trade entry candidates")
            raise RepositoryError(
                "Unable to list trade entry candidates."
            ) from error
        return [
            TradeEntryCandidate(
                tracker_details_id=int(row[0]),
                asset_id=int(row[1]),
                asset_name=str(row[2]),
                trading_symbol=str(row[3]),
                instrument_key=None if row[4] is None else str(row[4]),
                amount_allocated=float(row[5]),
            )
            for row in rows
        ]

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
        try:
            with self._connection() as connection:
                trade_row = connection.execute(
                    """
                    INSERT INTO public.trade (
                        asset_id,
                        tracker_details_id,
                        asset_name,
                        trading_symbol,
                        instrument_key,
                        side,
                        allocated_amount,
                        is_future_trade,
                        status
                    )
                    VALUES (%s, %s, %s, %s, %s, 'buy', %s, FALSE, 'open')
                    RETURNING trade_id
                    """,
                    (
                        asset_id,
                        tracker_details_id,
                        asset_name,
                        trading_symbol,
                        instrument_key,
                        allocated_amount,
                    ),
                ).fetchone()
                if trade_row is None:
                    raise RepositoryError("Unable to open the trade.")
                trade_id = int(trade_row[0])

                order_row = connection.execute(
                    """
                    INSERT INTO public.trade_order (
                        trade_id,
                        broker_order_id,
                        order_type,
                        transaction_type,
                        quantity,
                        price,
                        status
                    )
                    VALUES (%s, %s, 'limit', 'buy', %s, %s, 'pending')
                    RETURNING order_id
                    """,
                    (trade_id, broker_order_id, quantity, price),
                ).fetchone()
        except RepositoryError:
            raise
        except psycopg.Error as error:
            LOGGER.exception(
                "Failed to open trade trading_symbol=%r", trading_symbol
            )
            raise RepositoryError("Unable to open the trade.") from error

        if order_row is None:
            LOGGER.error(
                "Trade limit order insert returned no row trade_id=%r",
                trade_id,
            )
            raise RepositoryError("Unable to open the trade.")
        return TradeOrderIds(trade_id=trade_id, order_id=int(order_row[0]))

    def list_pending_limit_orders(self) -> list[PendingLimitOrder]:
        """List limit entry orders still awaiting a broker fill."""
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT
                        trade_order.order_id,
                        trade_order.trade_id,
                        trade.tracker_details_id,
                        trade.trading_symbol,
                        trade_order.broker_order_id
                    FROM public.trade_order AS trade_order
                    JOIN public.trade AS trade
                      ON trade.trade_id = trade_order.trade_id
                    WHERE trade_order.order_type = 'limit'
                      AND trade_order.status = 'pending'
                      AND trade_order.executed_at IS NULL
                      AND trade_order.broker_order_id IS NOT NULL
                    ORDER BY trade_order.placed_at
                    """
                ).fetchall()
        except psycopg.Error as error:
            LOGGER.exception("Failed to list pending limit orders")
            raise RepositoryError(
                "Unable to list pending limit orders."
            ) from error
        return [
            PendingLimitOrder(
                order_id=int(row[0]),
                trade_id=int(row[1]),
                tracker_details_id=int(row[2]),
                trading_symbol=str(row[3]),
                broker_order_id=str(row[4]),
            )
            for row in rows
        ]

    def record_limit_order_fill(
        self,
        order_id: int,
        tracker_details_id: int,
        executed_at: datetime,
    ) -> None:
        """Mark a limit order filled and flag its tracker entry as traded."""
        try:
            with self._connection() as connection:
                order_row = connection.execute(
                    """
                    UPDATE public.trade_order
                    SET status = 'complete', executed_at = %s
                    WHERE order_id = %s
                    RETURNING order_id
                    """,
                    (executed_at, order_id),
                ).fetchone()
                if order_row is None:
                    raise RepositoryError(
                        "Unable to record the limit order fill."
                    )
                connection.execute(
                    """
                    UPDATE public.tracker
                    SET is_trade_created = TRUE
                    WHERE tracker_details_id = %s
                    """,
                    (tracker_details_id,),
                )
        except RepositoryError:
            raise
        except psycopg.Error as error:
            LOGGER.exception(
                "Failed to record limit order fill order_id=%r", order_id
            )
            raise RepositoryError(
                "Unable to record the limit order fill."
            ) from error

    def record_limit_order_cancellation(
        self, order_id: int, trade_id: int, closed_at: datetime
    ) -> None:
        """Cancel a stale/broker-rejected limit order and close its trade."""
        try:
            with self._connection() as connection:
                order_row = connection.execute(
                    """
                    UPDATE public.trade_order
                    SET status = 'cancelled'
                    WHERE order_id = %s
                    RETURNING order_id
                    """,
                    (order_id,),
                ).fetchone()
                if order_row is None:
                    raise RepositoryError(
                        "Unable to record the limit order cancellation."
                    )
                connection.execute(
                    """
                    UPDATE public.trade
                    SET status = 'closed', closed_at = %s
                    WHERE trade_id = %s
                    """,
                    (closed_at, trade_id),
                )
        except RepositoryError:
            raise
        except psycopg.Error as error:
            LOGGER.exception(
                "Failed to record limit order cancellation order_id=%r",
                order_id,
            )
            raise RepositoryError(
                "Unable to record the limit order cancellation."
            ) from error

    def list_trades_awaiting_gtt(self) -> list[TradeAwaitingGtt]:
        """List filled trades that still need a GTT exit order placed."""
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT
                        trade.trade_id,
                        trade.trading_symbol,
                        trade.instrument_key,
                        trade_order.quantity
                    FROM public.trade AS trade
                    JOIN public.trade_order AS trade_order
                      ON trade_order.trade_id = trade.trade_id
                     AND trade_order.order_type = 'limit'
                     AND trade_order.status = 'complete'
                    WHERE trade.status = 'open'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.trade_order AS gtt_order
                          WHERE gtt_order.trade_id = trade.trade_id
                            AND gtt_order.order_type = 'gtt'
                      )
                    ORDER BY trade.trade_id
                    """
                ).fetchall()
        except psycopg.Error as error:
            LOGGER.exception("Failed to list trades awaiting a GTT order")
            raise RepositoryError(
                "Unable to list trades awaiting a GTT order."
            ) from error
        return [
            TradeAwaitingGtt(
                trade_id=int(row[0]),
                trading_symbol=str(row[1]),
                instrument_key=None if row[2] is None else str(row[2]),
                quantity=int(row[3]),
            )
            for row in rows
        ]

    def create_gtt_order(
        self,
        trade_id: int,
        broker_order_id: str,
        quantity: int,
        target_price: float,
        stoploss_price: float,
    ) -> int:
        """Record a placed GTT exit order and return its order_id."""
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    INSERT INTO public.trade_order (
                        trade_id,
                        broker_order_id,
                        order_type,
                        transaction_type,
                        quantity,
                        target_price,
                        stoploss_price,
                        status
                    )
                    VALUES (%s, %s, 'gtt', 'sell', %s, %s, %s, 'pending')
                    RETURNING order_id
                    """,
                    (
                        trade_id,
                        broker_order_id,
                        quantity,
                        target_price,
                        stoploss_price,
                    ),
                ).fetchone()
        except psycopg.Error as error:
            LOGGER.exception(
                "Failed to record GTT order trade_id=%r", trade_id
            )
            raise RepositoryError("Unable to record the GTT order.") from error

        if row is None:
            LOGGER.error(
                "GTT order insert returned no row trade_id=%r", trade_id
            )
            raise RepositoryError("Unable to record the GTT order.")
        return int(row[0])

    def list_pending_gtt_orders(self) -> list[PendingGttOrder]:
        """List GTT exit orders still awaiting a broker trigger."""
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT
                        order_id,
                        trade_id,
                        broker_order_id,
                        target_price,
                        stoploss_price
                    FROM public.trade_order
                    WHERE order_type = 'gtt'
                      AND status = 'pending'
                      AND executed_at IS NULL
                      AND broker_order_id IS NOT NULL
                    ORDER BY placed_at
                    """
                ).fetchall()
        except psycopg.Error as error:
            LOGGER.exception("Failed to list pending GTT orders")
            raise RepositoryError(
                "Unable to list pending GTT orders."
            ) from error
        return [
            PendingGttOrder(
                order_id=int(row[0]),
                trade_id=int(row[1]),
                broker_order_id=str(row[2]),
                target_price=float(row[3]),
                stoploss_price=float(row[4]),
            )
            for row in rows
        ]

    def record_gtt_order_result(
        self,
        order_id: int,
        trade_id: int,
        exit_price: float,
        executed_at: datetime,
    ) -> None:
        """Record a triggered GTT's fill price and close its trade."""
        try:
            with self._connection() as connection:
                order_row = connection.execute(
                    """
                    UPDATE public.trade_order
                    SET status = 'complete',
                        executed_at = %s,
                        exit_price = %s
                    WHERE order_id = %s
                    RETURNING order_id
                    """,
                    (executed_at, exit_price, order_id),
                ).fetchone()
                if order_row is None:
                    raise RepositoryError(
                        "Unable to record the GTT order result."
                    )
                connection.execute(
                    """
                    UPDATE public.trade
                    SET status = 'closed', closed_at = %s
                    WHERE trade_id = %s
                    """,
                    (executed_at, trade_id),
                )
        except RepositoryError:
            raise
        except psycopg.Error as error:
            LOGGER.exception(
                "Failed to record GTT order result order_id=%r", order_id
            )
            raise RepositoryError(
                "Unable to record the GTT order result."
            ) from error

    def record_orders_checked(
        self, order_ids: Sequence[int], checked_at: datetime
    ) -> None:
        """Stamp when pending orders were last polled against the broker."""
        if not order_ids:
            return
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    UPDATE public.trade_order
                    SET last_checked_at = %s
                    WHERE order_id = ANY(%s)
                    """,
                    (checked_at, list(order_ids)),
                )
        except psycopg.Error as error:
            LOGGER.exception("Failed to record order check timestamps")
            raise RepositoryError(
                "Unable to record order check timestamps."
            ) from error

    def _connection(self) -> Any:
        return self._connect(
            self._settings.database_url,
            connect_timeout=self._settings.connect_timeout_seconds,
        )


def _asset_record(row: tuple[Any, ...]) -> AssetRecord:
    return AssetRecord(
        asset_id=int(row[0]),
        asset_name=str(row[1]),
        trading_symbol=str(row[2]),
        instrument_key=None if row[3] is None else str(row[3]),
        has_fno=bool(row[4]),
    )


def _tracker_entry(row: tuple[Any, ...]) -> TrackerEntry:
    return TrackerEntry(
        tracker_details_id=int(row[0]),
        asset_id=int(row[1]),
        asset_name=str(row[2]),
        trading_symbol=str(row[3]),
        has_momentum=bool(row[4]),
        is_trade_created=bool(row[5]),
        is_approved_for_trade=bool(row[6]),
        amount_allocated=float(row[7]),
        added_date=row[8],
        has_fno=bool(row[9]),
        side=None if row[10] is None else str(row[10]),
    )


def _momentum_candidate(row: tuple[Any, ...]) -> MomentumCandidate:
    return MomentumCandidate(
        asset_id=int(row[0]),
        asset_name=str(row[1]),
        trading_symbol=str(row[2]),
        instrument_key=None if row[3] is None else str(row[3]),
        tracker_details_id=None if row[4] is None else int(row[4]),
    )
