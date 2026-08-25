from datetime import date

from database.repository import (
    AssetAlreadyExistsError,
    AssetInUseError,
    AssetNotFoundError,
    AssetRecord,
    TrackerAlreadyExistsError,
    TrackerEntry,
    TrackerNotFoundError,
)
from fundamental.scanner import (
    FundamentalScanError,
    FundamentalScanResult,
    FundamentalStock,
    SymbolFundamentalAnalysis,
)
from slack.commands import CommandRouter, build_router, ephemeral
from slack.file_exports import CsvFileExporter, SlackFileUpload
from tracker.momentum_scanner import (
    MomentumScanError,
    MomentumScanResult,
    MomentumStock,
)
from upstox.assets import AssetCatalogError, AssetSearchResult


class FakeAuthService:
    def status_message(self) -> str:
        return "Upstox approval is pending."

    def set_token_message(self, access_token: str) -> str:
        assert access_token == "new-token"
        return "Upstox token stored."


class FakeAssetService:
    def __init__(self) -> None:
        self.search_query = ""

    def refresh(self) -> int:
        return 12_345

    def search(self, query: str) -> list[AssetSearchResult]:
        self.search_query = query
        assert query.casefold() in {"sun", "sunpharma"}
        return [
            AssetSearchResult(
                trading_symbol="SUNPHARMA",
                name="SUN PHARMACEUTICAL IND L",
                segment="NSE_EQ",
                instrument_type="EQ",
                instrument_key="NSE_EQ|INE044A01036",
            ),
            AssetSearchResult(
                trading_symbol="SUNTECH",
                name="SUN TECH LTD",
                segment="NSE_EQ",
                instrument_type="EQ",
                instrument_key="NSE_EQ|INE000000001",
            ),
        ]


class FakeAssetTrackerService:
    def __init__(self) -> None:
        self.asset = AssetRecord(
            asset_id=42,
            asset_name="SUN PHARMACEUTICAL IND L",
            trading_symbol="SUNPHARMA",
            instrument_key="NSE_EQ|INE044A01036",
        )
        self.entry = TrackerEntry(
            tracker_details_id=7,
            asset_id=42,
            asset_name=self.asset.asset_name,
            trading_symbol=self.asset.trading_symbol,
            has_momentum=True,
            is_trade_created=False,
            is_approved_for_trade=True,
            amount_allocated=12500.5,
            added_date=date(2026, 7, 28),
        )
        self.added_catalog_asset: AssetSearchResult | None = None
        self.deleted_asset_symbol = ""
        self.added_tracker_symbol = ""
        self.deleted_tracker_symbol = ""
        self.updated_tracker_settings: tuple[str, bool, float] | None = None

    def add_asset(self, asset: AssetSearchResult) -> AssetRecord:
        self.added_catalog_asset = asset
        return self.asset

    def delete_asset(self, trading_symbol: str) -> AssetRecord:
        self.deleted_asset_symbol = trading_symbol
        return self.asset

    def list_assets(self) -> list[AssetRecord]:
        return [self.asset]

    def add_tracker(self, trading_symbol: str) -> TrackerEntry:
        self.added_tracker_symbol = trading_symbol
        return self.entry

    def delete_tracker(self, trading_symbol: str) -> TrackerEntry:
        self.deleted_tracker_symbol = trading_symbol
        return self.entry

    def list_tracker(self) -> list[TrackerEntry]:
        return [self.entry]

    def update_tracker_trade_settings(
        self,
        trading_symbol: str,
        is_approved_for_trade: bool,
        amount_allocated: float,
    ) -> TrackerEntry:
        self.updated_tracker_settings = (
            trading_symbol,
            is_approved_for_trade,
            amount_allocated,
        )
        return self.entry


class FailingAssetService:
    def refresh(self) -> int:
        raise AssetCatalogError("Download failed.")

    def search(self, query: str) -> list[AssetSearchResult]:
        raise AssetCatalogError("Catalog is missing.")


class FakeEvaluationService:
    def evaluate_message(self) -> str:
        return (
            ":white_check_mark: Tracker asset evaluation completed for "
            "2 asset(s)."
        )


