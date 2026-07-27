from slack.commands import CommandRouter, build_router, ephemeral
from upstox.assets import AssetCatalogError, AssetSearchResult


class FakeAuthService:
    def status_message(self) -> str:
        return "Upstox approval is pending."

    def set_token_message(self, access_token: str) -> str:
        assert access_token == "new-token"
        return "Upstox token stored."


class FakeAssetService:
    def refresh(self) -> int:
        return 12_345

    def search(self, query: str) -> list[AssetSearchResult]:
        assert query == "sun"
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


class FailingAssetService:
    def refresh(self) -> int:
        raise AssetCatalogError("Download failed.")

    def search(self, query: str) -> list[AssetSearchResult]:
        raise AssetCatalogError("Catalog is missing.")


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
        "• `/swingengine asset refresh`",
        "• `/swingengine asset search <query>`",
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


def test_asset_refresh_reports_downloaded_count() -> None:
    response = build_router(
        asset_service=FakeAssetService()
    ).dispatch("asset refresh")

    assert response == ephemeral(
        ":white_check_mark: Refreshed 12,345 NSE assets."
    )


def test_asset_search_returns_related_instruments() -> None:
    response = build_router(
        asset_service=FakeAssetService()
    ).dispatch("asset search sun")

    assert response["response_type"] == "ephemeral"
    assert "SUNPHARMA" in response["text"]
    assert "SUNTECH" in response["text"]
    assert "NSE_EQ|INE044A01036" in response["text"]


def test_asset_search_requires_a_query() -> None:
    response = build_router(
        asset_service=FakeAssetService()
    ).dispatch("asset search")

    assert "Provide a search term" in response["text"]


def test_asset_command_reports_catalog_errors() -> None:
    router = build_router(asset_service=FailingAssetService())

    assert "Download failed" in router.dispatch("asset refresh")["text"]
    assert "Catalog is missing" in router.dispatch("asset search sun")["text"]


def test_asset_command_rejects_unknown_action() -> None:
    response = build_router(
        asset_service=FakeAssetService()
    ).dispatch("asset delete")

    assert "Unknown asset action" in response["text"]


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
