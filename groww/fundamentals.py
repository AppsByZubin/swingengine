"""Fallback fundamentals for ISINs Upstox's fundamentals API returns empty for.

Upstox's fundamental-data vendor does not cover every NSE-listed company
(small/mid-caps are common gaps). When that happens, this module scrapes the
public stock page on groww.in -- which embeds a Next.js ``__NEXT_DATA__`` JSON
payload containing real fundamentals, no login required -- and reshapes the
result into the same payload shape Upstox's ``/v2/fundamentals/*`` endpoints
return, so ``fundamental.analyzer.FundamentalAnalyzer`` can consume it
unchanged.

groww's page does not expose full balance-sheet or cash-flow line items (only
revenue/profit/net worth), so the ``balance-sheet`` and ``cash-flow``
endpoints are never produced by this fallback.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import logging
import re
from threading import Lock
from typing import Any

import requests

from upstox.client import FUNDAMENTAL_ENDPOINTS, UpstoxAPIError

LOGGER = logging.getLogger(__name__)

SEARCH_URL = "https://groww.in/v1/api/search/v3/query/global/st_query"
STOCK_URL = "https://groww.in/stocks/{search_id}"
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
REQUEST_TIMEOUT = 20

_NEXT_DATA_PATTERN = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


class GrowwLookupError(RuntimeError):
    """Raised when an ISIN can't be resolved or its page can't be parsed."""


def resolve_search_id(isin: str) -> str:
    """Resolve an NSE ISIN to groww's stock-page URL slug."""
    response = requests.get(
        SEARCH_URL,
        params={"query": isin},
        headers=REQUEST_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    content = ((response.json().get("data") or {}).get("content")) or []
    for item in content:
        if (
            item.get("entity_type") == "Stocks"
            and str(item.get("isin") or "").upper() == isin.upper()
        ):
            return str(item["search_id"])
    raise GrowwLookupError(f"No groww.in listing found for ISIN {isin!r}")


def fetch_stock_data(search_id: str) -> dict[str, Any]:
    """Fetch a groww stock page and return its embedded ``stockData`` payload."""
    response = requests.get(
        STOCK_URL.format(search_id=search_id),
        headers=REQUEST_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    match = _NEXT_DATA_PATTERN.search(response.text)
    if match is None:
        raise GrowwLookupError(
            f"groww.in page for {search_id!r} had no __NEXT_DATA__ payload"
        )
    try:
        payload = json.loads(match.group(1))
        return payload["props"]["pageProps"]["stockData"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise GrowwLookupError(
            f"Unexpected groww.in page structure for {search_id!r}"
        ) from exc


_MONTH_NUMBERS = {
    name: index
    for index, name in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}


def _quarter_to_period(label: str) -> str:
    """Convert groww's "Jun '26" quarter labels to "Jun 2026" (Upstox's style)."""
    match = re.match(r"([A-Za-z]{3,9})\s*'(\d{2})", label.strip())
    if not match:
        return label
    month, year_suffix = match.groups()
    return f"{month} 20{year_suffix}"


def _quarter_sort_key(label: str) -> tuple[int, int]:
    """Chronological sort key for groww's "Jun '26" quarter labels."""
    match = re.match(r"([A-Za-z]{3,9})\s*'(\d{2})", label.strip())
    if not match:
        return (0, 0)
    month, year_suffix = match.groups()
    return (2000 + int(year_suffix), _MONTH_NUMBERS.get(month[:3].title(), 0))


def build_profile(stock: dict[str, Any]) -> dict[str, Any]:
    header = stock.get("header") or {}
    details = stock.get("details") or {}
    full_name = details.get("fullName") or header.get("displayName")
    summary = details.get("businessSummary") or ""

    # analyze_fundamentals's company-name heuristic looks for a leading
    # "<Name> is ..." clause; groww's summary text doesn't use that phrasing,
    # so prefix it to keep the report title from falling back to "Unknown company".
    company_profile = (
        f"{full_name} Limited is a company that {summary}" if full_name and summary else summary
    )

    return {
        "status": "success",
        "data": {
            "company_profile": company_profile,
            "sector": header.get("industryName") or "Unknown",
        },
    }


def build_key_ratios(stock: dict[str, Any]) -> dict[str, Any]:
    fundamentals = stock.get("fundamentals") or []
    by_name = {item.get("name"): item.get("value") for item in fundamentals if item.get("name")}

    ratios = []
    if "P/E Ratio(TTM)" in by_name:
        ratios.append(
            {
                "name": "P/E",
                "company_value": by_name["P/E Ratio(TTM)"],
                "sector_value": by_name.get("Industry P/E"),
            }
        )
    if "EPS(TTM)" in by_name:
        ratios.append({"name": "EPS", "company_value": by_name["EPS(TTM)"], "sector_value": None})
    if "P/B Ratio" in by_name:
        ratios.append({"name": "P/B", "company_value": by_name["P/B Ratio"], "sector_value": None})
    if "ROE" in by_name:
        ratios.append({"name": "ROE", "company_value": by_name["ROE"], "sector_value": None})
    if "Debt to Equity" in by_name:
        ratios.append(
            {"name": "Debt to Equity", "company_value": by_name["Debt to Equity"], "sector_value": None}
        )
    if "Dividend Yield" in by_name:
        ratios.append(
            {"name": "Dividend Yield", "company_value": by_name["Dividend Yield"], "sector_value": None}
        )
    if "Book Value" in by_name:
        ratios.append(
            {"name": "Book Value", "company_value": by_name["Book Value"], "sector_value": None}
        )

    return {"status": "success", "data": ratios}


def build_income_statement(stock: dict[str, Any]) -> dict[str, Any] | None:
    statement = stock.get("financialStatement") or []
    by_title = {item.get("title"): item for item in statement if item.get("title")}

    categories = []
    for title, category_name in (("Revenue", "revenue"), ("Profit", "net_profit")):
        entry = by_title.get(title)
        if not entry or not entry.get("yearly"):
            continue
        history = [
            {"period": f"Mar {year}", "value": value}
            for year, value in sorted(entry["yearly"].items(), key=lambda item: item[0])
        ]
        categories.append({"category": category_name, "history": history})

    if not categories:
        return None
    return {
        "status": "success",
        "data": {
            "type": "standalone",
            "time_period": "yearly",
            "units_in": "crore",
            "income_statement": categories,
        },
    }


def build_share_holdings(stock: dict[str, Any]) -> dict[str, Any] | None:
    pattern = stock.get("shareHoldingPattern") or {}
    if not pattern:
        return None

    series: dict[str, list[dict[str, Any]]] = {
        "promoters": [],
        "fii": [],
        "otherdii": [],
        "mutualfunds": [],
        "retail": [],
    }
    for period_label, breakdown in sorted(pattern.items(), key=lambda item: _quarter_sort_key(item[0])):
        period = _quarter_to_period(period_label)

        promoters = breakdown.get("promoters") or {}
        promoter_pct = sum(
            (promoters.get(key) or {}).get("percent") or 0.0 for key in ("individual", "government", "corporation")
        )
        series["promoters"].append({"period": period, "value": promoter_pct})

        series["fii"].append(
            {"period": period, "value": (breakdown.get("foreignInstitutions") or {}).get("percent") or 0.0}
        )
        series["mutualfunds"].append(
            {"period": period, "value": (breakdown.get("mutualFunds") or {}).get("percent") or 0.0}
        )

        other_dii = breakdown.get("otherDomesticInstitutions") or {}
        other_dii_pct = sum((other_dii.get(key) or {}).get("percent") or 0.0 for key in ("insurance", "otherFirms"))
        series["otherdii"].append({"period": period, "value": other_dii_pct})

        series["retail"].append(
            {"period": period, "value": (breakdown.get("retailAndOthers") or {}).get("percent") or 0.0}
        )

    data = [{"category": category, "history": history} for category, history in series.items() if history]
    return {"status": "success", "data": data} if data else None


def build_competitors(stock: dict[str, Any]) -> dict[str, Any] | None:
    peers = ((stock.get("similarAssets") or {}).get("peerList")) or []
    if not peers:
        return None
    data = [
        {
            "name": (peer.get("companyHeader") or {}).get("displayName"),
            "nse_symbol": (peer.get("companyHeader") or {}).get("nseScriptCode"),
            "market_cap": peer.get("marketCap"),
            "pe_ratio": peer.get("peRatio"),
            "pb_ratio": peer.get("pbRatio"),
        }
        for peer in peers
    ]
    return {"status": "success", "data": data}


BUILDERS = {
    "profile": build_profile,
    "key-ratios": build_key_ratios,
    "income-statement": build_income_statement,
    "share-holdings": build_share_holdings,
    "competitors": build_competitors,
}
assert set(BUILDERS) <= FUNDAMENTAL_ENDPOINTS


def fetch_groww_fundamentals(isin: str) -> dict[str, dict[str, Any]]:
    """Fetch groww.in data for ``isin`` and return {upstox_endpoint: payload}.

    Endpoints whose source data isn't available on the groww page are omitted
    rather than returned with placeholder content.
    """
    search_id = resolve_search_id(isin)
    stock = fetch_stock_data(search_id)

    outputs: dict[str, dict[str, Any]] = {}
    for endpoint, builder in BUILDERS.items():
        payload = builder(stock)
        if payload is not None:
            outputs[endpoint] = payload
    return outputs


def _has_usable_data(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    if str(payload.get("status", "")).casefold() != "success":
        return False
    data = payload.get("data")
    if isinstance(data, (Mapping, list, tuple, set, str, bytes)):
        return bool(data)
    return data is not None


class GrowwFundamentalFallback:
    """Fill Upstox fundamentals gaps from groww.in.

    Wraps another ``get_fundamental_data(access_token, isin, endpoint,
    params)`` client (matching ``fundamental.scanner.FundamentalClient``).
    The wrapped call is tried first; groww.in is only consulted when it
    raises a non-auth ``UpstoxAPIError`` or returns a payload with no usable
    data, which is how Upstox represents a company its fundamentals vendor
    doesn't cover.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._cache: dict[str, dict[str, dict[str, Any]] | None] = {}
        self._lock = Lock()

    def get_fundamental_data(
        self,
        access_token: str,
        isin: str,
        endpoint: str,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            payload = self._client.get_fundamental_data(access_token, isin, endpoint, params)
        except UpstoxAPIError as error:
            if error.status_code in {401, 403}:
                raise
            fallback = self._groww_payload(isin, endpoint)
            if fallback is None:
                raise
            LOGGER.info(
                "Using groww.in fallback for fundamentals isin=%r endpoint=%r "
                "after Upstox error=%s",
                isin,
                endpoint,
                error,
            )
            return fallback

        if _has_usable_data(payload):
            return payload
        fallback = self._groww_payload(isin, endpoint)
        if fallback is None:
            return payload
        LOGGER.info(
            "Using groww.in fallback for fundamentals isin=%r endpoint=%r "
            "after Upstox returned no usable data",
            isin,
            endpoint,
        )
        return fallback

    def _groww_payload(self, isin: str, endpoint: str) -> dict[str, Any] | None:
        outputs = self._cached_outputs(isin)
        return outputs.get(endpoint) if outputs else None

    def _cached_outputs(self, isin: str) -> dict[str, dict[str, Any]] | None:
        with self._lock:
            if isin in self._cache:
                return self._cache[isin]
        try:
            outputs = fetch_groww_fundamentals(isin)
        except (GrowwLookupError, requests.RequestException) as error:
            LOGGER.warning("groww.in fallback lookup failed isin=%r error=%s", isin, error)
            outputs = None
        with self._lock:
            self._cache[isin] = outputs
        return outputs