class FakeMomentumService:
    def scan(self) -> MomentumScanResult:
        return MomentumScanResult(
            catalog_instruments=12_345,
            equity_assets=2,
            evaluated=2,
            failed=0,
            stocks=(
                MomentumStock(
                    asset_name="SUN PHARMACEUTICAL IND L",
                    trading_symbol="SUNPHARMA",
                    ltp=1789.25,
                ),
            ),
            minimum_candles=100,
        )


class FakeFundamentalService:
    def scan(self) -> FundamentalScanResult:
        return FundamentalScanResult(
            catalog_instruments=12_345,
            equity_assets=2,
            evaluated=2,
            failed=0,
            skipped=0,
            endpoint_failures=0,
            good_threshold=70.0,
            stocks=(
                FundamentalStock(
                    asset_name="SUN PHARMACEUTICAL IND L",
                    trading_symbol="SUNPHARMA",
                    isin="INE044A01036",
                    score=82.5,
                    rating="STRONG",
                    confidence=100.0,
                    sector="Pharmaceuticals",
                    latest_financial_period="Mar 2026",
                    has_fno=True,
                ),
            ),
        )


def test_empty_command_shows_help() -> None:
    response = build_router().dispatch("")

    assert response["response_type"] == "ephemeral"
    assert "/swingengine help" in response["text"]


def test_help_lists_every_supported_command_and_disabled_workflow() -> None:
    text = build_router().dispatch("help")["text"]

    expected_entries = (
        "• `/swingengine` or `/swingengine help`",
        "• `/swingengine ping`",
        "• `/swingengine status`",
        "• `/swingengine auth` or `/swingengine auth status`",
        "• `/swingengine auth set <token>`",
        "• `/swingengine instrument refresh`",
        "• `/swingengine instrument search <query>`",
        "• `/swingengine asset add <trading_symbol>`",
        "• `/swingengine asset delete <trading_symbol>`",
        "• `/swingengine asset list`",
        "• `/swingengine asset upload`",
        "• `/swingengine momentum list file`",
        "• `/swingengine fundamental list file`",
        "• `/swingengine tracker add <trading_symbol>`",
        "• `/swingengine tracker delete <trading_symbol>`",
        "• `/swingengine tracker asset evaluate`",
        "• `/swingengine tracker list`",
        "• `/swingengine tracker upload`",
        "• `/swingengine auth request`",
    )
    for entry in expected_entries:
        assert entry in text
    assert "Disabled workflow" in text


def test_command_names_are_case_insensitive() -> None:
    assert build_router().dispatch("  PiNg  ") == ephemeral("pong")


def test_any_whitespace_can_separate_command_and_arguments() -> None:
    router = CommandRouter()
    router.register("help", lambda arguments: ephemeral("help"))
    router.register("echo", lambda arguments: ephemeral(arguments))

    assert router.dispatch("echo\tone two") == ephemeral("one two")


def test_status_command_reports_running() -> None:
    assert "running" in build_router().dispatch("status")["text"]


def test_status_and_auth_commands_include_upstox_state() -> None:
    router = build_router(FakeAuthService())

    assert "approval is pending" in router.dispatch("status")["text"]
    assert "approval is pending" in router.dispatch("auth status")["text"]
    assert "token stored" in router.dispatch("auth set new-token")["text"]


def test_auth_set_requires_a_token() -> None:
    response = build_router(FakeAuthService()).dispatch("auth set")

    assert "Provide the token" in response["text"]


def test_auth_request_explains_manual_workflow() -> None:
    response = build_router(FakeAuthService()).dispatch("auth request")

    assert "disabled" in response["text"]
    assert "auth set" in response["text"]


def test_auth_command_rejects_unknown_action() -> None:
    response = build_router(FakeAuthService()).dispatch("auth rotate")

    assert "Unknown auth action" in response["text"]


def test_instrument_refresh_reports_downloaded_count() -> None:
    response = build_router(
        asset_service=FakeAssetService()
    ).dispatch("instrument refresh")

    assert response == ephemeral(
        ":white_check_mark: Refreshed 12,345 NSE instruments."
    )


def test_instrument_search_returns_related_instruments() -> None:
    response = build_router(
        asset_service=FakeAssetService()
    ).dispatch("instrument search sun")

    assert response["response_type"] == "ephemeral"
    assert "SUNPHARMA" in response["text"]
    assert "SUNTECH" in response["text"]
    assert "NSE_EQ|INE044A01036" in response["text"]


