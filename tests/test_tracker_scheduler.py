from datetime import UTC, datetime

from tracker.config import TrackerEvaluationSettings
from tracker.evaluator import EvaluationResult
from tracker.scheduler import TrackerEvaluationScheduler


class RecordingEvaluator:
    def __init__(self) -> None:
        self.calls: list[datetime | None] = []

    def evaluate(self, *, now: datetime | None = None) -> EvaluationResult:
        self.calls.append(now)
        return EvaluationResult(True, "evaluated")


def test_scheduler_runs_once_after_4pm_ist_on_a_weekday() -> None:
    service = RecordingEvaluator()
    scheduler = TrackerEvaluationScheduler(
        TrackerEvaluationSettings.from_env({}),
        service,
    )

    assert scheduler.run_pending(
        datetime(2026, 7, 30, 10, 29, tzinfo=UTC)
    ) is None
    assert scheduler.run_pending(
        datetime(2026, 7, 30, 10, 30, tzinfo=UTC)
    ) is not None
    assert scheduler.run_pending(
        datetime(2026, 7, 30, 11, 30, tzinfo=UTC)
    ) is None
    assert len(service.calls) == 1


def test_scheduler_skips_saturday_and_sunday() -> None:
    service = RecordingEvaluator()
    scheduler = TrackerEvaluationScheduler(
        TrackerEvaluationSettings.from_env({}),
        service,
    )

    assert scheduler.run_pending(
        datetime(2026, 8, 1, 11, 0, tzinfo=UTC)
    ) is None
    assert scheduler.run_pending(
        datetime(2026, 8, 2, 11, 0, tzinfo=UTC)
    ) is None
    assert service.calls == []
