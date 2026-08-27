"""Recurring weekday scheduler for automated trade execution."""

from datetime import UTC, datetime
import logging
from threading import Event, Thread
from typing import Protocol
from zoneinfo import ZoneInfo

from trade.config import TradeExecutionSettings
from trade.executor import CycleResult

LOGGER = logging.getLogger(__name__)


class TradeExecutionCycle(Protocol):
    def run_cycle(self, *, now: datetime | None = None) -> CycleResult:
        """Run one trade-execution cycle."""


class TradeExecutionScheduler:
    """Run the trade execution cycle every poll interval on weekdays.

    Unlike ``TrackerEvaluationScheduler``, there is no once-per-day gate
    here: each step's own idempotency comes from the database state itself
    (a filled/cancelled order is no longer "pending" and won't be picked up
    again), so it's safe to just run every tick.
    """

    def __init__(
        self, settings: TradeExecutionSettings, service: TradeExecutionCycle
    ):
        self.settings = settings
        self.service = service
        self._stop_event = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if not self.settings.enabled or self._thread is not None:
            return
        self._thread = Thread(
            target=self._run, name="trade-execution-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def run_pending(self, now: datetime | None = None) -> CycleResult | None:
        current = now or datetime.now(UTC)
        local = current.astimezone(ZoneInfo(self.settings.timezone_name))
        if local.weekday() >= 5:
            return None
        return self.service.run_cycle(now=current)

    def _run(self) -> None:
        LOGGER.info(
            "Trade execution scheduler started poll_interval_seconds=%d",
            self.settings.poll_interval_seconds,
        )
        while not self._stop_event.is_set():
            result = self.run_pending()
            if result is not None:
                log = LOGGER.info if result.ok else LOGGER.warning
                log("Scheduled trade execution cycle: %s", result.message)
            self._stop_event.wait(self.settings.poll_interval_seconds)