def test_instrument_search_requires_a_query() -> None:
    response = build_router(
        asset_service=FakeAssetService()
    ).dispatch("instrument search")

    assert "Provide a search term" in response["text"]


def test_instrument_command_reports_catalog_errors() -> None:
    router = build_router(asset_service=FailingAssetService())

    assert "Download failed" in router.dispatch("instrument refresh")["text"]
    assert "Catalog is missing" in router.dispatch(
        "instrument search sun"
    )["text"]


def test_asset_add_resolves_an_exact_nse_symbol_and_saves_it() -> None:
    asset_service = FakeAssetService()
    tracker_service = FakeAssetTrackerService()
    response = build_router(
        asset_service=asset_service,
        tracker_service=tracker_service,
    ).dispatch("asset add sunpharma")

    assert response == ephemeral(
        ":white_check_mark: Saved asset `SUNPHARMA`."
    )
    assert asset_service.search_query == "SUNPHARMA"
    assert tracker_service.added_catalog_asset is not None
    assert (
        tracker_service.added_catalog_asset.instrument_key
        == "NSE_EQ|INE044A01036"
    )


def test_asset_add_requires_an_exact_trading_symbol() -> None:
    response = build_router(
        asset_service=FakeAssetService(),
        tracker_service=FakeAssetTrackerService(),
    ).dispatch("asset add sun")

    assert "No exact NSE trading symbol" in response["text"]


def test_asset_add_reports_an_existing_saved_asset() -> None:
    class DuplicateAssetService(FakeAssetTrackerService):
        def add_asset(self, asset: AssetSearchResult) -> AssetRecord:
            raise AssetAlreadyExistsError

    response = build_router(
        asset_service=FakeAssetService(),
        tracker_service=DuplicateAssetService(),
    ).dispatch("asset add SUNPHARMA")

    assert response == ephemeral("Asset `SUNPHARMA` is already present.")


def test_asset_delete_removes_a_saved_asset() -> None:
    tracker_service = FakeAssetTrackerService()
    response = build_router(
        tracker_service=tracker_service
    ).dispatch("asset delete sunpharma")

    assert response == ephemeral(
        ":white_check_mark: Deleted asset `SUNPHARMA`."
    )
    assert tracker_service.deleted_asset_symbol == "SUNPHARMA"


def test_asset_delete_requires_tracker_removal_first() -> None:
    class TrackedAssetService(FakeAssetTrackerService):
        def delete_asset(self, trading_symbol: str) -> AssetRecord:
            raise AssetInUseError

    response = build_router(
        tracker_service=TrackedAssetService()
    ).dispatch("asset delete SUNPHARMA")

    assert "still tracked" in response["text"]
    assert "tracker entry first" in response["text"]


def test_asset_list_shows_names_symbols_and_instrument_keys() -> None:
    response = build_router(
        tracker_service=FakeAssetTrackerService()
    ).dispatch("asset list")

    assert "*Saved assets*" in response["text"]
    assert "SUN PHARMACEUTICAL IND L" in response["text"]
    assert "SUNPHARMA" in response["text"]
    assert "NSE_EQ|INE044A01036" in response["text"]


def test_asset_list_file_requests_a_csv_upload(tmp_path) -> None:
    response = build_router(
        tracker_service=FakeAssetTrackerService(),
        file_exporter=CsvFileExporter(tmp_path),
    ).dispatch("asset list file")

    upload = response["_file_upload"]
    assert isinstance(upload, SlackFileUpload)
    assert upload.path == tmp_path / "asset-list.csv"
    assert response["text"] == ":white_check_mark: Saved assets CSV uploaded."


def test_asset_upload_requests_the_slack_file_modal() -> None:
    response = build_router().dispatch("asset upload")

    assert response["response_type"] == "ephemeral"
    assert response["text"] == "Opening the asset CSV upload dialog."
    assert response["_asset_import_modal"] is True


def test_tracker_add_matches_a_saved_asset() -> None:
    tracker_service = FakeAssetTrackerService()
    response = build_router(
        tracker_service=tracker_service
    ).dispatch("tracker add sunpharma")

    assert response == ephemeral(
        ":white_check_mark: Tracking `SUNPHARMA` from 2026-07-28."
    )
    assert tracker_service.added_tracker_symbol == "SUNPHARMA"


