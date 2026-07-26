from dataclasses import replace
from http.client import HTTPConnection
import json

from upstox.config import UpstoxSettings
from upstox.service import OperationResult
from upstox.webhook import UpstoxWebhookServer


class RecordingService:
    def __init__(self) -> None:
        self.payloads: list[object] = []

    def status_message(self) -> str:
        return "authorization pending"

    def accept_webhook(self, payload: object) -> OperationResult:
        self.payloads.append(payload)
        return OperationResult(True, "stored")


def test_webhook_serves_health_and_forwards_json_payload() -> None:
    settings = replace(
        UpstoxSettings.from_env({}),
        webhook_enabled=True,
        webhook_host="127.0.0.1",
        webhook_port=0,
    )
    service = RecordingService()
    server = UpstoxWebhookServer(
        settings, service  # type: ignore[arg-type]
    )
    server.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.port, timeout=2)
        connection.request("GET", "/healthz")
        health = connection.getresponse()
        assert health.status == 200
        health.read()

        payload = {"message_type": "access_token"}
        connection.request(
            "POST",
            settings.webhook_path,
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["status"] == "success"
        assert service.payloads == [payload]
    finally:
        server.stop()

