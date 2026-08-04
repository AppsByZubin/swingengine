"""Environment-backed settings for tracker momentum evaluation."""

from dataclasses import dataclass
from datetime import time
from math import isfinite
from os import environ
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TrackerEvaluationConfigurationError(ValueError):
    """Raised when tracker evaluation configuration is invalid."""


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
    try:
        parsed = int(values.get(name, str(default)).strip())
    except ValueError:
        parsed = 0
    if parsed <= 0:
        errors.append(f"{name} must be a positive integer")
        return default
    return parsed


def _parse_float(
    values: Mapping[str, str],
    name: str,
    default: float,
    errors: list[str],
) -> float:
    try:
        parsed = float(values.get(name, str(default)).strip())
    except ValueError:
        errors.append(f"{name} must be a number")
        return default
    if not isfinite(parsed):
        errors.append(f"{name} must be a finite number")
        return default
    return parsed


def _parse_positive_float(
    values: Mapping[str, str],
    name: str,
    default: float,
    errors: list[str],
) -> float:
    parsed = _parse_float(values, name, default, errors)
    if parsed <= 0:
        errors.append(f"{name} must be greater than zero")
        return default
    return parsed


@dataclass(frozen=True)
class TrackerEvaluationSettings:
    """Schedule and strategy settings for the daily momentum screen."""

    enabled: bool
    evaluation_time: time
    timezone_name: str
    lookback_days: int
    ema_angle_threshold: float
    sma_angle_threshold: float
    momentum_scan_lookback_days: int
    momentum_scan_request_interval_seconds: float
    retry_interval_seconds: int
    poll_interval_seconds: int

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "TrackerEvaluationSettings":
        values = environ if env is None else env
        errors: list[str] = []

        enabled = _parse_bool(
            values,
            "SWINGENGINE_TRACKER_EVALUATION_ENABLED",
            True,
            errors,
        )
        raw_time = values.get(
            "SWINGENGINE_TRACKER_EVALUATION_TIME", "16:00"
        ).strip()
        try:
            evaluation_time = time.fromisoformat(raw_time)
            if evaluation_time.tzinfo is not None:
                raise ValueError
        except ValueError:
            errors.append(
                "SWINGENGINE_TRACKER_EVALUATION_TIME must use HH:MM or HH:MM:SS"
            )
            evaluation_time = time(hour=16)

        timezone_name = values.get(
            "SWINGENGINE_TRACKER_EVALUATION_TIMEZONE",
            "Asia/Kolkata",
        ).strip()
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            errors.append(
                "SWINGENGINE_TRACKER_EVALUATION_TIMEZONE must be a valid "
                "IANA timezone"
            )

        lookback_days = _parse_positive_int(
            values,
            "SWINGENGINE_TRACKER_EVALUATION_LOOKBACK_DAYS",
            200,
            errors,
        )
        ema_angle_threshold = _parse_float(
            values,
            "SWINGENGINE_TRACKER_EMA_ANGLE_THRESHOLD",
            70.0,
            errors,
        )
        sma_angle_threshold = _parse_float(
            values,
            "SWINGENGINE_TRACKER_SMA_ANGLE_THRESHOLD",
            50.0,
            errors,
        )
        momentum_scan_request_interval_seconds = _parse_positive_float(
            values,
            "SWINGENGINE_MOMENTUM_SCAN_REQUEST_INTERVAL_SECONDS",
            1.0,
            errors,
        )
        momentum_scan_lookback_days = _parse_positive_int(
            values,
            "SWINGENGINE_MOMENTUM_SCAN_LOOKBACK_DAYS",
            365,
            errors,
        )
        if momentum_scan_lookback_days < 200:
            errors.append(
                "SWINGENGINE_MOMENTUM_SCAN_LOOKBACK_DAYS must be at least 200"
            )
        retry_interval_seconds = _parse_positive_int(
            values,
            "SWINGENGINE_TRACKER_EVALUATION_RETRY_INTERVAL_SECONDS",
            300,
            errors,
        )
        poll_interval_seconds = _parse_positive_int(
            values,
            "SWINGENGINE_TRACKER_EVALUATION_POLL_INTERVAL_SECONDS",
            30,
            errors,
        )

        if errors:
            raise TrackerEvaluationConfigurationError("; ".join(errors))
        return cls(
            enabled=enabled,
            evaluation_time=evaluation_time,
            timezone_name=timezone_name,
            lookback_days=lookback_days,
            ema_angle_threshold=ema_angle_threshold,
            sma_angle_threshold=sma_angle_threshold,
            momentum_scan_lookback_days=momentum_scan_lookback_days,
            momentum_scan_request_interval_seconds=(
                momentum_scan_request_interval_seconds
            ),
            retry_interval_seconds=retry_interval_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
