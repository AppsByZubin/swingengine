import pytest

from database.config import DatabaseConfigurationError, DatabaseSettings


def test_database_settings_load_connection_configuration() -> None:
    settings = DatabaseSettings.from_env(
        {
            "SWINGENGINE_DATABASE_URL": (
                "postgresql://swingengine:secret@postgres/swingengine"
            ),
            "SWINGENGINE_DATABASE_CONNECT_TIMEOUT_SECONDS": "15",
        }
    )

    assert settings.database_url.endswith("@postgres/swingengine")
    assert settings.connect_timeout_seconds == 15
    assert "secret" not in repr(settings)


def test_database_url_is_required() -> None:
    with pytest.raises(
        DatabaseConfigurationError, match="SWINGENGINE_DATABASE_URL"
    ):
        DatabaseSettings.from_env({})


@pytest.mark.parametrize("timeout", ["0", "-1", "later"])
def test_database_timeout_must_be_positive(timeout: str) -> None:
    with pytest.raises(
        DatabaseConfigurationError,
        match="SWINGENGINE_DATABASE_CONNECT_TIMEOUT_SECONDS",
    ):
        DatabaseSettings.from_env(
            {
                "SWINGENGINE_DATABASE_URL": "postgresql:///swingengine",
                "SWINGENGINE_DATABASE_CONNECT_TIMEOUT_SECONDS": timeout,
            }
        )
