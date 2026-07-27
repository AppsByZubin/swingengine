"""Pure command parsing and dispatch for the Slack interface."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from upstox.assets import AssetCatalogError, AssetSearchResult

SlackResponse = dict[str, Any]
CommandHandler = Callable[[str], SlackResponse]


class TokenAuthService(Protocol):
    def status_message(self) -> str:
        """Return a credential-free description of Upstox auth state."""

    def set_token_message(self, access_token: str) -> str:
        """Validate, persist, and describe a manually supplied token."""


class AssetService(Protocol):
    def refresh(self) -> int:
        """Refresh the local catalog and return its asset count."""

    def search(self, query: str) -> list[AssetSearchResult]:
        """Find assets related to the supplied query."""


def ephemeral(text: str) -> SlackResponse:
    """Build a response visible only to the user who ran the command."""
    return {"response_type": "ephemeral", "text": text}


def help_command(_: str = "") -> SlackResponse:
    return ephemeral(
        "*SwingEngine commands*\n\n"
        "• `/swingengine` or `/swingengine help` — show this help\n"
        "• `/swingengine ping` — test the Slack connection\n"
        "• `/swingengine status` — check service and Upstox authorization\n"
        "• `/swingengine auth` or `/swingengine auth status` — check the "
        "stored Upstox token\n"
        "• `/swingengine auth set <token>` — validate and store a new token\n\n"
        "*Assets*\n"
        "• `/swingengine asset refresh` — download the latest NSE assets\n"
        "• `/swingengine asset search <query>` — find NSE assets by name, "
        "symbol, key, or ISIN\n\n"
        "*Disabled workflow*\n"
        "• `/swingengine auth request` — unavailable until the Upstox "
        "notifier webhook is enabled"
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
        return ephemeral("Upstox token management is not configured.")

    parts = arguments.strip().split(maxsplit=1)
    action = parts[0].casefold() if parts else "status"
    if action == "status":
        return ephemeral(auth_service.status_message())
    if action == "set":
        if len(parts) != 2:
            return ephemeral(
                "Provide the token: `/swingengine auth set <token>`."
            )
        return ephemeral(auth_service.set_token_message(parts[1]))
    if action == "request":
        return ephemeral(
            "Semi-automated token requests are disabled. Generate a token in "
            "Upstox, then use `/swingengine auth set <token>`."
        )
    return ephemeral(
        "Unknown auth action. Use `/swingengine auth status` or "
        "`/swingengine auth set <token>`."
    )


def asset_command(
    arguments: str = "", asset_service: AssetService | None = None
) -> SlackResponse:
    if asset_service is None:
        return ephemeral("Upstox asset search is not configured.")

    parts = arguments.strip().split(maxsplit=1)
    action = parts[0].casefold() if parts else ""
    if action == "refresh":
        if len(parts) != 1:
            return ephemeral("Use `/swingengine asset refresh`.")
        try:
            asset_count = asset_service.refresh()
        except AssetCatalogError as error:
            return ephemeral(f":warning: {error}")
        return ephemeral(
            f":white_check_mark: Refreshed {asset_count:,} NSE assets."
        )

    if action == "search":
        if len(parts) != 2 or not parts[1].strip():
            return ephemeral(
                "Provide a search term: "
                "`/swingengine asset search <query>`."
            )
        query = parts[1].strip()
        try:
            matches = asset_service.search(query)
        except AssetCatalogError as error:
            return ephemeral(f":warning: {error}")
        if not matches:
            return ephemeral(f"No NSE assets found for `{_code_text(query)}`.")

        lines = [
            f"*NSE assets matching `{_code_text(query)}`*",
            *(_format_asset(match) for match in matches),
        ]
        return ephemeral("\n".join(lines))

    return ephemeral(
        "Unknown asset action. Use `/swingengine asset refresh` or "
        "`/swingengine asset search <query>`."
    )


def _format_asset(asset: AssetSearchResult) -> str:
    symbol = asset.trading_symbol or "(no trading symbol)"
    name = asset.name
    identity = asset.segment
    if asset.instrument_type:
        identity = (
            f"{identity} · {asset.instrument_type}"
            if identity
            else asset.instrument_type
        )
    detail = (
        f" — {_slack_text(name)}" if name and name != symbol else ""
    )
    metadata = f" ({_slack_text(identity)})" if identity else ""
    key = (
        f" · `{_code_text(asset.instrument_key)}`"
        if asset.instrument_key
        else ""
    )
    return f"• `{_code_text(symbol)}`{detail}{metadata}{key}"


def _slack_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _code_text(value: str) -> str:
    return _slack_text(value).replace("`", "'")


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
    asset_service: AssetService | None = None,
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
    router.register(
        "asset",
        lambda arguments: asset_command(arguments, asset_service),
    )
    return router
