from datetime import time

import pytest

from tracker.config import (
    TrackerEvaluationConfigurationError,
    TrackerEvaluationSettings,
)


def test_tracker_evaluation_defaults_to_weekday_post_market_settings() -> None:
    settings = TrackerEvaluationSettings.from_env({})

    assert settings.enabled
    assert settings.evaluation_time == time(hour=16)
    assert settings.timezone_name == "Asia/Kolkata"
    assert settings.lookback_days == 300
    assert settings.momentum_scan_lookback_days == 365
    assert settings.momentum_scan_minimum_candles == 200
    assert settings.momentum_scan_request_interval_seconds == 1
    assert settings.momentum_angle_threshold_degrees == 40


def test_momentum_scan_minimum_candles_can_be_configured() -> None:
    settings = TrackerEvaluationSettings.from_env(
        {"SWINGENGINE_MOMENTUM_SCAN_MINIMUM_CANDLES": "100"}
    )

    assert settings.momentum_scan_minimum_candles == 100


def test_momentum_angle_threshold_degrees_can_be_configured() -> None:
    settings = TrackerEvaluationSettings.from_env(
        {"SWINGENGINE_MOMENTUM_ANGLE_THRESHOLD_DEGREES": "35"}
    )

    assert settings.momentum_angle_threshold_degrees == 35


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SWINGENGINE_TRACKER_EVALUATION_ENABLED", "sometimes"),
        ("SWINGENGINE_TRACKER_EVALUATION_TIME", "after close"),
        ("SWINGENGINE_TRACKER_EVALUATION_TIME", "16:00+05:30"),
        ("SWINGENGINE_TRACKER_EVALUATION_TIMEZONE", "Mars/Olympus"),
        ("SWINGENGINE_TRACKER_EVALUATION_LOOKBACK_DAYS", "0"),
        ("SWINGENGINE_MOMENTUM_SCAN_REQUEST_INTERVAL_SECONDS", "0"),
        ("SWINGENGINE_MOMENTUM_SCAN_MINIMUM_CANDLES", "0"),
        ("SWINGENGINE_MOMENTUM_SCAN_LOOKBACK_DAYS", "199"),
        ("SWINGENGINE_MOMENTUM_ANGLE_THRESHOLD_DEGREES", "0"),
        ("SWINGENGINE_MOMENTUM_ANGLE_THRESHOLD_DEGREES", "90"),
    ],
)
def test_invalid_tracker_evaluation_configuration_is_rejected(
    name: str,
    value: str,
) -> None:
    with pytest.raises(TrackerEvaluationConfigurationError):
        TrackerEvaluationSettings.from_env({name: value})
