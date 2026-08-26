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
from fundamental.scanner import (
    FundamentalScanError,
    FundamentalScanResult,
    SymbolFundamentalAnalysis,
)
from slack.file_exports import (
    CsvFileExporter,
    FileExportError,
    SlackFileUpload,
)
from tracker.momentum_analysis import (
    MomentumAnalysisBatch,
    MomentumAnalysisError,
    SymbolMomentumAnalysis,
)
from tracker.momentum_scanner import (
    MomentumScanError,
    MomentumScanResult,
)
from upstox.assets import AssetCatalogError, AssetSearchResult, asset_isin

SlackResponse = dict[str, Any]
CommandHandler = Callable[[str], SlackResponse]
FILE_UPLOAD_KEY = "_file_upload"
ASSET_IMPORT_MODAL_KEY = "_asset_import_modal"
TRACKER_IMPORT_MODAL_KEY = "_tracker_import_modal"


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

    def fno_isins(self) -> frozenset[str]:
        """Return ISINs of NSE equities with at least one F&O contract."""


class AssetTrackerService(Protocol):
    def add_asset(
        self, asset: AssetSearchResult, has_fno: bool = False
    ) -> AssetRecord:
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

    def update_tracker_trade_settings(
        self,
        trading_symbol: str,
        is_approved_for_trade: bool,
        amount_allocated: float,
    ) -> TrackerEntry:
        """Update an entry's admin-managed approval and allocation."""


class TrackerEvaluationService(Protocol):
    def evaluate_message(self) -> str:
        """Run the tracker momentum screen and return a Slack summary."""


class MomentumScanService(Protocol):
    def scan(self) -> MomentumScanResult:
        """Screen every NSE equity and return momentum export rows."""


class MomentumAnalysisService(Protocol):
    def analyze_symbol(
        self, trading_symbol: str, *, update_tracker: bool = False
    ) -> SymbolMomentumAnalysis:
        """Analyze one saved asset's trading symbol for momentum."""

    def analyze_assets(
        self, *, update_tracker: bool = False
    ) -> MomentumAnalysisBatch:
        """Analyze every saved asset for momentum."""

    def analyze_tracker(self) -> MomentumAnalysisBatch:
        """Re-check every tracked asset and clear momentum/side that lapsed."""


class FundamentalScanService(Protocol):
    def scan(self) -> FundamentalScanResult:
        """Screen every NSE equity and return fundamental export rows."""


class FundamentalAnalysisService(Protocol):
    def analyze(self, trading_symbol: str) -> SymbolFundamentalAnalysis:
        """Score one NSE equity's fundamentals by trading symbol."""


def ephemeral(text: str) -> SlackResponse:
    """Build a response visible only to the user who ran the command."""
    return {"response_type": "ephemeral", "text": text}


def file_upload_response(text: str, upload: SlackFileUpload) -> SlackResponse:
    """Build an ephemeral confirmation with an internal file-upload request."""
    return {
        "response_type": "ephemeral",
        "text": text,
        FILE_UPLOAD_KEY: upload,
    }


def asset_import_modal_response(text: str) -> SlackResponse:
    """Request that the Slack adapter open the asset CSV upload modal."""
    return {
        "response_type": "ephemeral",
        "text": text,
        ASSET_IMPORT_MODAL_KEY: True,
    }