def test_tracker_add_requires_a_saved_untracked_asset() -> None:
    class MissingAssetService(FakeAssetTrackerService):
        def add_tracker(self, trading_symbol: str) -> TrackerEntry:
            raise AssetNotFoundError

    class DuplicateTrackerService(FakeAssetTrackerService):
        def add_tracker(self, trading_symbol: str) -> TrackerEntry:
            raise TrackerAlreadyExistsError

    missing_response = build_router(
        tracker_service=MissingAssetService()
    ).dispatch("tracker add MISSING")
    duplicate_response = build_router(
        tracker_service=DuplicateTrackerService()
    ).dispatch("tracker add SUNPHARMA")

    assert "not saved" in missing_response["text"]
    assert duplicate_response == ephemeral(
        "Asset `SUNPHARMA` is already present."
    )


def test_tracker_delete_removes_the_matching_entry() -> None:
    tracker_service = FakeAssetTrackerService()
    response = build_router(
        tracker_service=tracker_service
    ).dispatch("tracker delete SUNPHARMA")

    assert response == ephemeral(
        ":white_check_mark: Stopped tracking `SUNPHARMA`."
    )
    assert tracker_service.deleted_tracker_symbol == "SUNPHARMA"


def test_tracker_delete_reports_a_missing_entry() -> None:
    class MissingTrackerService(FakeAssetTrackerService):
        def delete_tracker(self, trading_symbol: str) -> TrackerEntry:
            raise TrackerNotFoundError

    response = build_router(
        tracker_service=MissingTrackerService()
    ).dispatch("tracker delete MISSING")

    assert "not tracked" in response["text"]


def test_tracker_list_shows_asset_name_symbol_and_added_date() -> None:
    response = build_router(
        tracker_service=FakeAssetTrackerService()
    ).dispatch("tracker list")

    assert "*Tracked assets*" in response["text"]
    assert "SUN PHARMACEUTICAL IND L" in response["text"]
    assert "SUNPHARMA" in response["text"]
    assert "2026-07-28" in response["text"]


def test_tracker_list_file_requests_a_csv_upload(tmp_path) -> None:
    response = build_router(
        tracker_service=FakeAssetTrackerService(),
        file_exporter=CsvFileExporter(tmp_path),
    ).dispatch("tracker list file")

    upload = response["_file_upload"]
    assert isinstance(upload, SlackFileUpload)
    assert upload.path == tmp_path / "tracker-list.csv"
    assert response["text"] == ":white_check_mark: Tracker CSV uploaded."


def test_tracker_upload_requests_the_slack_file_modal() -> None:
    response = build_router().dispatch("tracker upload")

    assert response["response_type"] == "ephemeral"
    assert response["text"] == "Opening the tracker CSV upload dialog."
    assert response["_tracker_import_modal"] is True


def test_tracker_asset_evaluate_runs_the_momentum_screen() -> None:
    response = build_router(
        evaluation_service=FakeEvaluationService()
    ).dispatch("tracker asset evaluate")

    assert response == ephemeral(
        ":white_check_mark: Tracker asset evaluation completed for 2 asset(s)."
    )


def test_tracker_asset_evaluate_requires_exact_command_and_service() -> None:
    router = build_router()

    assert "not configured" in router.dispatch(
        "tracker asset evaluate"
    )["text"]
    assert "tracker asset evaluate" in router.dispatch(
        "tracker asset refresh"
    )["text"]


def test_momentum_list_file_runs_scan_and_requests_csv_upload(tmp_path) -> None:
    response = build_router(
        file_exporter=CsvFileExporter(tmp_path),
        momentum_service=FakeMomentumService(),
    ).dispatch("momentum list file")

    upload = response["_file_upload"]
    assert isinstance(upload, SlackFileUpload)
    assert upload.path == tmp_path / "momentum-list.csv"
    assert "Momentum: 1" in upload.initial_comment
    assert "2 of 2" in response["text"]
    assert "Momentum: 1" in response["text"]
    assert "ineligible (<100 candles): 0" in response["text"]
    assert "failed: 0" in response["text"]


