from dataclasses import replace
from datetime import UTC, datetime

from upstox.config import UpstoxSettings
from upstox.monitor import TokenHealthMonitor
from upstox.service import OperationResult


class RecordingService:
    def __init__(self, result: OperationResult):
        self.result = result
        self.calls: list[datetime | None] = []

    def validate_current_token(
        self, *, now: datetime | None = None
    ) -> OperationResult:
        self.calls.append(now)
        return self.result


def test_monitor_notifies_when_token_is_missing() -> None:
    settings = replace(
        UpstoxSettings.from_env({}),
        monitor_enabled=True,
        monitor_interval_seconds=10_800,
    )
    service = RecordingService(
        OperationResult(False, "No Upstox token is stored.", 404)
    )
    alerts: list[str] = []
    monitor = TokenHealthMonitor(
        settings,
        service,  # type: ignore[arg-type]
        alerts.append,
    )

    result = monitor.run_once(datetime(2026, 7, 27, 4, 0, tzinfo=UTC))

    assert not result.ok
    assert alerts == ["No Upstox token is stored."]


def test_monitor_does_not_alert_for_valid_token() -> None:
    settings = replace(
        UpstoxSettings.from_env({}),
        monitor_enabled=True,
    )
    service = RecordingService(OperationResult(True, "Token valid."))
    alerts: list[str] = []
    monitor = TokenHealthMonitor(
        settings,
        service,  # type: ignore[arg-type]
        alerts.append,
    )

    assert monitor.run_once().ok
    assert alerts == []

