"""Pure command parsing and dispatch for the Slack interface."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from database.repository import (
    AssetAlreadyExistsError,
    AssetInUseError,
    AssetNotFoundError,
    AssetRecord,
    RepositoryError,
    TrackerAlreadyExistsError,
    TrackerEntry,
    TrackerNotFoundError,
)
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


class AssetTrackerService(Protocol):
    def add_asset(self, asset: AssetSearchResult) -> AssetRecord:
        """Save an asset selected from the NSE catalog."""

    def delete_asset(self, trading_symbol: str) -> AssetRecord:
        """Delete and return a saved asset."""

    def list_assets(self) -> list[AssetRecord]:
        """Return every saved asset."""

    def add_tracker(self, trading_symbol: str) -> TrackerEntry:
        """Add a saved asset to the tracker."""

    def delete_tracker(self, trading_symbol: str) -> TrackerEntry:
        """Delete and return an asset's tracker entry."""

    def list_tracker(self) -> list[TrackerEntry]:
        """Return tracker entries joined with their saved assets."""


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
        "*Instruments*\n"
        "• `/swingengine instrument refresh` — download the latest NSE "
        "instruments\n"
        "• `/swingengine instrument search <query>` — find NSE instruments "
        "by name, symbol, key, or ISIN\n"
        "\n*Assets*\n"
        "• `/swingengine asset add <trading_symbol>` — save an NSE asset\n"
        "• `/swingengine asset delete <trading_symbol>` — delete a saved "
        "asset\n"
        "• `/swingengine asset list` — list saved assets\n\n"
        "*Tracker*\n"
        "• `/swingengine tracker add <trading_symbol>` — start tracking a "
        "saved asset\n"
        "• `/swingengine tracker delete <trading_symbol>` — stop tracking a "
        "saved asset\n"
        "• `/swingengine tracker list` — list tracked assets\n\n"
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


def instrument_command(
    arguments: str = "",
    asset_service: AssetService | None = None,
) -> SlackResponse:
    parts = arguments.strip().split(maxsplit=1)
    action = parts[0].casefold() if parts else ""
    if action == "refresh":
        if asset_service is None:
            return ephemeral("Upstox instrument search is not configured.")
        if len(parts) != 1:
            return ephemeral("Use `/swingengine instrument refresh`.")
        try:
            asset_count = asset_service.refresh()
        except AssetCatalogError as error:
            return ephemeral(f":warning: {error}")
        return ephemeral(
            f":white_check_mark: Refreshed {asset_count:,} NSE instruments."
        )

    if action == "search":
        if asset_service is None:
            return ephemeral("Upstox instrument search is not configured.")
        if len(parts) != 2 or not parts[1].strip():
            return ephemeral(
                "Provide a search term: "
                "`/swingengine instrument search <query>`."
            )
        query = parts[1].strip()
        try:
            matches = asset_service.search(query)
        except AssetCatalogError as error:
            return ephemeral(f":warning: {error}")
        if not matches:
            return ephemeral(
                f"No NSE instruments found for `{_code_text(query)}`."
            )

        lines = [
            f"*NSE instruments matching `{_code_text(query)}`*",
            *(_format_asset(match) for match in matches),
        ]
        return ephemeral("\n".join(lines))

    return ephemeral(
        "Unknown instrument action. Use "
        "`/swingengine instrument refresh` or "
        "`/swingengine instrument search <query>`."
    )


def asset_command(
    arguments: str = "",
    asset_service: AssetService | None = None,
    tracker_service: AssetTrackerService | None = None,
) -> SlackResponse:
    parts = arguments.strip().split(maxsplit=1)
    action = parts[0].casefold() if parts else ""
    if action == "add":
        if asset_service is None:
            return ephemeral("Upstox instrument search is not configured.")
        if tracker_service is None:
            return ephemeral("Asset database is not configured.")
        if len(parts) != 2 or not parts[1].strip():
            return ephemeral(
                "Provide a trading symbol: "
                "`/swingengine asset add <trading_symbol>`."
            )

        symbol = parts[1].strip()
        try:
            matches = asset_service.search(symbol)
        except AssetCatalogError as error:
            return ephemeral(f":warning: {error}")
        asset = next(
            (
                match
                for match in matches
                if match.trading_symbol.casefold() == symbol.casefold()
            ),
            None,
        )
        if asset is None:
            return ephemeral(
                f"No exact NSE trading symbol found for "
                f"`{_code_text(symbol)}`."
            )

        try:
            saved_asset = tracker_service.add_asset(asset)
        except AssetAlreadyExistsError:
            return ephemeral(
                f"Asset `{_code_text(asset.trading_symbol)}` is already saved."
            )
        except RepositoryError as error:
            return ephemeral(f":warning: {error}")
        return ephemeral(
            f":white_check_mark: Saved asset "
            f"`{_code_text(saved_asset.trading_symbol)}`."
        )

    if action == "delete":
        if tracker_service is None:
            return ephemeral("Asset database is not configured.")
        if len(parts) != 2 or not parts[1].strip():
            return ephemeral(
                "Provide a trading symbol: "
                "`/swingengine asset delete <trading_symbol>`."
            )

        symbol = parts[1].strip()
        try:
            deleted_asset = tracker_service.delete_asset(symbol)
        except AssetNotFoundError:
            return ephemeral(
                f"Asset `{_code_text(symbol)}` is not saved."
            )
        except AssetInUseError:
            return ephemeral(
                f"Asset `{_code_text(symbol)}` is still tracked. Delete its "
                "tracker entry first."
            )
        except RepositoryError as error:
            return ephemeral(f":warning: {error}")
        return ephemeral(
            f":white_check_mark: Deleted asset "
            f"`{_code_text(deleted_asset.trading_symbol)}`."
        )

    if action == "list":
        if tracker_service is None:
            return ephemeral("Asset database is not configured.")
        if len(parts) != 1:
            return ephemeral("Use `/swingengine asset list`.")
        try:
            assets = tracker_service.list_assets()
        except RepositoryError as error:
            return ephemeral(f":warning: {error}")
        if not assets:
            return ephemeral("No assets have been saved.")
        return ephemeral(
            "\n".join(
                ["*Saved assets*", *(_format_saved_asset(asset) for asset in assets)]
            )
        )

    return ephemeral(
        "Unknown asset action. Use "
        "`/swingengine asset add|delete|list ...`."
    )