def test_momentum_list_file_reports_scan_failure() -> None:
    class FailingMomentumService:
        def scan(self) -> MomentumScanResult:
            raise MomentumScanError("Upstox quotes failed.")

    response = build_router(
        file_exporter=CsvFileExporter("unused"),
        momentum_service=FailingMomentumService(),
    ).dispatch("momentum list file")

    assert response == ephemeral(":warning: Upstox quotes failed.")


def test_momentum_command_requires_exact_arguments_and_services(tmp_path) -> None:
    assert "momentum list file" in build_router().dispatch(
        "momentum list"
    )["text"]
    assert "not configured" in build_router(
        file_exporter=CsvFileExporter(tmp_path)
    ).dispatch("momentum list file")["text"]
    assert "not configured" in build_router(
        momentum_service=FakeMomentumService()
    ).dispatch("momentum list file")["text"]


def test_fundamental_list_file_runs_scan_and_requests_csv_upload(
    tmp_path,
) -> None:
    response = build_router(
        file_exporter=CsvFileExporter(tmp_path),
        fundamental_service=FakeFundamentalService(),
    ).dispatch("fundamental list file")

    upload = response["_file_upload"]
    assert isinstance(upload, SlackFileUpload)
    assert upload.path == tmp_path / "fundamental-list.csv"
    assert "Decent (score >= 70): 1" in upload.initial_comment
    assert "2 of 2" in response["text"]
    assert "endpoint failures: 0" in response["text"]


def test_fundamental_list_file_reports_scan_failure() -> None:
    class FailingFundamentalService:
        def scan(self) -> FundamentalScanResult:
            raise FundamentalScanError("Fundamentals API failed.")

    response = build_router(
        file_exporter=CsvFileExporter("unused"),
        fundamental_service=FailingFundamentalService(),
    ).dispatch("fundamental list file")

    assert response == ephemeral(":warning: Fundamentals API failed.")


def test_fundamental_command_requires_exact_arguments_and_services(
    tmp_path,
) -> None:
    assert "fundamental list file" in build_router().dispatch(
        "fundamental list"
    )["text"]
    assert "not configured" in build_router(
        file_exporter=CsvFileExporter(tmp_path)
    ).dispatch("fundamental list file")["text"]
    assert "not configured" in build_router(
        fundamental_service=FakeFundamentalService()
    ).dispatch("fundamental list file")["text"]


def _sample_symbol_analysis(decision: str = "GOOD") -> SymbolFundamentalAnalysis:
    return SymbolFundamentalAnalysis(
        trading_symbol="SUNPHARMA",
        asset_name="SUN PHARMACEUTICAL IND L",
        isin="INE044A01036",
        instrument_key="NSE_EQ|INE044A01036",
        analysis={
            "company": "Sun Pharmaceutical Industries",
            "sector": "Pharmaceuticals",
            "latest_financial_period": "Mar 2026",
            "decision": decision,
            "rating": "STRONG",
            "score": 82.5,
            "good_threshold": 70.0,
            "confidence": {"score": 91.0, "label": "HIGH"},
            "categories": [
                {"name": "Valuation", "score": 85.0},
                {"name": "Profitability", "score": None},
            ],
            "data_issues": [
                {
                    "endpoint": "cash_flow",
                    "message": "Endpoint returned no data",
                    "code": None,
                },
            ],
            "overall_caveats": ["This is a screen, not advice."],
        },
    )


class FakeFundamentalAnalysisService:
    def __init__(
        self,
        result: SymbolFundamentalAnalysis | None = None,
        error: FundamentalScanError | None = None,
    ) -> None:
        self.result = result or _sample_symbol_analysis()
        self.error = error
        self.requested_symbol = ""

    def analyze(self, trading_symbol: str) -> SymbolFundamentalAnalysis:
        self.requested_symbol = trading_symbol
        if self.error is not None:
            raise self.error
        return self.result


def test_fundamental_analyze_formats_score_and_categories() -> None:
    service = FakeFundamentalAnalysisService()
    response = build_router(fundamental_analysis_service=service).dispatch(
        "fundamental analyze sunpharma"
    )

    assert service.requested_symbol == "SUNPHARMA"
    text = response["text"]
    assert "Sun Pharmaceutical Industries" in text
    assert "`SUNPHARMA`" in text
    assert "GOOD (STRONG)" in text
    assert "82.5/100" in text
    assert "Valuation: 85.0/100" in text
    assert "Profitability: N/A" in text
    assert "cash_flow" in text
    assert "This is a screen, not advice." in text


