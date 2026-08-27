"""Environment-backed configuration for automated trade execution."""

from dataclasses import dataclass
from datetime import time
from os import environ
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TradeExecutionConfigurationError(ValueError):
    """Raised when trade execution configuration is invalid."""


def _parse_bool(
    values: Mapping[str, str],
    name: str,
    default: bool,
    errors: list[str],
) -> bool:
    raw_value = values.get(name)
    if raw_value is None or not raw_value.strip():
        return default

    normalized = raw_value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    errors.append(f"{name} must be true or false")
    return default


def _parse_positive_int(
    values: Mapping[str, str],
    name: str,
    default: int,
    errors: list[str],
) -> int:
    raw_value = values.get(name, str(default)).strip()
    try:
        parsed = int(raw_value)
    except ValueError:
        errors.append(f"{name} must be a positive integer")
        return default
    if parsed <= 0:
        errors.append(f"{name} must be a positive integer")
        return default
    return parsed


def _parse_positive_float(
    values: Mapping[str, str],
    name: str,
    default: float,
    errors: list[str],
) -> float:
    raw_value = values.get(name, str(default)).strip()
    try:
        parsed = float(raw_value)
    except ValueError:
        errors.append(f"{name} must be a number")
        return default
    if parsed <= 0:
        errors.append(f"{name} must be greater than zero")
        return default
    return parsed


def _parse_time(
    raw_value: str, name: str, errors: list[str], default: time
) -> time:
    try:
        parsed = time.fromisoformat(raw_value)
        if parsed.tzinfo is not None:
            raise ValueError
        return parsed
    except ValueError:
        errors.append(f"{name} must use HH:MM or HH:MM:SS")
        return default


@dataclass(frozen=True)
class TradeExecutionSettings:
    """Schedule, sizing, and indicator settings for automated trading."""

    enabled: bool
    timezone_name: str
    entry_window_start: time
    entry_window_end: time
    poll_interval_seconds: int
    minimum_amount_allocated: float
    atr_period: int
    target_atr_multiple: float
    stoploss_atr_multiple: float
    price_rounding_increment: float
    hourly_lookback_days: int
    product: str

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "TradeExecutionSettings":
        values = environ if env is None else env
        errors: list[str] = []

        enabled = _parse_bool(
            values, "SWINGENGINE_TRADE_EXECUTION_ENABLED", False, errors
        )

        timezone_name = values.get(
            "SWINGENGINE_TRADE_EXECUTION_TIMEZONE", "Asia/Kolkata"
        ).strip()
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            errors.append(
                "SWINGENGINE_TRADE_EXECUTION_TIMEZONE must be a valid IANA "
                "timezone"
            )

        entry_window_start = _parse_time(
            values.get("SWINGENGINE_TRADE_ENTRY_WINDOW_START", "10:20").strip(),
            "SWINGENGINE_TRADE_ENTRY_WINDOW_START",
            errors,
            time(10, 20),
        )
        entry_window_end = _parse_time(
            values.get("SWINGENGINE_TRADE_ENTRY_WINDOW_END", "15:00").strip(),
            "SWINGENGINE_TRADE_ENTRY_WINDOW_END",
            errors,
            time(15, 0),
        )
        if entry_window_start >= entry_window_end:
            errors.append(
                "SWINGENGINE_TRADE_ENTRY_WINDOW_START must be before "
                "SWINGENGINE_TRADE_ENTRY_WINDOW_END"
            )

        poll_interval_seconds = _parse_positive_int(
            values, "SWINGENGINE_TRADE_POLL_INTERVAL_SECONDS", 600, errors
        )
        minimum_amount_allocated = _parse_positive_float(
            values,
            "SWINGENGINE_TRADE_MINIMUM_AMOUNT_ALLOCATED",
            1000.0,
            errors,
        )
        atr_period = _parse_positive_int(
            values, "SWINGENGINE_TRADE_ATR_PERIOD", 8, errors
        )
        target_atr_multiple = _parse_positive_float(
            values, "SWINGENGINE_TRADE_TARGET_ATR_MULTIPLE", 3.0, errors
        )
        stoploss_atr_multiple = _parse_positive_float(
            values, "SWINGENGINE_TRADE_STOPLOSS_ATR_MULTIPLE", 2.0, errors
        )
        price_rounding_increment = _parse_positive_float(
            values,
            "SWINGENGINE_TRADE_PRICE_ROUNDING_INCREMENT",
            5.0,
            errors,
        )
        hourly_lookback_days = _parse_positive_int(
            values, "SWINGENGINE_TRADE_HOURLY_LOOKBACK_DAYS", 10, errors
        )
        product = values.get("SWINGENGINE_TRADE_PRODUCT", "CNC").strip().upper()
        if not product:
            errors.append("SWINGENGINE_TRADE_PRODUCT cannot be empty")

        if errors:
            raise TradeExecutionConfigurationError("; ".join(errors))

        return cls(
            enabled=enabled,
            timezone_name=timezone_name,
            entry_window_start=entry_window_start,
            entry_window_end=entry_window_end,
            poll_interval_seconds=poll_interval_seconds,
            minimum_amount_allocated=minimum_amount_allocated,
            atr_period=atr_period,
            target_atr_multiple=target_atr_multiple,
            stoploss_atr_multiple=stoploss_atr_multiple,
            price_rounding_increment=price_rounding_increment,
            hourly_lookback_days=hourly_lookback_days,
            product=product,
        )
