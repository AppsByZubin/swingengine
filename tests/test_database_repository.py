import logging

import psycopg
import pytest

from database.config import DatabaseSettings
from database.repository import AssetTrackerRepository, RepositoryError
from upstox.assets import AssetSearchResult


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
