import logging
from datetime import date
from typing import Any

import psycopg
import pytest

from database.config import DatabaseSettings
from database.repository import (
    AssetTrackerRepository,
    RepositoryError,
    TrackerNotFoundError,
)
from upstox.assets import AssetSearchResult


class RowsResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class StubConnection:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.query = ""
        self.parameters: tuple[object, ...] | None = None

    def __enter__(self) -> "StubConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(
        self,
        query: str,
        _parameters: tuple[object, ...] | None = None,
    ) -> RowsResult:
        self.query = query
        self.parameters = _parameters
        return RowsResult(self._rows)


def test_add_asset_logs_the_database_error_without_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    database_url = (
        "postgresql://swingengine_app:database-secret@postgres/swingengine"
    )

    def failing_connect(*_args: object, **_kwargs: object) -> object:
        raise psycopg.OperationalError("permission denied for table assets")

    repository = AssetTrackerRepository(
        DatabaseSettings(database_url=database_url),
        connect=failing_connect,
    )
    asset = AssetSearchResult(
        trading_symbol="SUNPHARMA",
        name="SUN PHARMACEUTICAL IND L",
        segment="NSE_EQ",
        instrument_type="EQ",
        instrument_key="NSE_EQ|INE044A01036",
    )

    with caplog.at_level(logging.ERROR, logger="database.repository"):
        with pytest.raises(RepositoryError, match="Unable to add the asset"):
            repository.add_asset(asset)

    assert "Failed to add asset trading_symbol='SUNPHARMA'" in caplog.text
    assert "permission denied for table assets" in caplog.text
    assert "database-secret" not in caplog.text


def test_add_asset_persists_the_supplied_fno_flag() -> None:
    connection = StubConnection(
        [(42, "SUN PHARMACEUTICAL IND L", "SUNPHARMA", "NSE_EQ|INE044A01036", True)]
    )
    repository = AssetTrackerRepository(
        DatabaseSettings(database_url="postgresql://u:p@postgres/swingengine"),
        connect=lambda *_args, **_kwargs: connection,
    )
    asset = AssetSearchResult(
        trading_symbol="SUNPHARMA",
        name="SUN PHARMACEUTICAL IND L",
        segment="NSE_EQ",
        instrument_type="EQ",
        instrument_key="NSE_EQ|INE044A01036",
    )

    record = repository.add_asset(asset, has_fno=True)

    assert record.has_fno is True
    assert "has_fno" in connection.query
    assert connection.parameters == (
        asset.name,
        asset.trading_symbol,
        asset.instrument_key,
        True,
    )


def test_list_tracker_returns_all_exported_state_fields() -> None:
    connection = StubConnection(
        [
            (
                7,
                42,
                "SUN PHARMACEUTICAL IND L",
                "SUNPHARMA",
                True,
                False,
                True,
                12500.5,
                date(2026, 7, 28),
                True,
                "buy",
            )
        ]
    )
    repository = AssetTrackerRepository(
        DatabaseSettings(database_url="postgresql:///swingengine"),
        connect=lambda *_args, **_kwargs: connection,
    )

    entries = repository.list_tracker()

    assert len(entries) == 1
    assert entries[0].has_momentum is True
    assert entries[0].is_trade_created is False
    assert entries[0].is_approved_for_trade is True
    assert entries[0].amount_allocated == 12500.5
    assert entries[0].added_date == date(2026, 7, 28)
    assert entries[0].has_fno is True
    assert entries[0].side == "buy"
    assert "tracker.has_momentum" in connection.query
    assert "tracker.amount_allocated" in connection.query
    assert "tracker.has_fno" in connection.query
    assert "tracker.side" in connection.query


def test_update_tracker_trade_settings_changes_only_admin_managed_fields() -> None:
    connection = StubConnection(
        [
            (
                7,
                42,
                "TATA CONSULTANCY SERV LT",
                "TCS",
                True,
                False,
                True,
                7500.0,
                date(2026, 7, 30),
                False,
                None,
            )
        ]
    )
    repository = AssetTrackerRepository(
        DatabaseSettings(database_url="postgresql:///swingengine"),
        connect=lambda *_args, **_kwargs: connection,
    )

    entry = repository.update_tracker_trade_settings(
        "TCS",
        True,
        7500.0,
    )

    assert entry.trading_symbol == "TCS"
    assert entry.is_approved_for_trade is True
    assert entry.amount_allocated == 7500.0
    assert entry.side is None
    assert "UPDATE public.tracker AS tracker" in connection.query
    assert "is_approved_for_trade = %s" in connection.query
    assert "amount_allocated = %s" in connection.query
    assert "has_momentum =" not in connection.query
    assert "is_trade_created =" not in connection.query
    assert connection.parameters == (True, 7500.0, "TCS")


