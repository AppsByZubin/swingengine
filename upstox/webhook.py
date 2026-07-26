"""Small HTTP server for the Upstox notifier webhook and health checks."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from threading import Thread
from typing import Any
from urllib.parse import urlsplit

from upstox.config import UpstoxSettings
from upstox.service import OperationResult, TokenRotationService

LOGGER = logging.getLogger(__name__)
MAX_WEBHOOK_BODY_BYTES = 32 * 1024


class _WebhookHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class UpstoxWebhookServer:
    """Serve the notifier callback without exposing credential data in logs."""

    def __init__(
        self, settings: UpstoxSettings, service: TokenRotationService
    ):
        self.settings = settings
        self.service = service
        self._server: _WebhookHTTPServer | None = None
        self._thread: Thread | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            return self.settings.webhook_port
        return int(self._server.server_address[1])

    def start(self) -> None:
        if not self.settings.webhook_enabled or self._server is not None:
            return

        handler = _handler_for(self.settings, self.service)
        self._server = _WebhookHTTPServer(
            (self.settings.webhook_host, self.settings.webhook_port), handler
        )
        self._thread = Thread(
            target=self._server.serve_forever,
            name="upstox-webhook",
            daemon=True,
        )
        self._thread.start()
        LOGGER.info(
            "Upstox webhook listening host=%s port=%d path=%s",
            self.settings.webhook_host,
            self.port,
            self.settings.webhook_path,
        )

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None


def _handler_for(
    settings: UpstoxSettings, service: TokenRotationService
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "SwingEngineWebhook/1.0"

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path == "/healthz":
                self._send_json(200, {"status": "ok"})
                return
            if path == "/readyz":
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "upstox": service.status_message(),
                    },
                )
                return
            self._send_json(404, {"status": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if urlsplit(self.path).path != settings.webhook_path:
                self._send_json(404, {"status": "not_found"})
                return

            content_length = self.headers.get("Content-Length")
            if content_length is None:
                self._send_json(411, {"status": "error", "message": "length required"})
                return
            try:
                body_length = int(content_length)
            except ValueError:
                self._send_json(400, {"status": "error", "message": "invalid length"})
                return
            if body_length <= 0 or body_length > MAX_WEBHOOK_BODY_BYTES:
                self._send_json(
                    413, {"status": "error", "message": "invalid body size"}
                )
                return

            try:
                body = self.rfile.read(body_length)
                payload: Any = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(
                    400, {"status": "error", "message": "invalid JSON"}
                )
                return

            result = service.accept_webhook(payload)
            response = {
                "status": "success" if result.ok else "error",
                "message": result.message,
            }
            self._send_json(result.status_code, response)

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.debug("Webhook request: " + format, *args)

        def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler

