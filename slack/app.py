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
    TRACKER_IMPORT_MODAL_KEY,
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
from slack.file_imports import (
    AssetImportError,
    CsvAssetImporter,
    CsvTrackerImporter,
    TrackerImportError,
)
from slack.notifier import SlackTokenNotifier
from tracker.config import TrackerEvaluationSettings
from tracker.evaluator import TrackerMomentumEvaluator
from tracker.momentum_scanner import NSEMomentumScanner
from tracker.scheduler import TrackerEvaluationScheduler
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
TRACKER_IMPORT_CALLBACK_ID = "swingengine_tracker_upload"
TRACKER_IMPORT_BLOCK_ID = "tracker_csv"
TRACKER_IMPORT_ACTION_ID = "tracker_csv_file"


def handle_slash_command(
    ack: Callable[[], Any],
    respond: Callable[[Mapping[str, Any]], Any],
    command: Mapping[str, Any],
    router: CommandRouter,
    authorized_user_id: str = "",
    client: Any = None,
    asset_importer: CsvAssetImporter | None = None,
    tracker_importer: CsvTrackerImporter | None = None,
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
    if _is_momentum_list_file(command_text):
        respond(
            ephemeral(
                ":hourglass_flowing_sand: NSE momentum scan started. "
                "A full rate-limited scan can take tens of minutes; the "
                "CSV will be uploaded to this conversation when ready."
            )
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
    if _is_tracker_upload(command_text) and (
        not authorized_user_id
        or command.get("user_id") != authorized_user_id
    ):
        respond(
            ephemeral(
                "You are not authorized to update tracker approvals and "
                "allocations."
            )
        )
        return

    response = dict(router.dispatch(command_text))
    open_asset_import_modal = response.pop(ASSET_IMPORT_MODAL_KEY, False)
    open_tracker_import_modal = response.pop(
        TRACKER_IMPORT_MODAL_KEY,
        False,
    )
    if open_asset_import_modal or open_tracker_import_modal:
        trigger_id = str(command.get("trigger_id", "")).strip()
        channel_id = str(command.get("channel_id", "")).strip()
        importer = (
            asset_importer
            if open_asset_import_modal
            else tracker_importer
        )
        import_name = "Asset" if open_asset_import_modal else "Tracker"
        if importer is None:
            respond(ephemeral(f"{import_name} CSV import is not configured."))
            return
        if client is None or not trigger_id or not channel_id:
            LOGGER.error(
                "Cannot open %s import modal: client_available=%r "
                "trigger_id_available=%r channel_id=%r",
                import_name.casefold(),
                client is not None,
                bool(trigger_id),
                channel_id,
            )
            respond(ephemeral(":warning: Unable to open the CSV upload dialog."))
            return
        try:
            client.views_open(
                trigger_id=trigger_id,
                view=(
                    asset_import_modal(channel_id)
                    if open_asset_import_modal
                    else tracker_import_modal(channel_id)
                ),
            )
        except SlackApiError as error:
            error_code = str(error.response.get("error", "unknown_error"))
            LOGGER.error(
                "Slack rejected %s import modal channel_id=%r error=%r",
                import_name.casefold(),
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
                "Could not open %s import modal channel_id=%r",
                import_name.casefold(),
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
        LOGGER.info(
            "Starting Slack CSV upload channel_id=%r filename=%r path=%s",
            channel_id,
            upload.path.name,
            upload.path,
        )
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

    LOGGER.info(
        "Completed Slack CSV upload channel_id=%r filename=%r path=%s",
        channel_id,
        upload.path.name,
        upload.path,
    )

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


def tracker_import_modal(channel_id: str) -> dict[str, Any]:
    """Build the modal used to select one tracker update CSV."""
    return {
        "type": "modal",
        "callback_id": TRACKER_IMPORT_CALLBACK_ID,
        "private_metadata": channel_id,
        "title": {"type": "plain_text", "text": "Update tracker"},
        "submit": {"type": "plain_text", "text": "Update"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "Upload an exported tracker CSV (max 1 MB / 1,000 "
                        "rows). Only `is_approved_for_trade` and "
                        "`amount_allocated` are updated. Approval requires "
                        "`amount_allocated > 5000`."
                    ),
                },
            },
            {
                "type": "input",
                "block_id": TRACKER_IMPORT_BLOCK_ID,
                "label": {
                    "type": "plain_text",
                    "text": "Tracker update CSV",
                },
                "element": {
                    "type": "file_input",
                    "action_id": TRACKER_IMPORT_ACTION_ID,
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
    _handle_csv_import_submission(
        body,
        view,
        client,
        asset_importer,
        AssetImportError,
        ASSET_IMPORT_BLOCK_ID,
        ASSET_IMPORT_ACTION_ID,
        "Asset",
    )


def handle_tracker_import_submission(
    ack: Callable[[], Any],
    body: Mapping[str, Any],
    view: Mapping[str, Any],
    client: Any,
    tracker_importer: CsvTrackerImporter | None,
    authorized_user_id: str,
) -> None:
    """Apply an admin-submitted tracker CSV and send a private summary."""
    ack()
    _handle_csv_import_submission(
        body,
        view,
        client,
        tracker_importer,
        TrackerImportError,
        TRACKER_IMPORT_BLOCK_ID,
        TRACKER_IMPORT_ACTION_ID,
        "Tracker",
        authorized_user_id,
    )


def _handle_csv_import_submission(
    body: Mapping[str, Any],
    view: Mapping[str, Any],
    client: Any,
    importer: Any,
    error_type: type[RuntimeError],
    block_id: str,
    action_id: str,
    import_name: str,
    authorized_user_id: str | None = None,
) -> None:
    channel_id = str(view.get("private_metadata", "")).strip()
    user = body.get("user")
    user_id = (
        str(user.get("id", "")).strip()
        if isinstance(user, Mapping)
        else ""
    )
    if not channel_id or not user_id:
        LOGGER.error(
            "%s import submission is missing destination channel_id=%r "
            "user_id=%r",
            import_name,
            channel_id,
            user_id,
        )
        return
    if authorized_user_id is not None and (
        not authorized_user_id or user_id != authorized_user_id
    ):
        _post_ephemeral(
            client,
            channel_id,
            user_id,
            "You are not authorized to update tracker approvals and "
            "allocations.",
        )
        return
    if importer is None:
        _post_ephemeral(
            client,
            channel_id,
            user_id,
            f"{import_name} CSV import is not configured.",
        )
        return

    uploaded_files = _import_files(view, block_id, action_id)
    if len(uploaded_files) != 1:
        _post_ephemeral(
            client,
            channel_id,
            user_id,
            ":warning: Upload exactly one CSV file.",
        )
        return

    try:
        summary = importer.import_slack_file(
            uploaded_files[0],
            client,
        )
    except error_type as error:
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


def _import_files(
    view: Mapping[str, Any],
    block_id: str,
    action_id: str,
) -> list[Mapping[str, Any]]:
    state = view.get("state")
    values = state.get("values") if isinstance(state, Mapping) else None
    block = (
        values.get(block_id)
        if isinstance(values, Mapping)
        else None
    )
    action = (
        block.get(action_id)
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
            "Slack rejected CSV import response channel_id=%r user_id=%r "
            "error=%r",
            channel_id,
            user_id,
            error_code,
        )
    except SlackClientError:
        LOGGER.exception(
            "Could not send CSV import response channel_id=%r user_id=%r",
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


def _is_tracker_upload(text: str) -> bool:
    parts = text.split()
    return (
        len(parts) == 2
        and parts[0].casefold() == "tracker"
        and parts[1].casefold() == "upload"
    )


def _is_momentum_list_file(text: str) -> bool:
    parts = text.split()
    return (
        len(parts) == 3
        and parts[0].casefold() == "momentum"
        and parts[1].casefold() == "list"
        and parts[2].casefold() == "file"
    )


def create_app(
    settings: Settings,
    router: CommandRouter | None = None,
    asset_importer: CsvAssetImporter | None = None,
    tracker_importer: CsvTrackerImporter | None = None,
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
            tracker_importer,
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

    def tracker_import_listener(
        ack: Callable[[], Any],
        body: Any,
        view: Any,
        client: Any,
    ) -> None:
        handle_tracker_import_submission(
            ack,
            body,
            view,
            client,
            tracker_importer,
            settings.alert_user_id,
        )

    slack_app.command(settings.slash_command)(listener)
    slack_app.view(ASSET_IMPORT_CALLBACK_ID)(asset_import_listener)
    slack_app.view(TRACKER_IMPORT_CALLBACK_ID)(tracker_import_listener)
    return slack_app


def run() -> None:
    """Start token services and the blocking Socket Mode listener."""
    settings = Settings.from_env()
    upstox_settings = UpstoxSettings.from_env()
    asset_settings = AssetCatalogSettings.from_env()
    database_settings = DatabaseSettings.from_env()
    tracker_evaluation_settings = TrackerEvaluationSettings.from_env()
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
    tracker_evaluator = TrackerMomentumEvaluator(
        tracker_evaluation_settings,
        asset_tracker_repository,
        auth_client,
        token_store,
    )
    momentum_scanner = NSEMomentumScanner(
        tracker_evaluation_settings,
        asset_catalog,
        auth_client,
        token_store,
    )
    asset_importer = CsvAssetImporter(
        file_directories.input,
        asset_catalog,
        asset_tracker_repository,
        settings.bot_token,
    )
    tracker_importer = CsvTrackerImporter(
        file_directories.input,
        asset_tracker_repository,
        settings.bot_token,
    )
    slack_app = create_app(
        settings,
        build_router(
            auth_service=token_service,
            asset_service=asset_catalog,
            tracker_service=asset_tracker_repository,
            file_exporter=file_exporter,
            evaluation_service=tracker_evaluator,
            momentum_service=momentum_scanner,
        ),
        asset_importer,
        tracker_importer,
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
    tracker_scheduler = TrackerEvaluationScheduler(
        tracker_evaluation_settings,
        tracker_evaluator,
    )
    webhook = UpstoxWebhookServer(upstox_settings, token_service)

    LOGGER.info("Starting Slack listener for %s", settings.slash_command)
    if upstox_settings.credential_errors:
        LOGGER.error(
            "Upstox token management is not configured: %s",
            "; ".join(upstox_settings.credential_errors),
        )

    webhook.start()
    scheduler.start()
    tracker_scheduler.start()
    monitor.start()
    try:
        SocketModeHandler(slack_app, settings.app_token).start()
    finally:
        monitor.stop()
        tracker_scheduler.stop()
        scheduler.stop()
        webhook.stop()
