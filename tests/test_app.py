import logging
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


def test_slash_command_is_logged(caplog: Any) -> None:
    command = {
        "command": "/swingengine",
        "text": "status portfolio",
        "user_id": "U123",
        "channel_id": "C456",
    }

    with caplog.at_level(logging.INFO, logger="slack.app"):
        handle_slash_command(lambda: None, lambda _: None, command, build_router())

    assert caplog.messages == [
        "Received Slack command command='/swingengine' text='status portfolio' "
        "user_id='U123' channel_id='C456'"
    ]
