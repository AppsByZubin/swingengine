"""Background scheduler for daily Upstox approval requests."""

from datetime import UTC, datetime, timedelta
import logging
from threading import Event, Thread
from zoneinfo import ZoneInfo

from upstox.config import UpstoxSettings
from upstox.service import OperationResult, TokenRotationService

LOGGER = logging.getLogger(__name__)


class TokenRequestScheduler:
    """Request one token approval per local trading day."""

    def __init__(
        self, settings: UpstoxSettings, service: TokenRotationService
    ):
        self.settings = settings
        self.service = service
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._last_attempt: datetime | None = None
        self._completed_date: str | None = None

    def start(self) -> None:
        if not self.settings.rotation_enabled:
            return
        if self._thread is not None:
            return
        self._thread = Thread(
            target=self._run,
            name="upstox-token-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def run_pending(self, now: datetime | None = None) -> OperationResult | None:
        current = now or datetime.now(UTC)
        local = current.astimezone(ZoneInfo(self.settings.timezone_name))
        if local.time().replace(tzinfo=None) < self.settings.request_time:
            return None
        request_date = local.date().isoformat()
        if self._completed_date == request_date:
            return None
        if self._last_attempt is not None:
            retry_at = self._last_attempt + timedelta(
                seconds=self.settings.retry_interval_seconds
            )
            if current < retry_at:
                return None

        self._last_attempt = current
        result = self.service.request_token(force=False, now=current)
        if result.ok:
            self._completed_date = request_date
        return result

    def _run(self) -> None:
        LOGGER.info(
            "Upstox token scheduler started request_time=%s timezone=%s",
            self.settings.request_time.isoformat(timespec="minutes"),
            self.settings.timezone_name,
        )
        while not self._stop_event.is_set():
            result = self.run_pending()
            if result is not None:
                log = LOGGER.info if result.ok else LOGGER.warning
                log("Scheduled Upstox token request: %s", result.message)
            self._stop_event.wait(
                self.settings.scheduler_poll_interval_seconds
            )
