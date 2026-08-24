from typing import Any

import pytest
import requests

from groww import fundamentals
from groww.fundamentals import (
    GrowwFundamentalFallback,
    GrowwLookupError,
    build_competitors,
    build_income_statement,
    build_key_ratios,
    build_profile,
    build_share_holdings,
    fetch_stock_data,
    resolve_search_id,
)
from upstox.client import UpstoxAPIError


class FakeResponse:
    def __init__(self, *, json_payload: object = None, text: str = ""):
        self._json_payload = json_payload
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._json_payload


SEARCH_PAYLOAD = {
    "data": {
        "content": [
            {
                "entity_type": "Stocks",
                "isin": "INE0NT901020",
                "search_id": "netweb-technologies-india-ltd",
            },
            {
                "entity_type": "Stocks",
                "isin": "INE044A01036",
                "search_id": "sun-pharma-ltd",
            },
        ]
    }
}


def test_resolve_search_id_matches_exact_isin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fundamentals.requests, "get", lambda *a, **k: FakeResponse(json_payload=SEARCH_PAYLOAD)
    )
    assert resolve_search_id("INE0NT901020") == "netweb-technologies-india-ltd"


def test_resolve_search_id_raises_when_isin_not_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fundamentals.requests, "get", lambda *a, **k: FakeResponse(json_payload=SEARCH_PAYLOAD)
    )
    with pytest.raises(GrowwLookupError):
        resolve_search_id("INE9999999999")


def test_fetch_stock_data_parses_next_data_script(monkeypatch: pytest.MonkeyPatch) -> None:
    page = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        '{"props": {"pageProps": {"stockData": {"header": {"displayName": "Netweb"}}}}}'
        "</script></body></html>"
    )
    monkeypatch.setattr(fundamentals.requests, "get", lambda *a, **k: FakeResponse(text=page))
    stock = fetch_stock_data("netweb-technologies-india-ltd")
    assert stock == {"header": {"displayName": "Netweb"}}


def test_fetch_stock_data_raises_when_next_data_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fundamentals.requests, "get", lambda *a, **k: FakeResponse(text="<html></html>")
    )
    with pytest.raises(GrowwLookupError):
        fetch_stock_data("netweb-technologies-india-ltd")


def test_build_profile_prefixes_summary_with_company_name() -> None:
    stock = {
        "header": {"displayName": "Netweb", "industryName": "IT Services"},
        "details": {"fullName": "Netweb Technologies", "businessSummary": "makes servers."},
    }
    result = build_profile(stock)
    assert result["status"] == "success"
    assert result["data"]["sector"] == "IT Services"
    assert result["data"]["company_profile"] == "Netweb Technologies Limited is a company that makes servers."


def test_build_key_ratios_maps_documented_names() -> None:
    stock = {
        "fundamentals": [
            {"name": "P/E Ratio(TTM)", "value": 25.5},
            {"name": "Industry P/E", "value": 30.0},
            {"name": "ROE", "value": 18.2},
        ]
    }
    ratios = {item["name"]: item for item in build_key_ratios(stock)["data"]}
    assert ratios["P/E"]["company_value"] == 25.5
    assert ratios["P/E"]["sector_value"] == 30.0
    assert ratios["ROE"]["company_value"] == 18.2


def test_build_income_statement_sorts_years_and_labels_periods() -> None:
    stock = {
        "financialStatement": [
            {"title": "Revenue", "yearly": {"2025": 100, "2023": 60, "2024": 80}},
            {"title": "Profit", "yearly": {"2024": 10, "2023": 5}},
        ]
    }
    result = build_income_statement(stock)
    categories = {item["category"]: item["history"] for item in result["data"]["income_statement"]}
    assert [entry["period"] for entry in categories["revenue"]] == ["Mar 2023", "Mar 2024", "Mar 2025"]
    assert categories["revenue"][-1]["value"] == 100


def test_build_income_statement_returns_none_without_data() -> None:
    assert build_income_statement({}) is None


def test_build_share_holdings_sums_related_subcategories() -> None:
    stock = {
        "shareHoldingPattern": {
            "Jun '26": {
                "promoters": {"individual": {"percent": 40.0}, "corporation": {"percent": 5.0}},
                "foreignInstitutions": {"percent": 12.0},
            }
        }
    }
    result = build_share_holdings(stock)
    by_category = {item["category"]: item["history"] for item in result["data"]}
    assert by_category["promoters"][0]["period"] == "Jun 2026"
    assert by_category["promoters"][0]["value"] == 45.0
    assert by_category["fii"][0]["value"] == 12.0


def test_build_competitors_maps_peer_list() -> None:
    stock = {
        "similarAssets": {
            "peerList": [
                {
                    "companyHeader": {"displayName": "Peer Co", "nseScriptCode": "PEER"},
                    "marketCap": 1000,
                    "peRatio": 20.0,
                    "pbRatio": 3.0,
                }
            ]
        }
    }
    result = build_competitors(stock)
    assert result["data"] == [
        {"name": "Peer Co", "nse_symbol": "PEER", "market_cap": 1000, "pe_ratio": 20.0, "pb_ratio": 3.0}
    ]


