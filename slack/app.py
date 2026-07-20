"""Slack Bolt application wiring."""

import logging
from collections.abc import Callable, Mapping
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from slack.commands import CommandRouter, build_router
from slack.config import Settings

LOGGER = logging.getLogger(__name__)


def handle_slash_command(
    ack: Callable[[], Any],
    respond: Callable[[Mapping[str, Any]], Any],
    command: Mapping[str, Any],
    router: CommandRouter,
) -> None:
    """Acknowledge Slack immediately, then route the command text."""
    ack()
    respond(router.dispatch(str(command.get("text", ""))))


def create_app(settings: Settings, router: CommandRouter | None = None) -> App:
    """Create and configure the Bolt app without starting it."""
    slack_app = App(token=settings.bot_token)
    command_router = router or build_router()

    def listener(ack: Callable[[], Any], respond: Any, command: Any) -> None:
        handle_slash_command(ack, respond, command, command_router)

    slack_app.command(settings.slash_command)(listener)
    return slack_app


def run() -> None:
    """Start the blocking Socket Mode connection."""
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    LOGGER.info("Starting Slack listener for %s", settings.slash_command)
    SocketModeHandler(create_app(settings), settings.app_token).start()

