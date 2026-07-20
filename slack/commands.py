"""Pure command parsing and dispatch for the Slack interface."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

SlackResponse = dict[str, Any]
CommandHandler = Callable[[str], SlackResponse]


def ephemeral(text: str) -> SlackResponse:
    """Build a response visible only to the user who ran the command."""
    return {"response_type": "ephemeral", "text": text}


def help_command(_: str = "") -> SlackResponse:
    return ephemeral(
        "*SwingEngine commands*\n"
        "• `/swingengine help` — show this help\n"
        "• `/swingengine ping` — test the Slack connection\n"
        "• `/swingengine status` — check whether the service is running"
    )


def ping_command(_: str = "") -> SlackResponse:
    return ephemeral("pong")


def status_command(_: str = "") -> SlackResponse:
    return ephemeral(":large_green_circle: SwingEngine is running.")


@dataclass
class CommandRouter:
    """Map Slack subcommands to independently testable handlers."""

    handlers: dict[str, CommandHandler] = field(default_factory=dict)

    def register(self, name: str, handler: CommandHandler) -> None:
        normalized_name = name.strip().casefold()
        if not normalized_name or any(char.isspace() for char in normalized_name):
            raise ValueError("Command names must be one non-empty word")
        self.handlers[normalized_name] = handler

    def dispatch(self, text: str | None) -> SlackResponse:
        parts = (text or "").split(maxsplit=1)
        if not parts:
            return self.handlers["help"]("")

        command_name = parts[0].casefold()
        arguments = parts[1] if len(parts) == 2 else ""

        handler = self.handlers.get(command_name)
        if handler is None:
            return ephemeral(
                f"Unknown command `{command_name}`. "
                "Run `/swingengine help` to see available commands."
            )

        return handler(arguments.strip())


def build_router() -> CommandRouter:
    router = CommandRouter()
    router.register("help", help_command)
    router.register("ping", ping_command)
    router.register("status", status_command)
    return router
