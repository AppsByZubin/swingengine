import logging
from typing import Any

from slack.app import (
    ASSET_IMPORT_ACTION_ID,
    ASSET_IMPORT_BLOCK_ID,
    ASSET_IMPORT_CALLBACK_ID,
    TRACKER_IMPORT_ACTION_ID,
    TRACKER_IMPORT_BLOCK_ID,
    TRACKER_IMPORT_CALLBACK_ID,
    _redact_sensitive_command,
    handle_asset_import_submission,
    handle_slash_command,
    handle_tracker_import_submission,
)
from slack.commands import CommandRouter, build_router, file_upload_response
from slack.file_exports import SlackFileUpload
from slack.file_imports import AssetImportSummary, TrackerImportSummary


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


def test_momentum_scan_reports_started_before_running_handler() -> None:
    calls: list[tuple[str, Any]] = []
    router = CommandRouter()
    router.register("help", lambda _: {"text": "help"})

    def momentum_handler(_: str) -> dict[str, Any]:
        calls.append(("scan", None))
        return {"response_type": "ephemeral", "text": "scan complete"}

    router.register("momentum", momentum_handler)

    handle_slash_command(
        lambda: calls.append(("ack", None)),
        lambda message: calls.append(("respond", message)),
        {"text": "momentum list file"},
        router,
    )

    assert calls[0] == ("ack", None)
    assert calls[1][0] == "respond"
    assert "scan started" in calls[1][1]["text"]
    assert calls[2] == ("scan", None)
    assert calls[3] == (
        "respond",
        {"response_type": "ephemeral", "text": "scan complete"},
    )


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


def test_asset_upload_command_opens_a_csv_file_modal() -> None:
    calls: list[tuple[str, Any]] = []

    class FakeClient:
        def views_open(self, **arguments: Any) -> None:
            calls.append(("modal", arguments))

    handle_slash_command(
        lambda: calls.append(("ack", None)),
        lambda message: calls.append(("respond", message)),
        {
            "command": "/swingengine",
            "text": "asset upload",
            "channel_id": "C456",
            "user_id": "U123",
            "trigger_id": "trigger-123",
        },
        build_router(),
        client=FakeClient(),
        asset_importer=object(),  # type: ignore[arg-type]
    )

    assert calls[0] == ("ack", None)
    assert calls[1][0] == "modal"
    assert calls[1][1]["trigger_id"] == "trigger-123"
    modal = calls[1][1]["view"]
    assert modal["callback_id"] == ASSET_IMPORT_CALLBACK_ID
    assert modal["private_metadata"] == "C456"
    file_input = modal["blocks"][1]["element"]
    assert file_input == {
        "type": "file_input",
        "action_id": ASSET_IMPORT_ACTION_ID,
        "filetypes": ["csv"],
        "max_files": 1,
    }
    assert calls[2][0] == "respond"


def test_asset_upload_submission_imports_and_sends_private_summary() -> None:
    calls: list[tuple[str, Any]] = []
    uploaded_file = {"id": "F123", "name": "assets.csv"}

    class FakeImporter:
        def import_slack_file(
            self,
            file: dict[str, Any],
            client: Any,
        ) -> AssetImportSummary:
            assert file == uploaded_file
            assert isinstance(client, FakeClient)
            return AssetImportSummary(
                total=2,
                added=1,
                deleted=1,
                already_present=0,
                failed=0,
                issues=(),
            )

    class FakeClient:
        def chat_postEphemeral(self, **arguments: Any) -> None:
            calls.append(("message", arguments))

    handle_asset_import_submission(
        lambda: calls.append(("ack", None)),
        {"user": {"id": "U123"}},
        {
            "private_metadata": "C456",
            "state": {
                "values": {
                    ASSET_IMPORT_BLOCK_ID: {
                        ASSET_IMPORT_ACTION_ID: {
                            "type": "file_input",
                            "files": [uploaded_file],
                        }
                    }
                }
            },
        },
        FakeClient(),
        FakeImporter(),  # type: ignore[arg-type]
    )

    assert calls[0] == ("ack", None)
    assert calls[1][0] == "message"
    assert calls[1][1]["channel"] == "C456"
    assert calls[1][1]["user"] == "U123"
    assert "Added: 1" in calls[1][1]["text"]
    assert "Deleted: 1" in calls[1][1]["text"]