def test_build_competitors_returns_none_without_peers() -> None:
    assert build_competitors({}) is None


class RecordingUpstoxClient:
    def __init__(self, response: Any = None, error: UpstoxAPIError | None = None):
        self.response = response
        self.error = error
        self.calls: list[tuple[str, str, str, object]] = []

    def get_fundamental_data(
        self, access_token: str, isin: str, endpoint: str, params: object = None
    ) -> dict[str, Any]:
        self.calls.append((access_token, isin, endpoint, params))
        if self.error is not None:
            raise self.error
        return self.response


GROWW_OUTPUTS = {
    "profile": {"status": "success", "data": {"company_profile": "x", "sector": "IT"}},
    "key-ratios": {"status": "success", "data": [{"name": "P/E", "company_value": 1, "sector_value": None}]},
}


def test_fallback_used_when_upstox_returns_empty_data(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = RecordingUpstoxClient(response={"status": "success", "data": {}})
    monkeypatch.setattr(fundamentals, "fetch_groww_fundamentals", lambda isin: dict(GROWW_OUTPUTS))
    fallback = GrowwFundamentalFallback(upstream)

    result = fallback.get_fundamental_data("token", "INE0NT901020", "profile")

    assert result == GROWW_OUTPUTS["profile"]


def test_fallback_used_when_upstox_raises_non_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = RecordingUpstoxClient(error=UpstoxAPIError("boom", 500))
    monkeypatch.setattr(fundamentals, "fetch_groww_fundamentals", lambda isin: dict(GROWW_OUTPUTS))
    fallback = GrowwFundamentalFallback(upstream)

    result = fallback.get_fundamental_data("token", "INE0NT901020", "key-ratios")

    assert result == GROWW_OUTPUTS["key-ratios"]


def test_auth_errors_are_never_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = RecordingUpstoxClient(error=UpstoxAPIError("unauthorized", 401))
    monkeypatch.setattr(
        fundamentals,
        "fetch_groww_fundamentals",
        lambda isin: pytest.fail("groww should not be consulted for auth errors"),
    )
    fallback = GrowwFundamentalFallback(upstream)

    with pytest.raises(UpstoxAPIError):
        fallback.get_fundamental_data("token", "INE0NT901020", "profile")


def test_original_upstox_error_propagates_when_groww_has_no_data(monkeypatch: pytest.MonkeyPatch) -> None:
    error = UpstoxAPIError("boom", 500)
    upstream = RecordingUpstoxClient(error=error)
    monkeypatch.setattr(fundamentals, "fetch_groww_fundamentals", lambda isin: {})
    fallback = GrowwFundamentalFallback(upstream)

    with pytest.raises(UpstoxAPIError) as excinfo:
        fallback.get_fundamental_data("token", "INE0NT901020", "balance-sheet")
    assert excinfo.value is error


def test_upstox_success_payload_returned_untouched_when_groww_has_no_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_payload = {"status": "success", "data": {}}
    upstream = RecordingUpstoxClient(response=empty_payload)
    monkeypatch.setattr(fundamentals, "fetch_groww_fundamentals", lambda isin: {})
    fallback = GrowwFundamentalFallback(upstream)

    result = fallback.get_fundamental_data("token", "INE0NT901020", "balance-sheet")

    assert result is empty_payload


def test_groww_lookup_is_cached_per_isin(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_fetch(isin: str) -> dict[str, Any]:
        calls.append(isin)
        return dict(GROWW_OUTPUTS)

    upstream = RecordingUpstoxClient(response={"status": "success", "data": {}})
    monkeypatch.setattr(fundamentals, "fetch_groww_fundamentals", fake_fetch)
    fallback = GrowwFundamentalFallback(upstream)

    fallback.get_fundamental_data("token", "INE0NT901020", "profile")
    fallback.get_fundamental_data("token", "INE0NT901020", "key-ratios")

    assert calls == ["INE0NT901020"]


def test_groww_lookup_failure_falls_back_to_original_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    empty_payload = {"status": "success", "data": {}}
    upstream = RecordingUpstoxClient(response=empty_payload)

    def failing_fetch(isin: str) -> dict[str, Any]:
        raise GrowwLookupError("not found")

    monkeypatch.setattr(fundamentals, "fetch_groww_fundamentals", failing_fetch)
    fallback = GrowwFundamentalFallback(upstream)

    result = fallback.get_fundamental_data("token", "INE0NT901020", "profile")

    assert result is empty_payload


def test_upstox_success_payload_used_when_it_has_data(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"status": "success", "data": {"company_profile": "real", "sector": "IT"}}
    upstream = RecordingUpstoxClient(response=payload)
    monkeypatch.setattr(
        fundamentals,
        "fetch_groww_fundamentals",
        lambda isin: pytest.fail("groww should not be consulted when Upstox has data"),
    )
    fallback = GrowwFundamentalFallback(upstream)

    result = fallback.get_fundamental_data("token", "INE0NT901020", "profile")

    assert result is payload
