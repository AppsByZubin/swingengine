"""Slack Bolt application wiring."""

import logging
from collections.abc import Callable, Mapping
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError, SlackClientError

from database.config import DatabaseSettings
from database.repository import AssetTrackerRepository
from slack.commands import (
    ASSET_IMPORT_MODAL_KEY,
    FILE_UPLOAD_KEY,
    CommandRouter,
    build_router,
    ephemeral,
)
from slack.config import Settings
from slack.file_exports import (
    CsvFileExporter,
    FileDirectories,
    SlackFileUpload,
    configured_files_directory,
)
from slack.file_imports import AssetImportError, CsvAssetImporter
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

ASSET_IMPORT_CALLBACK_ID = "swingengine_asset_upload"
ASSET_IMPORT_BLOCK_ID = "asset_csv"
ASSET_IMPORT_ACTION_ID = "asset_csv_file"


def handle_slash_command(
    ack: Callable[[], Any],
    respond: Callable[[Mapping[str, Any]], Any],
    command: Mapping[str, Any],
    router: CommandRouter,
    authorized_user_id: str = "",
    client: Any = None,
    asset_importer: CsvAssetImporter | None = None,
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
    open_asset_import_modal = response.pop(ASSET_IMPORT_MODAL_KEY, False)
    if open_asset_import_modal:
        trigger_id = str(command.get("trigger_id", "")).strip()
        channel_id = str(command.get("channel_id", "")).strip()
        if asset_importer is None:
            respond(ephemeral("Asset CSV import is not configured."))
            return
        if client is None or not trigger_id or not channel_id:
            LOGGER.error(
                "Cannot open asset import modal: client_available=%r "
                "trigger_id_available=%r channel_id=%r",
                client is not None,
                bool(trigger_id),
                channel_id,
            )
            respond(ephemeral(":warning: Unable to open the CSV upload dialog."))
            return
        try:
            client.views_open(
                trigger_id=trigger_id,
                view=asset_import_modal(channel_id),
            )
        except SlackApiError as error:
            error_code = str(error.response.get("error", "unknown_error"))
            LOGGER.error(
                "Slack rejected asset import modal channel_id=%r error=%r",
                channel_id,
                error_code,
            )
            respond(
                ephemeral(
                    ":warning: Slack could not open the CSV upload dialog "
                    f"(`{error_code}`)."
                )
            )
            return
        except SlackClientError:
            LOGGER.exception(
                "Could not open asset import modal channel_id=%r",
                channel_id,
            )
            respond(ephemeral(":warning: Unable to open the CSV upload dialog."))
            return
        respond(response)
        return

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


def asset_import_modal(channel_id: str) -> dict[str, Any]:
    """Build the modal used to select one asset action CSV."""
    return {
        "type": "modal",
        "callback_id": ASSET_IMPORT_CALLBACK_ID,
        "private_metadata": channel_id,
        "title": {"type": "plain_text", "text": "Import assets"},
        "submit": {"type": "plain_text", "text": "Import"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "Upload one UTF-8 CSV (max 1 MB / 1,000 rows) with "
                        "exactly these columns: `name,action`. Actions must "
                        "be `add` or `delete`."
                    ),
                },
            },
            {
                "type": "input",
                "block_id": ASSET_IMPORT_BLOCK_ID,
                "label": {
                    "type": "plain_text",
                    "text": "Asset action CSV",
                },
                "element": {
                    "type": "file_input",
                    "action_id": ASSET_IMPORT_ACTION_ID,
                    "filetypes": ["csv"],
                    "max_files": 1,
                },
            },
        ],
    }


def handle_asset_import_submission(
    ack: Callable[[], Any],
    body: Mapping[str, Any],
    view: Mapping[str, Any],
    client: Any,
    asset_importer: CsvAssetImporter | None,
) -> None:
    """Acknowledge an upload modal and report the completed CSV import."""
    ack()
    channel_id = str(view.get("private_metadata", "")).strip()
    user = body.get("user")
    user_id = (
        str(user.get("id", "")).strip()
        if isinstance(user, Mapping)
        else ""
    )
    if not channel_id or not user_id:
        LOGGER.error(
            "Asset import submission is missing destination channel_id=%r "
            "user_id=%r",
            channel_id,
            user_id,
        )
        return
    if asset_importer is None:
        _post_ephemeral(
            client,
            channel_id,
            user_id,
            "Asset CSV import is not configured.",
        )
        return

    uploaded_files = _asset_import_files(view)
    if len(uploaded_files) != 1:
        _post_ephemeral(
            client,
            channel_id,
            user_id,
            ":warning: Upload exactly one CSV file.",
        )
        return

    try:
        summary = asset_importer.import_slack_file(uploaded_files[0], client)
    except AssetImportError as error:
        _post_ephemeral(
            client,
            channel_id,
            user_id,
            f":warning: {error}",
        )
        return
    _post_ephemeral(
        client,
        channel_id,
        user_id,
        summary.slack_message(),
    )


def _asset_import_files(
    view: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    state = view.get("state")
    values = state.get("values") if isinstance(state, Mapping) else None
    block = (
        values.get(ASSET_IMPORT_BLOCK_ID)
        if isinstance(values, Mapping)
        else None
    )
    action = (
        block.get(ASSET_IMPORT_ACTION_ID)
        if isinstance(block, Mapping)
        else None
    )
    files = action.get("files") if isinstance(action, Mapping) else None
    if not isinstance(files, list):
        return []
    return [file for file in files if isinstance(file, Mapping)]


def _post_ephemeral(
    client: Any,
    channel_id: str,
    user_id: str,
    text: str,
) -> None:
    try:
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text=text,
        )
    except SlackApiError as error:
        error_code = str(error.response.get("error", "unknown_error"))
        LOGGER.error(
            "Slack rejected asset import response channel_id=%r user_id=%r "
            "error=%r",
            channel_id,
            user_id,
            error_code,
        )
    except SlackClientError:
        LOGGER.exception(
            "Could not send asset import response channel_id=%r user_id=%r",
            channel_id,
            user_id,
        )


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


def create_app(
    settings: Settings,
    router: CommandRouter | None = None,
    asset_importer: CsvAssetImporter | None = None,
) -> App:
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
            asset_importer,
        )

    def asset_import_listener(
        ack: Callable[[], Any],
        body: Any,
        view: Any,
        client: Any,
    ) -> None:
        handle_asset_import_submission(
            ack,
            body,
            view,
            client,
            asset_importer,
        )

    slack_app.command(settings.slash_command)(listener)
    slack_app.view(ASSET_IMPORT_CALLBACK_ID)(asset_import_listener)
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
    asset_importer = CsvAssetImporter(
        file_directories.input,
        asset_catalog,
        asset_tracker_repository,
        settings.bot_token,
    )
    slack_app = create_app(
        settings,
        build_router(
            token_service,
            asset_catalog,
            asset_tracker_repository,
            file_exporter,
        ),
        asset_importer,
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