def test_tracker_upload_command_opens_a_csv_file_modal_for_admin() -> None:
    calls: list[tuple[str, Any]] = []

    class FakeClient:
        def views_open(self, **arguments: Any) -> None:
            calls.append(("modal", arguments))

    handle_slash_command(
        lambda: calls.append(("ack", None)),
        lambda message: calls.append(("respond", message)),
        {
            "command": "/swingengine",
            "text": "tracker upload",
            "channel_id": "C456",
            "user_id": "UADMIN",
            "trigger_id": "trigger-456",
        },
        build_router(),
        authorized_user_id="UADMIN",
        client=FakeClient(),
        tracker_importer=object(),  # type: ignore[arg-type]
    )

    assert calls[0] == ("ack", None)
    assert calls[1][0] == "modal"
    modal = calls[1][1]["view"]
    assert modal["callback_id"] == TRACKER_IMPORT_CALLBACK_ID
    assert modal["private_metadata"] == "C456"
    assert "amount_allocated > 5000" in modal["blocks"][0]["text"]["text"]
    assert modal["blocks"][1]["element"] == {
        "type": "file_input",
        "action_id": TRACKER_IMPORT_ACTION_ID,
        "filetypes": ["csv"],
        "max_files": 1,
    }


def test_tracker_upload_command_is_restricted_to_admin() -> None:
    calls: list[tuple[str, Any]] = []

    handle_slash_command(
        lambda: calls.append(("ack", None)),
        lambda message: calls.append(("respond", message)),
        {
            "command": "/swingengine",
            "text": "tracker upload",
            "channel_id": "C456",
            "user_id": "UOTHER",
            "trigger_id": "trigger-456",
        },
        build_router(),
        authorized_user_id="UADMIN",
    )

    assert calls[0] == ("ack", None)
    assert calls[1][0] == "respond"
    assert "not authorized" in calls[1][1]["text"]


def test_tracker_upload_submission_updates_and_sends_private_summary() -> None:
    calls: list[tuple[str, Any]] = []
    uploaded_file = {"id": "F789", "name": "tracker-list.csv"}

    class FakeImporter:
        def import_slack_file(
            self,
            file: dict[str, Any],
            client: Any,
        ) -> TrackerImportSummary:
            assert file == uploaded_file
            assert isinstance(client, FakeClient)
            return TrackerImportSummary(
                total=1,
                updated=1,
                failed=0,
                issues=(),
            )

    class FakeClient:
        def chat_postEphemeral(self, **arguments: Any) -> None:
            calls.append(("message", arguments))

    handle_tracker_import_submission(
        lambda: calls.append(("ack", None)),
        {"user": {"id": "UADMIN"}},
        {
            "private_metadata": "C456",
            "state": {
                "values": {
                    TRACKER_IMPORT_BLOCK_ID: {
                        TRACKER_IMPORT_ACTION_ID: {
                            "type": "file_input",
                            "files": [uploaded_file],
                        }
                    }
                }
            },
        },
        FakeClient(),
        FakeImporter(),  # type: ignore[arg-type]
        authorized_user_id="UADMIN",
    )

    assert calls[0] == ("ack", None)
    assert calls[1][0] == "message"
    assert calls[1][1]["channel"] == "C456"
    assert calls[1][1]["user"] == "UADMIN"
    assert "Updated: 1" in calls[1][1]["text"]


def test_tracker_upload_submission_rechecks_admin_identity() -> None:
    calls: list[tuple[str, Any]] = []

    class FakeClient:
        def chat_postEphemeral(self, **arguments: Any) -> None:
            calls.append(("message", arguments))

    handle_tracker_import_submission(
        lambda: calls.append(("ack", None)),
        {"user": {"id": "UOTHER"}},
        {"private_metadata": "C456"},
        FakeClient(),
        object(),  # type: ignore[arg-type]
        authorized_user_id="UADMIN",
    )

    assert calls[0] == ("ack", None)
    assert calls[1][0] == "message"
    assert "not authorized" in calls[1][1]["text"]
