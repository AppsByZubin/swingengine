from dataclasses import replace
from datetime import UTC, datetime, time

from upstox.config import UpstoxSettings
from upstox.scheduler import TokenRequestScheduler
from upstox.service import OperationResult


class RecordingService:
    def __init__(self) -> None:
        self.calls: list[tuple[bool, datetime | None]] = []

    def request_token(
        self, *, force: bool = False, now: datetime | None = None
    ) -> OperationResult:
        self.calls.append((force, now))
        return OperationResult(True, "requested")


def settings() -> UpstoxSettings:
    return replace(
        UpstoxSettings.from_env({}),
        enabled=True,
        request_time=time(hour=7, minute=30),
        retry_interval_seconds=300,
    )


def test_scheduler_waits_until_configured_local_time() -> None:
    service = RecordingService()
    scheduler = TokenRequestScheduler(
        settings(), service  # type: ignore[arg-type]
    )

    assert scheduler.run_pending(
        datetime(2026, 7, 27, 1, 59, tzinfo=UTC)
    ) is None
    assert service.calls == []

    result = scheduler.run_pending(
        datetime(2026, 7, 27, 2, 0, tzinfo=UTC)
    )

    assert result is not None and result.ok
    assert len(service.calls) == 1


def test_scheduler_throttles_failed_or_duplicate_attempts() -> None:
    service = RecordingService()
    scheduler = TokenRequestScheduler(
        settings(), service  # type: ignore[arg-type]
    )
    first = datetime(2026, 7, 27, 2, 0, tzinfo=UTC)

    scheduler.run_pending(first)
    scheduler.run_pending(datetime(2026, 7, 27, 2, 1, tzinfo=UTC))
    scheduler.run_pending(datetime(2026, 7, 27, 2, 10, tzinfo=UTC))

    assert len(service.calls) == 1