def tracker_command(
    arguments: str = "",
    tracker_service: AssetTrackerService | None = None,
) -> SlackResponse:
    if tracker_service is None:
        return ephemeral("Asset tracker database is not configured.")

    parts = arguments.strip().split(maxsplit=1)
    action = parts[0].casefold() if parts else ""
    if action == "add":
        if len(parts) != 2 or not parts[1].strip():
            return ephemeral(
                "Provide a trading symbol: "
                "`/swingengine tracker add <trading_symbol>`."
            )
        symbol = parts[1].strip()
        try:
            entry = tracker_service.add_tracker(symbol)
        except AssetNotFoundError:
            return ephemeral(
                f"Asset `{_code_text(symbol)}` is not saved. Add it first."
            )
        except TrackerAlreadyExistsError:
            return ephemeral(
                f"Asset `{_code_text(symbol)}` is already tracked."
            )
        except RepositoryError as error:
            return ephemeral(f":warning: {error}")
        return ephemeral(
            f":white_check_mark: Tracking "
            f"`{_code_text(entry.trading_symbol)}` from "
            f"{entry.added_date.isoformat()}."
        )

    if action == "delete":
        if len(parts) != 2 or not parts[1].strip():
            return ephemeral(
                "Provide a trading symbol: "
                "`/swingengine tracker delete <trading_symbol>`."
            )
        symbol = parts[1].strip()
        try:
            entry = tracker_service.delete_tracker(symbol)
        except TrackerNotFoundError:
            return ephemeral(
                f"Asset `{_code_text(symbol)}` is not tracked."
            )
        except RepositoryError as error:
            return ephemeral(f":warning: {error}")
        return ephemeral(
            f":white_check_mark: Stopped tracking "
            f"`{_code_text(entry.trading_symbol)}`."
        )

    if action == "list":
        if len(parts) != 1:
            return ephemeral("Use `/swingengine tracker list`.")
        try:
            entries = tracker_service.list_tracker()
        except RepositoryError as error:
            return ephemeral(f":warning: {error}")
        if not entries:
            return ephemeral("No assets are currently tracked.")
        return ephemeral(
            "\n".join(
                ["*Tracked assets*", *(_format_tracker(entry) for entry in entries)]
            )
        )

    return ephemeral(
        "Unknown tracker action. Use "
        "`/swingengine tracker add|delete|list ...`."
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


def _format_saved_asset(asset: AssetRecord) -> str:
    detail = (
        f" — {_slack_text(asset.asset_name)}"
        if asset.asset_name and asset.asset_name != asset.trading_symbol
        else ""
    )
    key = (
        f" · `{_code_text(asset.instrument_key)}`"
        if asset.instrument_key
        else ""
    )
    return f"• `{_code_text(asset.trading_symbol)}`{detail}{key}"


def _format_tracker(entry: TrackerEntry) -> str:
    detail = (
        f" — {_slack_text(entry.asset_name)}"
        if entry.asset_name and entry.asset_name != entry.trading_symbol
        else ""
    )
    return (
        f"• `{_code_text(entry.trading_symbol)}`{detail} · added "
        f"{entry.added_date.isoformat()}"
    )


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
    tracker_service: AssetTrackerService | None = None,
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
        "instrument",
        lambda arguments: instrument_command(arguments, asset_service),
    )
    router.register(
        "asset",
        lambda arguments: asset_command(
            arguments, asset_service, tracker_service
        ),
    )
    router.register(
        "tracker",
        lambda arguments: tracker_command(arguments, tracker_service),
    )
    return router
