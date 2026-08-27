from datetime import UTC, datetime

from trade.config import TradeExecutionSettings
from trade.executor import CycleResult
from trade.scheduler import TradeExecutionScheduler


class RecordingService:
    def __init__(self) -> None:
        self.calls: list[datetime | None] = []

    def run_cycle(self, *, now: datetime | None = None) -> CycleResult:
        self.calls.append(now)
        return CycleResult(True, "cycle completed")


def test_scheduler_runs_every_tick_on_a_weekday() -> None:
    service = RecordingService()
    scheduler = TradeExecutionScheduler(
        TradeExecutionSettings.from_env(
            {"SWINGENGINE_TRADE_EXECUTION_ENABLED": "true"}
        ),
        service,
    )

    first = scheduler.run_pending(datetime(2026, 7, 30, 6, 0, tzinfo=UTC))
    second = scheduler.run_pending(datetime(2026, 7, 30, 6, 10, tzinfo=UTC))

    assert first is not None
    assert second is not None
    assert len(service.calls) == 2


def test_scheduler_skips_saturday_and_sunday() -> None:
    service = RecordingService()
    scheduler = TradeExecutionScheduler(
        TradeExecutionSettings.from_env(
            {"SWINGENGINE_TRADE_EXECUTION_ENABLED": "true"}
        ),
        service,
    )

    assert scheduler.run_pending(
        datetime(2026, 8, 1, 6, 0, tzinfo=UTC)
    ) is None
    assert scheduler.run_pending(
        datetime(2026, 8, 2, 6, 0, tzinfo=UTC)
    ) is None
    assert service.calls == []
