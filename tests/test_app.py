from typing import Any

from slack.app import handle_slash_command
from slack.commands import build_router


def test_slash_command_is_acknowledged_before_response() -> None:
    calls: list[tuple[str, Any]] = []

    def ack() -> None:
        calls.append(("ack", None))

    def respond(message: dict[str, Any]) -> None:
        calls.append(("respond", message))

    handle_slash_command(ack, respond, {"text": "ping"}, build_router())

    assert calls[0] == ("ack", None)
    assert calls[1][0] == "respond"
    assert calls[1][1]["text"] == "pong"