def test_update_tracker_trade_settings_reports_a_missing_tracker() -> None:
    connection = StubConnection([])
    repository = AssetTrackerRepository(
        DatabaseSettings(database_url="postgresql:///swingengine"),
        connect=lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(TrackerNotFoundError):
        repository.update_tracker_trade_settings("MISSING", False, 0.0)


def test_list_momentum_candidates_includes_untracked_and_pending_assets() -> None:
    connection = StubConnection(
        [
            (
                42,
                "SUN PHARMACEUTICAL IND L",
                "SUNPHARMA",
                "NSE_EQ|INE044A01036",
                None,
            ),
            (
                43,
                "TATA CONSULTANCY SERVICES",
                "TCS",
                "NSE_EQ|INE467B01029",
                9,
            ),
        ]
    )
    repository = AssetTrackerRepository(
        DatabaseSettings(database_url="postgresql:///swingengine"),
        connect=lambda *_args, **_kwargs: connection,
    )

    candidates = repository.list_momentum_candidates()

    assert [candidate.trading_symbol for candidate in candidates] == [
        "SUNPHARMA",
        "TCS",
    ]
    assert candidates[0].tracker_details_id is None
    assert candidates[1].tracker_details_id == 9
    assert "LEFT JOIN public.tracker" in connection.query
    assert "tracker.is_trade_created = FALSE" in connection.query


def test_qualifying_momentum_is_upserted_with_requested_tracker_state() -> None:
    connection = StubConnection([(7,)])
    repository = AssetTrackerRepository(
        DatabaseSettings(database_url="postgresql:///swingengine"),
        connect=lambda *_args, **_kwargs: connection,
    )

    persisted = repository.record_momentum_evaluation(
        42,
        True,
        date(2026, 7, 30),
    )

    assert persisted
    assert "ON CONFLICT (asset_id) DO UPDATE" in connection.query
    assert "has_momentum = TRUE" in connection.query
    assert "is_trade_created = FALSE" in connection.query
    assert "is_approved_for_trade = FALSE" in connection.query
    assert "has_fno = EXCLUDED.has_fno" in connection.query
    assert "side = EXCLUDED.side" in connection.query
    assert connection.parameters == (42, date(2026, 7, 30), None, 42)


def test_qualifying_momentum_persists_the_supplied_side() -> None:
    connection = StubConnection([(7,)])
    repository = AssetTrackerRepository(
        DatabaseSettings(database_url="postgresql:///swingengine"),
        connect=lambda *_args, **_kwargs: connection,
    )

    persisted = repository.record_momentum_evaluation(
        42,
        True,
        date(2026, 7, 30),
        side="buy",
    )

    assert persisted
    assert connection.parameters == (42, date(2026, 7, 30), "buy", 42)


def test_nonqualifying_pending_momentum_is_cleared_without_insert() -> None:
    connection = StubConnection([(7,)])
    repository = AssetTrackerRepository(
        DatabaseSettings(database_url="postgresql:///swingengine"),
        connect=lambda *_args, **_kwargs: connection,
    )

    persisted = repository.record_momentum_evaluation(
        42,
        False,
        date(2026, 7, 30),
    )

    assert persisted
    assert "UPDATE public.tracker" in connection.query
    assert "has_momentum = FALSE" in connection.query
    assert "side = NULL" in connection.query
    assert "is_trade_created = FALSE" in connection.query


def test_find_asset_by_trading_symbol_returns_none_when_unsaved() -> None:
    connection = StubConnection([])
    repository = AssetTrackerRepository(
        DatabaseSettings(database_url="postgresql:///swingengine"),
        connect=lambda *_args, **_kwargs: connection,
    )

    assert repository.find_asset_by_trading_symbol("MISSING") is None


def test_find_asset_by_trading_symbol_returns_the_saved_asset() -> None:
    connection = StubConnection(
        [(42, "SUN PHARMACEUTICAL IND L", "SUNPHARMA", "NSE_EQ|INE044A01036", True)]
    )
    repository = AssetTrackerRepository(
        DatabaseSettings(database_url="postgresql:///swingengine"),
        connect=lambda *_args, **_kwargs: connection,
    )

    asset = repository.find_asset_by_trading_symbol("sunpharma")

    assert asset is not None
    assert asset.trading_symbol == "SUNPHARMA"
    assert connection.parameters == ("sunpharma",)


def test_list_tracker_assets_returns_instrument_keys_for_every_entry() -> None:
    connection = StubConnection(
        [
            (42, "SUN PHARMACEUTICAL IND L", "SUNPHARMA", "NSE_EQ|INE044A01036", 7),
            (43, "TATA CONSULTANCY SERVICES", "TCS", "NSE_EQ|INE467B01029", 9),
        ]
    )
    repository = AssetTrackerRepository(
        DatabaseSettings(database_url="postgresql:///swingengine"),
        connect=lambda *_args, **_kwargs: connection,
    )

    candidates = repository.list_tracker_assets()

    assert [candidate.trading_symbol for candidate in candidates] == [
        "SUNPHARMA",
        "TCS",
    ]
    assert candidates[0].instrument_key == "NSE_EQ|INE044A01036"
    assert candidates[0].tracker_details_id == 7
    assert "JOIN public.assets" in connection.query
