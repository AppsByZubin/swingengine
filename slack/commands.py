"""Pure command parsing and dispatch for the Slack interface."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

SlackResponse = dict[str, Any]
CommandHandler = Callable[[str], SlackResponse]


class TokenAuthService(Protocol):
    def status_message(self) -> str:
        """Return a credential-free description of Upstox auth state."""

    def request_token_message(self, force: bool = True) -> str:
        """Request a new user-approved token and describe the result."""


def ephemeral(text: str) -> SlackResponse:
    """Build a response visible only to the user who ran the command."""
    return {"response_type": "ephemeral", "text": text}


def help_command(_: str = "") -> SlackResponse:
    return ephemeral(
        "*SwingEngine commands*\n"
        "• `/swingengine help` — show this help\n"
        "• `/swingengine ping` — test the Slack connection\n"
        "• `/swingengine status` — check service and Upstox authorization\n"
        "• `/swingengine auth status` — check Upstox authorization\n"
        "• `/swingengine auth request` — request Upstox approval now"
    )


def ping_command(_: str = "") -> SlackResponse:
    return ephemeral("pong")


def status_command(
    _: str = "", auth_service: TokenAuthService | None = None
) -> SlackResponse:
    text = ":large_green_circle: SwingEngine is running."
    if auth_service is not None:
        text += f"\n{auth_service.status_message()}"
    return ephemeral(text)


def auth_command(
    arguments: str = "", auth_service: TokenAuthService | None = None
) -> SlackResponse:
    if auth_service is None:
        return ephemeral("Upstox token rotation is not configured.")

    action = arguments.strip().casefold() or "status"
    if action == "status":
        return ephemeral(auth_service.status_message())
    if action == "request":
        return ephemeral(auth_service.request_token_message(force=True))
    return ephemeral(
        "Unknown auth action. Use `/swingengine auth status` or "
        "`/swingengine auth request`."
    )


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


def build_router(
    auth_service: TokenAuthService | None = None,
) -> CommandRouter:
    router = CommandRouter()
    router.register("help", help_command)
    router.register("ping", ping_command)
    router.register(
        "status",
        lambda arguments: status_command(arguments, auth_service),
    )
    router.register(
        "auth",
        lambda arguments: auth_command(arguments, auth_service),
    )
    return router