def tracker_import_modal_response(text: str) -> SlackResponse:
    """Request that the Slack adapter open the tracker CSV upload modal."""
    return {
        "response_type": "ephemeral",
        "text": text,
        TRACKER_IMPORT_MODAL_KEY: True,
    }


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
        "• `/swingengine asset list` — list saved assets\n"
        "• `/swingengine asset list file` — return saved assets as CSV\n"
        "• `/swingengine asset upload` — upload an add/delete CSV\n\n"
        "*Momentum*\n"
        "• `/swingengine momentum list file` — refresh NSE instruments, "
        "screen all equities, and upload qualifying stocks as CSV\n"
        "• `/swingengine momentum analyze <trading_symbol>` — check one "
        "asset for momentum and its side (buy/sell); falls back to the "
        "NSE catalog if the symbol isn't saved\n"
        "• `/swingengine momentum analyze <trading_symbol> update tracker` "
        "— same, and update the tracker's has_momentum/side if it qualifies\n"
        "• `/swingengine momentum analyze assets` — check every saved "
        "asset for momentum and side\n"
        "• `/swingengine momentum analyze assets update tracker` — same, "
        "and update the tracker for every qualifying asset\n"
        "• `/swingengine momentum analyze tracker` — re-check every "
        "tracked asset and clear has_momentum/side for any that lapsed\n\n"
        "*Fundamentals*\n"
        "• `/swingengine fundamental list file` — refresh NSE instruments, "
        "fundamentally score all equities, and upload decent stocks as CSV\n"
        "• `/swingengine fundamental analyze <trading_symbol>` — fundamentally "
        "score one NSE equity\n"
        "• `/swingengine fundamental analyze update asset <trading_symbol>` "
        "— fundamentally score one NSE equity and save it as an asset if "
        "it scores GOOD\n"
        "• `/swingengine fundamental analyze update assets` — refresh NSE "
        "instruments, fundamentally score all equities, and save every "
        "GOOD equity as an asset\n\n"
        "*Tracker*\n"
        "• `/swingengine tracker add <trading_symbol>` — start tracking a "
        "saved asset\n"
        "• `/swingengine tracker delete <trading_symbol>` — stop tracking a "
        "saved asset\n"
        "• `/swingengine tracker asset evaluate` — evaluate saved and pending "
        "tracker assets now\n"
        "• `/swingengine tracker list` — list tracked assets\n"
        "• `/swingengine tracker list file` — return tracked assets as CSV\n"
        "• `/swingengine tracker upload` — update tracker approvals and "
        "allocations from CSV\n\n"
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
    file_exporter: CsvFileExporter | None = None,
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

        symbol = _normalize_trading_symbol(parts[1])
        try:
            matches = asset_service.search(symbol)
            fno_isins = asset_service.fno_isins()
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
            saved_asset = tracker_service.add_asset(
                asset, has_fno=asset_isin(asset) in fno_isins
            )
        except AssetAlreadyExistsError:
            return ephemeral(
                f"Asset `{_code_text(asset.trading_symbol)}` is already "
                "present."
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

        symbol = _normalize_trading_symbol(parts[1])
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
        file_requested = (
            len(parts) == 2 and parts[1].strip().casefold() == "file"
        )
        if len(parts) == 2 and not file_requested:
            return ephemeral("Use `/swingengine asset list [file]`.")
        try:
            assets = tracker_service.list_assets()
        except RepositoryError as error:
            return ephemeral(f":warning: {error}")
        if file_requested:
            if file_exporter is None:
                return ephemeral("CSV file export is not configured.")
            try:
                upload = file_exporter.export_assets(assets)
            except FileExportError as error:
                return ephemeral(f":warning: {error}")
            return file_upload_response(
                ":white_check_mark: Saved assets CSV uploaded.",
                upload,
            )
        if not assets:
            return ephemeral("No assets have been saved.")
        return ephemeral(
            "\n".join(
                ["*Saved assets*", *(_format_saved_asset(asset) for asset in assets)]
            )
        )

    if action == "upload":
        if len(parts) != 1:
            return ephemeral("Use `/swingengine asset upload`.")
        return asset_import_modal_response("Opening the asset CSV upload dialog.")

    return ephemeral(
        "Unknown asset action. Use "
        "`/swingengine asset add|delete|list|upload ...`."
    )


def tracker_command(
    arguments: str = "",
    tracker_service: AssetTrackerService | None = None,
    file_exporter: CsvFileExporter | None = None,
    evaluation_service: TrackerEvaluationService | None = None,
) -> SlackResponse:
    parts = arguments.strip().split(maxsplit=1)
    action = parts[0].casefold() if parts else ""
    if action == "asset":
        if len(parts) != 2 or parts[1].strip().casefold() != "evaluate":
            return ephemeral(
                "Use `/swingengine tracker asset evaluate`."
            )
        if evaluation_service is None:
            return ephemeral("Tracker asset evaluation is not configured.")
        return ephemeral(evaluation_service.evaluate_message())

    if action == "upload":
        if len(parts) != 1:
            return ephemeral("Use `/swingengine tracker upload`.")
        return tracker_import_modal_response(
            "Opening the tracker CSV upload dialog."
        )

    if tracker_service is None:
        return ephemeral("Asset tracker database is not configured.")

    if action == "add":
        if len(parts) != 2 or not parts[1].strip():
            return ephemeral(
                "Provide a trading symbol: "
                "`/swingengine tracker add <trading_symbol>`."
            )
        symbol = _normalize_trading_symbol(parts[1])
        try:
            entry = tracker_service.add_tracker(symbol)
        except AssetNotFoundError:
            return ephemeral(
                f"Asset `{_code_text(symbol)}` is not saved. Add it first."
            )
        except TrackerAlreadyExistsError:
            return ephemeral(
                f"Asset `{_code_text(symbol)}` is already present."
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
        symbol = _normalize_trading_symbol(parts[1])
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
        file_requested = (
            len(parts) == 2 and parts[1].strip().casefold() == "file"
        )
        if len(parts) == 2 and not file_requested:
            return ephemeral("Use `/swingengine tracker list [file]`.")
        try:
            entries = tracker_service.list_tracker()
        except RepositoryError as error:
            return ephemeral(f":warning: {error}")
        if file_requested:
            if file_exporter is None:
                return ephemeral("CSV file export is not configured.")
            try:
                upload = file_exporter.export_tracker(entries)
            except FileExportError as error:
                return ephemeral(f":warning: {error}")
            return file_upload_response(
                ":white_check_mark: Tracker CSV uploaded.",
                upload,
            )
        if not entries:
            return ephemeral("No assets are currently tracked.")
        return ephemeral(
            "\n".join(
                ["*Tracked assets*", *(_format_tracker(entry) for entry in entries)]
            )
        )

    return ephemeral(
        "Unknown tracker action. Use "
        "`/swingengine tracker add|delete|list|upload ...` or "
        "`/swingengine tracker asset evaluate`."
    )


MOMENTUM_ANALYZE_USAGE = (
    "`/swingengine momentum analyze <trading_symbol>|assets|tracker "
    "[update tracker]`."
)


def momentum_command(
    arguments: str = "",
    momentum_service: MomentumScanService | None = None,
    file_exporter: CsvFileExporter | None = None,
    momentum_analysis_service: MomentumAnalysisService | None = None,
) -> SlackResponse:
    parts = arguments.strip().split(maxsplit=1)
    action = parts[0].casefold() if parts else ""

    if action == "analyze":
        if momentum_analysis_service is None:
            return ephemeral("Momentum analysis is not configured.")
        rest = parts[1].strip() if len(parts) == 2 else ""
        tokens = rest.split()
        lowered = [token.casefold() for token in tokens]
        if not tokens:
            return ephemeral(f"Use {MOMENTUM_ANALYZE_USAGE}")

        if lowered == ["tracker"]:
            try:
                batch = momentum_analysis_service.analyze_tracker()
            except MomentumAnalysisError as error:
                return ephemeral(f":warning: {error}")
            return ephemeral(_format_momentum_batch("Tracker", batch))

        update_tracker = False
        symbol_tokens = tokens
        if len(lowered) >= 3 and lowered[-2:] == ["update", "tracker"]:
            update_tracker = True
            symbol_tokens = tokens[:-2]

        if (
            len(symbol_tokens) == 1
            and symbol_tokens[0].casefold() == "assets"
        ):
            try:
                batch = momentum_analysis_service.analyze_assets(
                    update_tracker=update_tracker
                )
            except MomentumAnalysisError as error:
                return ephemeral(f":warning: {error}")
            return ephemeral(_format_momentum_batch("Assets", batch))

        if len(symbol_tokens) != 1 or not symbol_tokens[0].strip():
            return ephemeral(f"Use {MOMENTUM_ANALYZE_USAGE}")

        symbol = _normalize_trading_symbol(symbol_tokens[0])
        try:
            result = momentum_analysis_service.analyze_symbol(
                symbol, update_tracker=update_tracker
            )
        except MomentumAnalysisError as error:
            return ephemeral(f":warning: {error}")
        return ephemeral(_format_momentum_analysis(result))

    if arguments.strip().casefold() != "list file":
        return ephemeral(
            "Use `/swingengine momentum list file` or "
            f"{MOMENTUM_ANALYZE_USAGE}"
        )
    if momentum_service is None:
        return ephemeral("NSE momentum evaluation is not configured.")
    if file_exporter is None:
        return ephemeral("CSV file export is not configured.")

    try:
        result = momentum_service.scan()
    except MomentumScanError as error:
        return ephemeral(f":warning: {error}")
    try:
        upload = file_exporter.export_momentum(result.stocks)
    except FileExportError as error:
        return ephemeral(f":warning: {error}")

    prefix = ":warning:" if result.failed else ":white_check_mark:"
    summary = (
        f"{prefix} NSE momentum scan completed for "
        f"{result.evaluated:,} of {result.equity_assets:,} equity "
        f"asset(s). Momentum: {len(result.stocks):,}; "
        f"ineligible (<{result.minimum_candles} candles): "
        f"{result.ineligible:,}; "
        f"failed: {result.failed:,}. CSV uploaded."
    )
    upload = SlackFileUpload(
        path=upload.path,
        title=upload.title,
        initial_comment=summary,
    )
    return file_upload_response(
        summary,
        upload,
    )


def fundamental_command(
    arguments: str = "",
    fundamental_service: FundamentalScanService | None = None,
    file_exporter: CsvFileExporter | None = None,
    fundamental_analysis_service: FundamentalAnalysisService | None = None,
    tracker_service: AssetTrackerService | None = None,
) -> SlackResponse:
    parts = arguments.strip().split(maxsplit=1)
    action = parts[0].casefold() if parts else ""

    if action == "analyze":
        rest = parts[1].strip() if len(parts) == 2 else ""
        tokens = rest.split()
        lowered = [token.casefold() for token in tokens]

        if lowered == ["update", "assets"]:
            if fundamental_service is None:
                return ephemeral("NSE fundamental evaluation is not configured.")
            if tracker_service is None:
                return ephemeral("Asset database is not configured.")
            try:
                scan_result = fundamental_service.scan()
            except FundamentalScanError as error:
                return ephemeral(f":warning: {error}")
            return ephemeral(
                _update_assets_from_scan(scan_result, tracker_service)
            )

        if fundamental_analysis_service is None:
            return ephemeral("NSE fundamental analysis is not configured.")

        if len(lowered) == 3 and lowered[0] == "update" and lowered[1] == "asset":
            if tracker_service is None:
                return ephemeral("Asset database is not configured.")
            symbol = _normalize_trading_symbol(tokens[2])
            try:
                result = fundamental_analysis_service.analyze(symbol)
            except FundamentalScanError as error:
                return ephemeral(f":warning: {error}")

            message = _format_fundamental_analysis(result)
            if result.analysis.get("decision") == "GOOD":
                note = _save_fundamental_asset(result, tracker_service)
            else:
                note = "Not saved: does not meet the GOOD threshold."
            return ephemeral(f"{message}\n\n{note}")

        if not rest:
            return ephemeral(
                "Provide a trading symbol: "
                "`/swingengine fundamental analyze <trading_symbol>`."
            )
        symbol = _normalize_trading_symbol(rest)
        try:
            result = fundamental_analysis_service.analyze(symbol)
        except FundamentalScanError as error:
            return ephemeral(f":warning: {error}")
        return ephemeral(_format_fundamental_analysis(result))

    if arguments.strip().casefold() != "list file":
        return ephemeral(
            "Use `/swingengine fundamental list file` or "
            "`/swingengine fundamental analyze <trading_symbol>`."
        )
    if fundamental_service is None:
        return ephemeral("NSE fundamental evaluation is not configured.")
    if file_exporter is None:
        return ephemeral("CSV file export is not configured.")

    try:
        result = fundamental_service.scan()
    except FundamentalScanError as error:
        return ephemeral(f":warning: {error}")
    try:
        upload = file_exporter.export_fundamental(result.stocks)
    except FileExportError as error:
        return ephemeral(f":warning: {error}")

    has_warnings = bool(
        result.failed or result.skipped or result.endpoint_failures
    )
    prefix = ":warning:" if has_warnings else ":white_check_mark:"
    summary = (
        f"{prefix} NSE fundamental scan completed for "
        f"{result.evaluated:,} of {result.equity_assets:,} equity "
        f"asset(s). Decent (score >= {result.good_threshold:g}): "
        f"{len(result.stocks):,}; skipped: {result.skipped:,}; "
        f"failed: {result.failed:,}; endpoint failures: "
        f"{result.endpoint_failures:,}. CSV uploaded."
    )
    upload = SlackFileUpload(
        path=upload.path,
        title=upload.title,
        initial_comment=summary,
    )
    return file_upload_response(summary, upload)


def _add_asset_if_new(
    asset: AssetSearchResult,
    tracker_service: AssetTrackerService,
    has_fno: bool = False,
) -> str:
    """Add an asset and report the outcome as "added", "present", or an
    "error: ..." message."""
    try:
        tracker_service.add_asset(asset, has_fno=has_fno)
    except AssetAlreadyExistsError:
        return "present"
    except RepositoryError as error:
        return f"error: {error}"
    return "added"


def _update_assets_from_scan(
    scan_result: FundamentalScanResult,
    tracker_service: AssetTrackerService,
) -> str:
    added = 0
    present = 0
    failed = 0
    for stock in scan_result.stocks:
        asset = AssetSearchResult(
            trading_symbol=stock.trading_symbol,
            name=stock.asset_name,
            segment="NSE_EQ",
            instrument_type="EQ",
            instrument_key=stock.instrument_key,
            isin=stock.isin,
        )
        outcome = _add_asset_if_new(asset, tracker_service, stock.has_fno)
        if outcome == "added":
            added += 1
        elif outcome == "present":
            present += 1
        else:
            failed += 1

    prefix = ":warning:" if failed else ":white_check_mark:"
    return (
        f"{prefix} Fundamental asset update completed. GOOD stocks: "
        f"{len(scan_result.stocks):,}; added: {added:,}; "
        f"already present: {present:,}; failed to save: {failed:,}."
    )


def _save_fundamental_asset(
    result: SymbolFundamentalAnalysis,
    tracker_service: AssetTrackerService,
) -> str:
    asset = AssetSearchResult(
        trading_symbol=result.trading_symbol,
        name=result.asset_name,
        segment="NSE_EQ",
        instrument_type="EQ",
        instrument_key=result.instrument_key,
        isin=result.isin,
    )
    outcome = _add_asset_if_new(asset, tracker_service, result.has_fno)
    if outcome == "present":
        return (
            f"Asset `{_code_text(result.trading_symbol)}` is already "
            "present; skipped."
        )
    if outcome != "added":
        error = outcome.removeprefix("error: ")
        return f":warning: {error}"
    return f":white_check_mark: Saved asset `{_code_text(result.trading_symbol)}`."


def _format_fundamental_analysis(result: SymbolFundamentalAnalysis) -> str:
    analysis = result.analysis
    decision = analysis["decision"]
    prefix = ":white_check_mark:" if decision == "GOOD" else ":warning:"
    company = _slack_text(str(analysis.get("company") or result.asset_name))
    sector = _slack_text(str(analysis.get("sector") or "Unknown"))
    period = _slack_text(str(analysis.get("latest_financial_period") or "Unknown"))

    lines = [
        f"{prefix} *{company}* (`{_code_text(result.trading_symbol)}`) — "
        f"{decision} ({analysis['rating']})",
        f"Sector: {sector} · Latest financial period: {period}",
        f"Score: {analysis['score']:.1f}/100 (GOOD requires "
        f"{analysis['good_threshold']:.1f}+) · Confidence: "
        f"{analysis['confidence']['score']:.1f}/100 "
        f"({analysis['confidence']['label']})",
        "",
        "*Category scores*",
    ]
    for category in analysis["categories"]:
        score = (
            "N/A" if category["score"] is None else f"{category['score']:.1f}/100"
        )
        lines.append(f"• {_slack_text(category['name'])}: {score}")

    issues = analysis.get("data_issues") or []
    if issues:
        lines.append("")
        lines.append("*Data issues*")
        for issue in issues[:5]:
            code = f" [{issue['code']}]" if issue.get("code") else ""
            lines.append(
                f"• {_slack_text(issue['endpoint'])}{code}: "
                f"{_slack_text(issue['message'])}"
            )
        if len(issues) > 5:
            lines.append(f"• …and {len(issues) - 5} more")

    caveats = analysis.get("overall_caveats") or []
    if caveats:
        lines.append("")
        lines.append("*Caveats*")
        for caveat in caveats:
            lines.append(f"• {_slack_text(caveat)}")

    return "\n".join(lines)


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


def _format_momentum_analysis(result: SymbolMomentumAnalysis) -> str:
    prefix = ":white_check_mark:" if result.has_momentum else ":warning:"
    lines = [
        f"{prefix} `{_code_text(result.trading_symbol)}` momentum: "
        f"{result.has_momentum} · side: {result.side or 'none'}",
    ]
    if result.tracker_updated:
        lines.append("Tracker updated.")
    return "\n".join(lines)


def _format_momentum_batch(label: str, batch: MomentumAnalysisBatch) -> str:
    momentum_count = sum(1 for result in batch.results if result.has_momentum)
    prefix = ":warning:" if batch.failed else ":white_check_mark:"
    lines = [
        f"{prefix} {label} momentum analysis completed for "
        f"{len(batch.results):,} symbol(s). Momentum: {momentum_count:,}; "
        f"failed: {batch.failed:,}."
    ]
    shown = batch.results[:20]
    if shown:
        lines.append("")
        lines.extend(
            f"• `{_code_text(result.trading_symbol)}` — momentum: "
            f"{result.has_momentum}, side: {result.side or 'none'}"
            for result in shown
        )
        if len(batch.results) > 20:
            lines.append(f"• …and {len(batch.results) - 20:,} more")
    return "\n".join(lines)


def _normalize_trading_symbol(value: str) -> str:
    """Normalize Slack trading symbols to the NSE catalog representation."""
    return value.strip().upper()


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
    file_exporter: CsvFileExporter | None = None,
    evaluation_service: TrackerEvaluationService | None = None,
    momentum_service: MomentumScanService | None = None,
    momentum_analysis_service: MomentumAnalysisService | None = None,
    fundamental_service: FundamentalScanService | None = None,
    fundamental_analysis_service: FundamentalAnalysisService | None = None,
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
            arguments,
            asset_service,
            tracker_service,
            file_exporter,
        ),
    )
    router.register(
        "momentum",
        lambda arguments: momentum_command(
            arguments,
            momentum_service,
            file_exporter,
            momentum_analysis_service,
        ),
    )
    router.register(
        "fundamental",
        lambda arguments: fundamental_command(
            arguments,
            fundamental_service,
            file_exporter,
            fundamental_analysis_service,
            tracker_service,
        ),
    )
    router.register(
        "tracker",
        lambda arguments: tracker_command(
            arguments,
            tracker_service,
            file_exporter,
            evaluation_service,
        ),
    )
    return router
