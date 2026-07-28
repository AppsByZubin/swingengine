"""Database operations for saved assets and tracker membership."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
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


@dataclass(frozen=True, slots=True)
class TrackerEntry:
    tracker_details_id: int
    asset_id: int
    asset_name: str
    trading_symbol: str
    added_date: date


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

    def add_asset(self, asset: AssetSearchResult) -> AssetRecord:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    INSERT INTO public.assets (
                        asset_name,
                        trading_symbol,
                        instrument_key
                    )
                    VALUES (%s, %s, %s)
                    RETURNING
                        asset_id,
                        asset_name,
                        trading_symbol,
                        instrument_key
                    """,
                    (
                        asset.name,
                        asset.trading_symbol,
                        asset.instrument_key or None,
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
                        instrument_key
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
                        instrument_key
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
                    INSERT INTO public.tracker (asset_id, added_date)
                    SELECT asset_id, CURRENT_DATE
                    FROM public.assets
                    WHERE upper(trading_symbol) = upper(%s)
                    ON CONFLICT (asset_id) DO NOTHING
                    RETURNING tracker_details_id, asset_id, added_date
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
            added_date=row[2],
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
                        tracker.added_date
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
                        tracker.added_date
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
    )


def _tracker_entry(row: tuple[Any, ...]) -> TrackerEntry:
    return TrackerEntry(
        tracker_details_id=int(row[0]),
        asset_id=int(row[1]),
        asset_name=str(row[2]),
        trading_symbol=str(row[3]),
        added_date=row[4],
    )
