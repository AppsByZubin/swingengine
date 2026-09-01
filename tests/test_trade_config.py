from datetime import time

import pytest

from trade.config import TradeExecutionConfigurationError, TradeExecutionSettings


def test_execution_is_disabled_by_default() -> None:
    settings = TradeExecutionSettings.from_env({})

    assert settings.enabled is False
    assert settings.entry_window_start == time(10, 20)
    assert settings.entry_window_end == time(15, 0)
    assert settings.poll_interval_seconds == 300
    assert settings.minimum_amount_allocated == 1000.0
    assert settings.atr_period == 8
    assert settings.target_atr_multiple == 3.0
    assert settings.stoploss_atr_multiple == 2.0
    assert settings.price_rounding_increment == 5.0
    assert settings.product == "CNC"


def test_enabled_execution_loads_overrides() -> None:
    settings = TradeExecutionSettings.from_env(
        {
            "SWINGENGINE_TRADE_EXECUTION_ENABLED": "true",
            "SWINGENGINE_TRADE_ENTRY_WINDOW_START": "09:30",
            "SWINGENGINE_TRADE_ENTRY_WINDOW_END": "14:45",
            "SWINGENGINE_TRADE_POLL_INTERVAL_SECONDS": "300",
            "SWINGENGINE_TRADE_MINIMUM_AMOUNT_ALLOCATED": "2000",
            "SWINGENGINE_TRADE_ATR_PERIOD": "14",
            "SWINGENGINE_TRADE_PRODUCT": "mis",
        }
    )

    assert settings.enabled is True
    assert settings.entry_window_start == time(9, 30)
    assert settings.entry_window_end == time(14, 45)
    assert settings.poll_interval_seconds == 300
    assert settings.minimum_amount_allocated == 2000.0
    assert settings.atr_period == 14
    assert settings.product == "MIS"


def test_entry_window_start_must_precede_end() -> None:
    with pytest.raises(TradeExecutionConfigurationError):
        TradeExecutionSettings.from_env(
            {
                "SWINGENGINE_TRADE_ENTRY_WINDOW_START": "15:00",
                "SWINGENGINE_TRADE_ENTRY_WINDOW_END": "10:20",
            }
        )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SWINGENGINE_TRADE_EXECUTION_TIMEZONE", "Nowhere/Fake"),
        ("SWINGENGINE_TRADE_ENTRY_WINDOW_START", "9:30 tomorrow"),
        ("SWINGENGINE_TRADE_POLL_INTERVAL_SECONDS", "-1"),
        ("SWINGENGINE_TRADE_ATR_PERIOD", "0"),
        ("SWINGENGINE_TRADE_TARGET_ATR_MULTIPLE", "-3"),
    ],
)
def test_invalid_runtime_configuration_is_rejected(
    name: str, value: str
) -> None:
    with pytest.raises(TradeExecutionConfigurationError):
        TradeExecutionSettings.from_env({name: value})
