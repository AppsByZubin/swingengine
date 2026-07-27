"""Slack Bolt application wiring."""

import logging
from collections.abc import Callable, Mapping
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from slack.commands import CommandRouter, build_router
from slack.config import Settings
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
    respond(router.dispatch(command_text))


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

    def listener(ack: Callable[[], Any], respond: Any, command: Any) -> None:
        handle_slash_command(
            ack,
            respond,
            command,
            command_router,
            settings.alert_user_id,
        )

    slack_app.command(settings.slash_command)(listener)
    return slack_app


def run() -> None:
    """Start token services and the blocking Socket Mode listener."""
    settings = Settings.from_env()
    upstox_settings = UpstoxSettings.from_env()
    asset_settings = AssetCatalogSettings.from_env()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    token_store = TokenStore(upstox_settings.token_file)
    auth_client = UpstoxAuthClient(upstox_settings)
    token_service = TokenRotationService(
        upstox_settings, token_store, auth_client
    )
    asset_catalog = AssetCatalog(asset_settings)
    slack_app = create_app(
        settings, build_router(token_service, asset_catalog)
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
