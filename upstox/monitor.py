"""Periodic validation of the persisted Upstox access token."""

from collections.abc import Callable
from datetime import UTC, datetime
import logging
from threading import Event, Thread

from upstox.config import UpstoxSettings
from upstox.service import OperationResult, TokenRotationService

LOGGER = logging.getLogger(__name__)


class TokenHealthMonitor:
    """Verify the token immediately and then at a fixed interval."""

    def __init__(
        self,
        settings: UpstoxSettings,
        service: TokenRotationService,
        notify: Callable[[str], None],
    ):
        self.settings = settings
        self.service = service
        self.notify = notify
        self._stop_event = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if not self.settings.monitor_enabled or self._thread is not None:
            return
        self._thread = Thread(
            target=self._run,
            name="upstox-token-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def run_once(self, now: datetime | None = None) -> OperationResult:
        result = self.service.validate_current_token(now=now or datetime.now(UTC))
        if result.ok:
            LOGGER.info("Periodic Upstox token check succeeded")
        else:
            LOGGER.warning("Periodic Upstox token check failed: %s", result.message)
            self.notify(result.message)
        return result

    def _run(self) -> None:
        LOGGER.info(
            "Upstox token monitor started interval_seconds=%d",
            self.settings.monitor_interval_seconds,
        )
        while not self._stop_event.is_set():
            self.run_once()
            self._stop_event.wait(self.settings.monitor_interval_seconds)
