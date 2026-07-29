import logging
from datetime import date
from typing import Any

import psycopg
import pytest

from database.config import DatabaseSettings
from database.repository import AssetTrackerRepository, RepositoryError
from upstox.assets import AssetSearchResult


class RowsResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class StubConnection:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.query = ""

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
    assert entries[0].is_order_created is False
    assert entries[0].is_approved_for_order is True
    assert entries[0].amount_allocated == 12500.5
    assert entries[0].added_date == date(2026, 7, 28)
    assert "tracker.has_momentum" in connection.query
    assert "tracker.amount_allocated" in connection.query
