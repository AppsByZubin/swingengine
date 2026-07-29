import logging
from typing import Any

from slack.app import _redact_sensitive_command, handle_slash_command
from slack.commands import CommandRouter, build_router, file_upload_response
from slack.file_exports import SlackFileUpload


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


def test_auth_set_token_is_redacted_from_logs(caplog: Any) -> None:
    command = {
        "command": "/swingengine",
        "text": "auth set secret-token-value",
        "user_id": "U123",
        "channel_id": "C456",
    }

    with caplog.at_level(logging.INFO, logger="slack.app"):
        handle_slash_command(lambda: None, lambda _: None, command, build_router())

    assert "secret-token-value" not in caplog.text
    assert "auth set [REDACTED]" in caplog.text
    assert _redact_sensitive_command("AUTH SET token") == "auth set [REDACTED]"


def test_auth_set_is_restricted_to_configured_user() -> None:
    calls: list[tuple[str, Any]] = []
    command = {
        "command": "/swingengine",
        "text": "auth set secret-token-value",
        "user_id": "UOTHER",
        "channel_id": "C456",
    }

    handle_slash_command(
        lambda: calls.append(("ack", None)),
        lambda message: calls.append(("respond", message)),
        command,
        build_router(),
        authorized_user_id="UADMIN",
    )

    assert calls[0] == ("ack", None)
    assert calls[1][1]["response_type"] == "ephemeral"
    assert calls[1][1]["text"] == (
        "You are not authorized to replace the Upstox token."
    )


def test_file_response_is_uploaded_to_command_channel(tmp_path) -> None:
    calls: list[tuple[str, Any]] = []
    upload = SlackFileUpload(
        path=tmp_path / "asset-list.csv",
        title="Saved assets",
        initial_comment="Here are the saved assets.",
    )
    upload.path.write_text("asset_id\n42\n", encoding="utf-8")
    router = CommandRouter()
    router.register("help", lambda _: {"text": "help"})
    router.register(
        "export",
        lambda _: file_upload_response("CSV uploaded.", upload),
    )

    class FakeClient:
        def files_upload_v2(self, **arguments: Any) -> None:
            calls.append(("upload", arguments))

    handle_slash_command(
        lambda: calls.append(("ack", None)),
        lambda message: calls.append(("respond", message)),
        {
            "command": "/swingengine",
            "text": "export",
            "channel_id": "C456",
            "user_id": "U123",
        },
        router,
        client=FakeClient(),
    )

    assert calls[0] == ("ack", None)
    assert calls[1][0] == "upload"
    assert calls[1][1] == {
        "channel": "C456",
        "file": str(upload.path),
        "filename": "asset-list.csv",
        "title": "Saved assets",
        "initial_comment": "Here are the saved assets.",
    }
    assert calls[2] == (
        "respond",
        {"response_type": "ephemeral", "text": "CSV uploaded."},
    )
