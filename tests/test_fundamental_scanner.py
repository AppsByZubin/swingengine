from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fundamental.scanner import (
    FUNDAMENTAL_REQUESTS,
    FundamentalScanError,
    NSEFundamentalScanner,
    analyze_fundamental_payloads,
)
from upstox.assets import AssetSearchResult
from upstox.client import UpstoxAPIError
from upstox.store import TokenState


def asset(symbol: str, isin: str) -> AssetSearchResult:
    return AssetSearchResult(
        trading_symbol=symbol,
        name=f"{symbol} LIMITED",
        segment="NSE_EQ",
        instrument_type="EQ",
        instrument_key=f"NSE_EQ|{isin}",
        isin=isin,
    )


class Catalog:
    def __init__(self, equities: list[AssetSearchResult]):
        self.equities = equities
        self.events: list[str] = []

    def refresh(self) -> int:
        self.events.append("refresh")
        return 12_345

    def list_equities(self) -> list[AssetSearchResult]:
        self.events.append("list")
        return self.equities


class Store:
    def __init__(self, valid: bool = True):
        self.valid = valid

    def load(self) -> TokenState:
        return TokenState(
            access_token="token" if self.valid else "",
            validation_status="valid" if self.valid else "unchecked",
        )


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.fail_endpoint = ""
        self.failure_status = 500

    def get_fundamental_data(
        self,
        access_token: str,
        isin: str,
        endpoint: str,
        params: object = None,
    ) -> dict[str, Any]:
        assert access_token == "token"
        self.calls.append((isin, endpoint, params))
        if endpoint == self.fail_endpoint:
            raise UpstoxAPIError("endpoint failed", self.failure_status)
        return {"status": "success", "data": {}, "isin": isin}


def fake_analysis(
    payloads: dict[str, Any],
    threshold: float,
) -> dict[str, Any]:
    isin = payloads["profile"]["isin"]
    score = 82.5 if isin == "INE044A01036" else 60.0
    return {
        "decision": "GOOD" if score >= threshold else "NOT GOOD ENOUGH",
        "score": score,
        "rating": "STRONG" if score >= 80 else "MIXED",
        "confidence": {"score": 95.0},
        "sector": "Pharmaceuticals",
        "latest_financial_period": "Mar 2026",
    }


def test_scanner_refreshes_and_exports_only_good_companies() -> None:
    catalog = Catalog(
        [
            asset("SUNPHARMA", "INE044A01036"),
            asset("RELIANCE", "INE002A01018"),
        ]
    )
    client = Client()
    sleep_calls: list[float] = []
    scanner = NSEFundamentalScanner(
        catalog,
        client,
        Store(),
        sleep_function=sleep_calls.append,
        analyze_payloads=fake_analysis,  # type: ignore[arg-type]
        progress_interval=1,
    )

    result = scanner.scan(now=datetime(2026, 8, 5, tzinfo=UTC))

    assert catalog.events == ["refresh", "list"]
    assert result.catalog_instruments == 12_345
    assert result.equity_assets == 2
    assert result.evaluated == 2
    assert result.failed == 0
    assert result.skipped == 0
    assert result.endpoint_failures == 0
    assert [stock.trading_symbol for stock in result.stocks] == ["SUNPHARMA"]
    assert result.stocks[0].score == 82.5
    assert [call[1] for call in client.calls[:8]] == [
        request[1] for request in FUNDAMENTAL_REQUESTS
    ]
    assert client.calls[2][2] == {"type": "consolidated", "fs": "true"}
    assert len(client.calls) == 16
    assert sleep_calls == [0.125] * 15


def test_non_auth_endpoint_failure_reduces_confidence_but_continues() -> None:
    client = Client()
    client.fail_endpoint = "competitors"
    scanner = NSEFundamentalScanner(
        Catalog([asset("SUNPHARMA", "INE044A01036")]),
        client,
        Store(),
        request_interval_seconds=0,
        analyze_payloads=fake_analysis,  # type: ignore[arg-type]
    )

    result = scanner.scan(now=datetime(2026, 8, 5, tzinfo=UTC))

    assert result.evaluated == 1
    assert result.endpoint_failures == 1
    assert len(result.stocks) == 1


def test_auth_endpoint_failure_stops_the_scan() -> None:
    client = Client()
    client.fail_endpoint = "profile"
    client.failure_status = 401
    scanner = NSEFundamentalScanner(
        Catalog([asset("SUNPHARMA", "INE044A01036")]),
        client,
        Store(),
        request_interval_seconds=0,
        analyze_payloads=fake_analysis,  # type: ignore[arg-type]
    )

    with pytest.raises(FundamentalScanError, match="rejected the access token"):
        scanner.scan(now=datetime(2026, 8, 5, tzinfo=UTC))

    assert len(client.calls) == 1


def test_invalid_and_duplicate_isins_are_skipped() -> None:
    client = Client()
    scanner = NSEFundamentalScanner(
        Catalog(
            [
                asset("NOISIN", "not-an-isin"),
                asset("SUN", "INE044A01036"),
                asset("SUN2", "INE044A01036"),
            ]
        ),
        client,
        Store(),
        request_interval_seconds=0,
        analyze_payloads=fake_analysis,  # type: ignore[arg-type]
    )

    result = scanner.scan(now=datetime(2026, 8, 5, tzinfo=UTC))

    assert result.skipped == 2
    assert result.evaluated == 1
    assert len(client.calls) == 8


def test_valid_stored_token_is_required() -> None:
    scanner = NSEFundamentalScanner(
        Catalog([asset("SUN", "INE044A01036")]),
        Client(),
        Store(valid=False),
        request_interval_seconds=0,
    )

    with pytest.raises(FundamentalScanError, match="valid Upstox token"):
        scanner.scan(now=datetime(2026, 8, 5, tzinfo=UTC))


def test_payload_adapter_runs_without_writing_temporary_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_file_write(*args: object, **kwargs: object) -> int:
        raise AssertionError("in-memory analysis must not write files")

    monkeypatch.setattr(Path, "write_text", reject_file_write)
    payloads = {
        "profile": {
            "status": "success",
            "data": {
                "company_profile": "Example Limited is a manufacturer.",
                "sector": "Industrials",
            },
        },
        "key_ratios": {
            "status": "success",
            "data": [
                {"name": "P/E", "company_value": "10", "sector_value": "20"},
                {"name": "P/B", "company_value": "1", "sector_value": "2"},
                {"name": "EV/EBITDA", "company_value": "5", "sector_value": "10"},
                {"name": "ROA", "company_value": "12%", "sector_value": "8%"},
                {"name": "ROE", "company_value": "25%", "sector_value": "18%"},
                {"name": "ROCE", "company_value": "25%", "sector_value": "18%"},
            ],
        },
    }

    result = analyze_fundamental_payloads(payloads, 70.0)

    assert result["company"] == "Example Limited"
    assert result["score"] >= 70
    assert result["decision"] == "GOOD"
