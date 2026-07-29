"""Weekday scheduler for the post-market tracker momentum screen."""

from datetime import UTC, datetime, timedelta
import logging
from threading import Event, Thread
from typing import Protocol
from zoneinfo import ZoneInfo

from tracker.config import TrackerEvaluationSettings
from tracker.evaluator import EvaluationResult

LOGGER = logging.getLogger(__name__)


class EvaluationService(Protocol):
    def evaluate(self, *, now: datetime | None = None) -> EvaluationResult:
        """Run one tracker asset evaluation."""


class TrackerEvaluationScheduler:
    """Run the momentum screen once after 4 PM on each weekday."""

    def __init__(
        self,
        settings: TrackerEvaluationSettings,
        service: EvaluationService,
    ):
        self.settings = settings
        self.service = service
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._last_attempt: datetime | None = None
        self._completed_date: str | None = None

    def start(self) -> None:
        if not self.settings.enabled or self._thread is not None:
            return
        self._thread = Thread(
            target=self._run,
            name="tracker-evaluation-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def run_pending(
        self, now: datetime | None = None
    ) -> EvaluationResult | None:
        current = now or datetime.now(UTC)
        local = current.astimezone(ZoneInfo(self.settings.timezone_name))
        if local.weekday() >= 5:
            return None
        if local.time().replace(tzinfo=None) < self.settings.evaluation_time:
            return None

        evaluation_date = local.date().isoformat()
        if self._completed_date == evaluation_date:
            return None
        if self._last_attempt is not None:
            retry_at = self._last_attempt + timedelta(
                seconds=self.settings.retry_interval_seconds
            )
            if current < retry_at:
                return None

        self._last_attempt = current
        result = self.service.evaluate(now=current)
        if result.ok:
            self._completed_date = evaluation_date
        return result

    def _run(self) -> None:
        LOGGER.info(
            "Tracker evaluation scheduler started evaluation_time=%s "
            "timezone=%s",
            self.settings.evaluation_time.isoformat(timespec="minutes"),
            self.settings.timezone_name,
        )
        while not self._stop_event.is_set():
            result = self.run_pending()
            if result is not None:
                log = LOGGER.info if result.ok else LOGGER.warning
                log("Scheduled tracker asset evaluation: %s", result.message)
            self._stop_event.wait(self.settings.poll_interval_seconds)
