"""Slack Bolt application wiring."""

import logging
from collections.abc import Callable, Mapping
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError, SlackClientError

from database.config import DatabaseSettings
from database.repository import AssetTrackerRepository
from slack.commands import FILE_UPLOAD_KEY, CommandRouter, build_router, ephemeral
from slack.config import Settings
from slack.file_exports import (
    CsvFileExporter,
    FileDirectories,
    SlackFileUpload,
    configured_files_directory,
)
from slack.notifier import SlackTokenNotifier
from upstox.assets import AssetCatalog, AssetCatalogSettings
from upstox.client import UpstoxAuthClient
from upstox.config import UpstoxSettings
from upstox.monitor import TokenHealthMonitor
from upstox.scheduler import TokenRequestScheduler
from upstox.service import TokenRotationService
from upstox.store import TokenStore
from upstox.webhook import UpstoxWebhookServer

LOGGER = logging.getLogger(__name__)


def handle_slash_command(
    ack: Callable[[], Any],
    respond: Callable[[Mapping[str, Any]], Any],
    command: Mapping[str, Any],
    router: CommandRouter,
    authorized_user_id: str = "",
    client: Any = None,
) -> None:
    """Acknowledge Slack immediately, then route the command text."""
    ack()
    command_text = str(command.get("text", ""))
    LOGGER.info(
        "Received Slack command command=%r text=%r user_id=%r channel_id=%r",
        command.get("command"),
        _redact_sensitive_command(command_text),
        command.get("user_id"),
        command.get("channel_id"),
    )
    if (
        _is_auth_set(command_text)
        and authorized_user_id
        and command.get("user_id") != authorized_user_id
    ):
        respond(
            {
                "response_type": "ephemeral",
                "text": "You are not authorized to replace the Upstox token.",
            }
        )
        return
    response = dict(router.dispatch(command_text))
    upload = response.pop(FILE_UPLOAD_KEY, None)
    if upload is None:
        respond(response)
        return

    if not isinstance(upload, SlackFileUpload):
        LOGGER.error("Command returned an invalid Slack file upload request")
        respond(ephemeral(":warning: Unable to prepare the CSV upload."))
        return

    channel_id = str(command.get("channel_id", "")).strip()
    if client is None or not channel_id:
        LOGGER.error(
            "Cannot upload Slack file: client_available=%r channel_id=%r",
            client is not None,
            channel_id,
        )
        respond(ephemeral(":warning: Unable to upload the CSV to Slack."))
        return

    try:
        client.files_upload_v2(
            channel=channel_id,
            file=str(upload.path),
            filename=upload.path.name,
            title=upload.title,
            initial_comment=upload.initial_comment,
        )
    except SlackApiError as error:
        error_code = str(error.response.get("error", "unknown_error"))
        LOGGER.error(
            "Slack rejected CSV upload channel_id=%r error=%r",
            channel_id,
            error_code,
        )
        respond(
            ephemeral(
                ":warning: Slack could not upload the CSV "
                f"(`{error_code}`). Make sure SwingEngine is in this channel."
            )
        )
        return
    except (SlackClientError, OSError):
        LOGGER.exception("Could not upload CSV to Slack channel_id=%r", channel_id)
        respond(ephemeral(":warning: Unable to upload the CSV to Slack."))
        return

    respond(response)


def _redact_sensitive_command(text: str) -> str:
    if _is_auth_set(text):
        return "auth set [REDACTED]"
    return text


def _is_auth_set(text: str) -> bool:
    parts = text.split(maxsplit=2)
    return (
        len(parts) >= 2
        and parts[0].casefold() == "auth"
        and parts[1].casefold() == "set"
    )


def create_app(settings: Settings, router: CommandRouter | None = None) -> App:
    """Create and configure the Bolt app without starting it."""
    slack_app = App(token=settings.bot_token)
    command_router = router or build_router()

    def listener(
        ack: Callable[[], Any],
        respond: Any,
        command: Any,
        client: Any,
    ) -> None:
        handle_slash_command(
            ack,
            respond,
            command,
            command_router,
            settings.alert_user_id,
            client,
        )

    slack_app.command(settings.slash_command)(listener)
    return slack_app


def run() -> None:
    """Start token services and the blocking Socket Mode listener."""
    settings = Settings.from_env()
    upstox_settings = UpstoxSettings.from_env()
    asset_settings = AssetCatalogSettings.from_env()
    database_settings = DatabaseSettings.from_env()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    file_directories = FileDirectories.create(configured_files_directory())
    file_exporter = CsvFileExporter(file_directories.output)
    LOGGER.info(
        "Initialized file directories input=%s output=%s",
        file_directories.input,
        file_directories.output,
    )
    token_store = TokenStore(upstox_settings.token_file)
    auth_client = UpstoxAuthClient(upstox_settings)
    token_service = TokenRotationService(
        upstox_settings, token_store, auth_client
    )
    asset_catalog = AssetCatalog(asset_settings)
    asset_tracker_repository = AssetTrackerRepository(database_settings)
    slack_app = create_app(
        settings,
        build_router(
            token_service,
            asset_catalog,
            asset_tracker_repository,
            file_exporter,
        ),
    )
    notifier = SlackTokenNotifier(
        slack_app.client,
        settings.alert_user_id,
    )
    monitor = TokenHealthMonitor(
        upstox_settings,
        token_service,
        notifier.notify,
    )
    scheduler = TokenRequestScheduler(upstox_settings, token_service)
    webhook = UpstoxWebhookServer(upstox_settings, token_service)

    LOGGER.info("Starting Slack listener for %s", settings.slash_command)
    if upstox_settings.credential_errors:
        LOGGER.error(
            "Upstox token management is not configured: %s",
            "; ".join(upstox_settings.credential_errors),
        )

    webhook.start()
    scheduler.start()
    monitor.start()
    try:
        SocketModeHandler(slack_app, settings.app_token).start()
    finally:
        monitor.stop()
        scheduler.stop()
        webhook.stop()