def test_fundamental_analyze_saves_asset_when_good() -> None:
    service = FakeFundamentalAnalysisService()
    tracker_service = FakeAssetTrackerService()

    response = build_router(
        fundamental_analysis_service=service,
        tracker_service=tracker_service,
    ).dispatch("fundamental analyze sunpharma")

    assert tracker_service.added_catalog_asset is not None
    assert tracker_service.added_catalog_asset.trading_symbol == "SUNPHARMA"
    assert "Saved asset `SUNPHARMA`" in response["text"]


def test_fundamental_analyze_reports_already_saved_asset() -> None:
    service = FakeFundamentalAnalysisService()

    class AlreadySavedTrackerService(FakeAssetTrackerService):
        def add_asset(self, asset: AssetSearchResult) -> AssetRecord:
            raise AssetAlreadyExistsError

    response = build_router(
        fundamental_analysis_service=service,
        tracker_service=AlreadySavedTrackerService(),
    ).dispatch("fundamental analyze sunpharma")

    assert "SUNPHARMA` is already saved" in response["text"]


def test_fundamental_analyze_does_not_save_when_not_good() -> None:
    service = FakeFundamentalAnalysisService(
        result=_sample_symbol_analysis(decision="AVOID")
    )
    tracker_service = FakeAssetTrackerService()

    response = build_router(
        fundamental_analysis_service=service,
        tracker_service=tracker_service,
    ).dispatch("fundamental analyze sunpharma")

    assert tracker_service.added_catalog_asset is None
    assert "Saved asset" not in response["text"]


def test_fundamental_analyze_reports_lookup_failure() -> None:
    service = FakeFundamentalAnalysisService(
        error=FundamentalScanError(
            "No NSE equity found for trading symbol `BOGUS`."
        )
    )
    response = build_router(fundamental_analysis_service=service).dispatch(
        "fundamental analyze BOGUS"
    )

    assert response == ephemeral(
        ":warning: No NSE equity found for trading symbol `BOGUS`."
    )


def test_fundamental_analyze_requires_symbol_and_service() -> None:
    assert "Provide a trading symbol" in build_router(
        fundamental_analysis_service=FakeFundamentalAnalysisService()
    ).dispatch("fundamental analyze")["text"]
    assert "not configured" in build_router().dispatch(
        "fundamental analyze SUNPHARMA"
    )["text"]


def test_list_file_requires_csv_export_configuration() -> None:
    router = build_router(tracker_service=FakeAssetTrackerService())

    assert "not configured" in router.dispatch("asset list file")["text"]
    assert "not configured" in router.dispatch("tracker list file")["text"]


def test_asset_command_rejects_unknown_action() -> None:
    response = build_router(
        asset_service=FakeAssetService()
    ).dispatch("asset import")

    assert "Unknown asset action" in response["text"]


def test_instrument_command_rejects_unknown_action() -> None:
    response = build_router(
        asset_service=FakeAssetService()
    ).dispatch("instrument import")

    assert "Unknown instrument action" in response["text"]


def test_asset_command_no_longer_handles_instrument_actions() -> None:
    router = build_router(asset_service=FakeAssetService())

    assert "Unknown asset action" in router.dispatch("asset refresh")["text"]
    assert "Unknown asset action" in router.dispatch("asset search sun")["text"]


def test_unknown_command_suggests_help() -> None:
    response = build_router().dispatch("buy NOW")

    assert "Unknown command `buy`" in response["text"]
    assert "/swingengine help" in response["text"]


def test_router_passes_arguments_to_handler() -> None:
    router = CommandRouter()
    router.register("help", lambda arguments: ephemeral("help"))
    router.register("echo", lambda arguments: ephemeral(arguments))

    assert router.dispatch("echo one two") == ephemeral("one two")


def test_router_rejects_invalid_command_names() -> None:
    router = CommandRouter()

    try:
        router.register("two words", lambda arguments: ephemeral(arguments))
    except ValueError as error:
        assert "one non-empty word" in str(error)
    else:
        raise AssertionError("Expected ValueError")
