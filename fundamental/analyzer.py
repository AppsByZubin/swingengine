#!/usr/bin/env python3
"""Explainable fundamental analysis for Upstox fundamental-data exports.

The analyzer reads the JSON endpoint responses in a directory such as ``funda/``
and evaluates six pillars:

* valuation versus the reported sector
* profitability and capital efficiency (highest-weighted pillar; includes EPS,
  with EPS > 18 treated as a high-priority earnings-quality bar)
* multi-year growth
* balance-sheet and liquidity health
* cash-flow quality
* shareholder returns (when available)

It deliberately does not call total liabilities "debt" or operating cash flow
plus investing cash flow "free cash flow" because the supplied API fields are
not detailed enough to support those claims.

This is a screening tool, not personalized investment advice.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence


ENDPOINT_FILES = {
    "profile": "profile.json",
    "key_ratios": "key_ratio.json",
    "balance_sheet": "balance_sheet.json",
    "income_statement": "income_statement.json",
    "cash_flow": "cash_flow.json",
    "corporate_actions": "corporate_action.json",
    "share_holdings": "share_holdings.json",
    "competitors": "competitor.json",
}

MANDATORY_ENDPOINTS = (
    "profile",
    "balance_sheet",
    "cash_flow",
    "income_statement",
    "key_ratios",
)

OPTIONAL_ENDPOINTS = (
    "competitors",
    "corporate_actions",
    "share_holdings",
)

# Confidence weights reflect how important an endpoint is to this analysis.
ENDPOINT_CONFIDENCE_WEIGHTS = {
    "profile": 0.05,
    "key_ratios": 0.20,
    "balance_sheet": 0.18,
    "income_statement": 0.20,
    "cash_flow": 0.18,
    "corporate_actions": 0.04,
    "share_holdings": 0.10,
    "competitors": 0.05,
}


@dataclass
class DataIssue:
    endpoint: str
    message: str
    code: Optional[str] = None


@dataclass
class CategoryScore:
    name: str
    score: Optional[float]
    weight: float
    metrics: dict[str, Any] = field(default_factory=dict)
    strengths: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def interpolate_score(value: float, points: Sequence[tuple[float, float]]) -> float:
    """Linearly interpolate a 0-100 score between ordered (value, score) points."""
    ordered = sorted(points, key=lambda point: point[0])
    if value <= ordered[0][0]:
        return clamp(ordered[0][1])
    if value >= ordered[-1][0]:
        return clamp(ordered[-1][1])

    for (left_x, left_score), (right_x, right_score) in zip(ordered, ordered[1:]):
        if left_x <= value <= right_x:
            width = right_x - left_x
            if width == 0:
                return clamp(right_score)
            fraction = (value - left_x) / width
            return clamp(left_score + fraction * (right_score - left_score))
    return 0.0


def weighted_average(items: Iterable[tuple[Optional[float], float]]) -> Optional[float]:
    available = [(score, weight) for score, weight in items if score is not None]
    total_weight = sum(weight for _, weight in available)
    if not available or total_weight <= 0:
        return None
    return sum(float(score) * weight for score, weight in available) / total_weight


def parse_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) else None
    if not isinstance(value, str):
        return None

    cleaned = value.strip().replace(",", "").replace("₹", "").replace("%", "")
    if not cleaned or cleaned.lower() in {"na", "n/a", "none", "null", "-"}:
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1]
    try:
        result = float(cleaned)
    except ValueError:
        return None
    if not math.isfinite(result):
        return None
    return -result if negative else result


def safe_divide(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def period_sort_key(period: str) -> tuple[int, int, str]:
    match = re.search(r"([A-Za-z]{3,9})\s+(\d{4})", period)
    if match:
        month_text, year_text = match.groups()
        for fmt in ("%b", "%B"):
            try:
                month = datetime.strptime(month_text, fmt).month
                return int(year_text), month, period
            except ValueError:
                continue
    year_match = re.search(r"(19|20)\d{2}", period)
    return (int(year_match.group(0)) if year_match else 0, 0, period)


def history_series(history: Any) -> list[tuple[str, float]]:
    if not isinstance(history, list):
        return []
    result: list[tuple[str, float]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        period = str(item.get("period") or "Unknown")
        value = parse_number(item.get("value"))
        if value is not None:
            result.append((period, value))
    return sorted(result, key=lambda item: period_sort_key(item[0]))


def latest_value(series: Sequence[tuple[str, float]]) -> Optional[float]:
    return series[-1][1] if series else None


def latest_period(series: Sequence[tuple[str, float]]) -> Optional[str]:
    return series[-1][0] if series else None


def latest_growth(series: Sequence[tuple[str, float]]) -> Optional[float]:
    if len(series) < 2 or series[-2][1] == 0:
        return None
    return (series[-1][1] / series[-2][1] - 1.0) * 100.0


def series_cagr(series: Sequence[tuple[str, float]]) -> Optional[float]:
    if len(series) < 2:
        return None
    first, last = series[0][1], series[-1][1]
    if first <= 0 or last < 0:
        return None
    first_year = period_sort_key(series[0][0])[0]
    last_year = period_sort_key(series[-1][0])[0]
    years = last_year - first_year
    if years <= 0:
        years = len(series) - 1
    if years <= 0:
        return None
    return ((last / first) ** (1.0 / years) - 1.0) * 100.0


def ratio_change_percent(company: float, sector: float) -> Optional[float]:
    if sector == 0:
        return None
    return (company / sector - 1.0) * 100.0


def growth_score(value: float) -> float:
    return interpolate_score(
        value,
        [(-20, 0), (-5, 5), (0, 20), (5, 45), (10, 70), (15, 88), (25, 100)],
    )


def valuation_score(company: float, sector: float) -> Optional[float]:
    if company <= 0 or sector <= 0:
        return 0.0 if company <= 0 else None
    relative = company / sector
    return interpolate_score(
        relative,
        [(0.50, 100), (0.75, 95), (0.90, 85), (1.00, 72), (1.15, 52),
         (1.30, 32), (1.50, 15), (2.00, 0)],
    )


def profitability_score(name: str, company: float, sector: Optional[float]) -> float:
    absolute_points = {
        "ROE": [(0, 0), (5, 20), (10, 50), (15, 75), (20, 90), (25, 100)],
        "ROCE": [(0, 0), (5, 20), (10, 50), (15, 75), (20, 90), (25, 100)],
        "ROA": [(0, 0), (2, 20), (5, 60), (8, 85), (12, 100)],
    }
    absolute = interpolate_score(company, absolute_points[name])
    if sector is None or sector <= 0:
        return absolute
    relative = interpolate_score(
        company / sector,
        [(0, 0), (0.50, 25), (0.75, 50), (1.00, 75), (1.25, 100)],
    )
    return 0.60 * absolute + 0.40 * relative


# High-priority earnings-quality bar: crossing it is scored as a step into
# "good" territory (70+) rather than a smooth continuation of the curve below it.
EPS_HIGH_PRIORITY_THRESHOLD = 18.0


def eps_score(value: float) -> float:
    return interpolate_score(
        value,
        [(0, 0), (5, 15), (10, 35), (15, 55), (18, 70), (22, 82), (30, 92), (45, 100)],
    )


def fmt_number(value: Any, decimals: int = 2) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return str(value)
    if abs(value) >= 1_000:
        return f"{value:,.{decimals}f}"
    return f"{value:.{decimals}f}"


def _data_available(data: Any) -> bool:
    """Return whether an endpoint supplied non-empty analysis data."""
    if data is None:
        return False
    if isinstance(data, (dict, list, tuple, set, str, bytes)):
        return bool(data)
    return True


class FundamentalAnalyzer:
    """Analyze file-backed or in-memory Upstox fundamentals responses."""

    def __init__(
        self,
        folder: Path | str | None,
        good_threshold: float = 70.0,
        *,
        source_payloads: Mapping[str, Any] | None = None,
    ) -> None:
        self.folder = Path(folder) if folder is not None else None
        self.good_threshold = good_threshold
        self.source_payloads = (
            None if source_payloads is None else dict(source_payloads)
        )
        self.payloads: dict[str, Any] = {}
        self.endpoint_status: dict[str, str] = {}
        self.issues: list[DataIssue] = []

    @classmethod
    def from_payloads(
        cls,
        payloads: Mapping[str, Any],
        good_threshold: float = 70.0,
    ) -> "FundamentalAnalyzer":
        """Build an analyzer that needs no temporary JSON files."""
        return cls(
            None,
            good_threshold,
            source_payloads=payloads,
        )

    def _load(self) -> None:
        # Make repeated calls to analyze() deterministic for library users.
        self.payloads.clear()
        self.endpoint_status.clear()
        self.issues.clear()

        if self.source_payloads is not None:
            for endpoint, filename in ENDPOINT_FILES.items():
                if endpoint not in self.source_payloads:
                    self.endpoint_status[endpoint] = "missing"
                    self.issues.append(
                        DataIssue(endpoint, f"Missing payload: {filename}")
                    )
                    continue
                self._record_payload(
                    endpoint,
                    self.source_payloads[endpoint],
                )
            return

        if self.folder is None or not self.folder.is_dir():
            raise ValueError(
                f"Fundamentals folder does not exist: {self.folder}"
            )

        for endpoint, filename in ENDPOINT_FILES.items():
            path = self.folder / filename
            if not path.exists():
                self.endpoint_status[endpoint] = "missing"
                self.issues.append(DataIssue(endpoint, f"Missing file: {filename}"))
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                self.endpoint_status[endpoint] = "invalid"
                self.issues.append(DataIssue(endpoint, f"Could not read valid JSON: {exc}"))
                continue

            self._record_payload(endpoint, payload)

    def _record_payload(self, endpoint: str, payload: Any) -> None:
        self.payloads[endpoint] = payload
        if not isinstance(payload, dict):
            self.endpoint_status[endpoint] = "invalid"
            self.issues.append(
                DataIssue(endpoint, "Endpoint response is not a JSON object")
            )
            return

        status = str(payload.get("status", "unknown")).lower()
        if status == "success" and "data" in payload:
            if _data_available(payload["data"]):
                self.endpoint_status[endpoint] = "success"
            else:
                self.endpoint_status[endpoint] = "unavailable"
                self.issues.append(
                    DataIssue(endpoint, "Endpoint returned no data")
                )
            return

        self.endpoint_status[endpoint] = "error"
        errors = payload.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            error = errors[0]
            code = error.get("errorCode") or error.get("error_code")
            message = error.get("message") or f"API status was {status}"
            self.issues.append(
                DataIssue(
                    endpoint,
                    str(message),
                    str(code) if code else None,
                )
            )
        else:
            self.issues.append(DataIssue(endpoint, f"API status was {status}"))

    def data(self, endpoint: str) -> Any:
        payload = self.payloads.get(endpoint)
        if self.endpoint_status.get(endpoint) != "success" or not isinstance(payload, dict):
            return None
        return payload.get("data")

    def ratio_map(self) -> dict[str, tuple[Optional[float], Optional[float]]]:
        result: dict[str, tuple[Optional[float], Optional[float]]] = {}
        data = self.data("key_ratios")
        if not isinstance(data, list):
            return result
        for item in data:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            result[str(item["name"]).strip().upper()] = (
                parse_number(item.get("company_value")),
                parse_number(item.get("sector_value")),
            )
        return result

    def category_series(self, endpoint: str, section: str, category: str) -> list[tuple[str, float]]:
        data = self.data(endpoint)
        if not isinstance(data, dict) or not isinstance(data.get(section), list):
            return []
        target = normalize_name(category)
        for item in data[section]:
            if isinstance(item, dict) and normalize_name(str(item.get("category", ""))) == target:
                return history_series(item.get("history"))
        return []

    def statement_series(self, endpoint: str, particular: str) -> list[tuple[str, float]]:
        data = self.data(endpoint)
        if not isinstance(data, dict) or not isinstance(data.get("full_statement"), list):
            return []
        target = normalize_name(particular)
        for item in data["full_statement"]:
            if isinstance(item, dict) and normalize_name(str(item.get("particular", ""))) == target:
                return history_series(item.get("history"))
        return []

    def analyze_valuation(self, ratios: dict[str, tuple[Optional[float], Optional[float]]]) -> CategoryScore:
        category = CategoryScore("Valuation", None, 20.0)
        components: list[tuple[Optional[float], float]] = []
        configured = [("P/E", 0.35), ("P/B", 0.25), ("EV/EBITDA", 0.40)]

        for name, weight in configured:
            company, sector = ratios.get(name, (None, None))
            if company is None:
                category.caveats.append(f"{name} is unavailable.")
                components.append((None, weight))
                continue
            category.metrics[f"{name} company"] = round(company, 2)
            if sector is None:
                category.caveats.append(f"Sector {name} is unavailable; relative valuation cannot be scored.")
                components.append((None, weight))
                continue
            category.metrics[f"{name} sector"] = round(sector, 2)
            premium = ratio_change_percent(company, sector)
            if premium is not None:
                category.metrics[f"{name} premium/discount %"] = round(premium, 2)
                if premium > 10:
                    category.risks.append(
                        f"{name} is {premium:.1f}% above the reported sector ({company:.2f} vs {sector:.2f})."
                    )
                elif premium < -10:
                    category.strengths.append(
                        f"{name} is {-premium:.1f}% below the reported sector ({company:.2f} vs {sector:.2f})."
                    )
                else:
                    category.strengths.append(f"{name} is close to the reported sector benchmark.")
            components.append((valuation_score(company, sector), weight))

        category.score = weighted_average(components)
        category.caveats.append("Relative multiples can miss differences in growth, business mix, and leverage.")
        return category

    def analyze_profitability(
        self, ratios: dict[str, tuple[Optional[float], Optional[float]]]
    ) -> CategoryScore:
        category = CategoryScore("Profitability", None, 30.0)
        components: list[tuple[Optional[float], float]] = []
        weights = {"ROE": 0.35, "ROCE": 0.30, "ROA": 0.15}

        for name, weight in weights.items():
            company, sector = ratios.get(name, (None, None))
            if company is None:
                category.caveats.append(f"{name} is unavailable.")
                components.append((None, weight))
                continue
            category.metrics[f"{name} %"] = round(company, 2)
            if sector is not None:
                category.metrics[f"{name} sector %"] = round(sector, 2)
                relative = safe_divide(company, sector)
                if relative is not None and relative < 0.75:
                    category.risks.append(
                        f"{name} of {company:.2f}% is materially below the sector's {sector:.2f}%."
                    )
                elif relative is not None and relative >= 1.0:
                    category.strengths.append(
                        f"{name} of {company:.2f}% meets or exceeds the sector's {sector:.2f}%."
                    )
            components.append((profitability_score(name, company, sector), weight))

        eps, _eps_sector = ratios.get("EPS", (None, None))
        eps_weight = 0.20
        if eps is None:
            category.caveats.append("EPS is unavailable.")
            components.append((None, eps_weight))
        else:
            category.metrics["EPS"] = round(eps, 2)
            components.append((eps_score(eps), eps_weight))
            if eps > EPS_HIGH_PRIORITY_THRESHOLD:
                category.strengths.append(
                    f"EPS of {eps:.2f} clears the {EPS_HIGH_PRIORITY_THRESHOLD:.0f} high-priority bar."
                )
            else:
                category.risks.append(
                    f"EPS of {eps:.2f} is below the {EPS_HIGH_PRIORITY_THRESHOLD:.0f} high-priority bar."
                )

        revenue = self.category_series("income_statement", "income_statement", "revenue")
        operating_profit = self.category_series(
            "income_statement", "income_statement", "operating_profit"
        )
        net_profit = self.category_series("income_statement", "income_statement", "net_profit")
        if revenue and operating_profit:
            current_margin = safe_divide(latest_value(operating_profit), latest_value(revenue))
            previous_margin = None
            if len(revenue) >= 2 and len(operating_profit) >= 2:
                previous_margin = safe_divide(operating_profit[-2][1], revenue[-2][1])
            if current_margin is not None:
                category.metrics["reported operating margin %"] = round(current_margin * 100, 2)
            if current_margin is not None and previous_margin is not None:
                change_pp = (current_margin - previous_margin) * 100
                category.metrics["operating margin change pp"] = round(change_pp, 2)
                if change_pp > 0.5:
                    category.strengths.append(f"Reported operating margin expanded {change_pp:.2f} percentage points.")
                elif change_pp < -0.5:
                    category.risks.append(f"Reported operating margin contracted {-change_pp:.2f} percentage points.")
        if revenue and net_profit:
            net_margin = safe_divide(latest_value(net_profit), latest_value(revenue))
            if net_margin is not None:
                category.metrics["net margin %"] = round(net_margin * 100, 2)

        category.score = weighted_average(components)
        return category

    def analyze_growth(self) -> CategoryScore:
        category = CategoryScore("Growth", None, 15.0)
        revenue = self.category_series("income_statement", "income_statement", "revenue")
        operating_profit = self.category_series(
            "income_statement", "income_statement", "operating_profit"
        )
        net_profit = self.category_series("income_statement", "income_statement", "net_profit")
        configured = [
            ("revenue", revenue, 0.20),
            ("operating profit", operating_profit, 0.20),
            ("net profit", net_profit, 0.25),
        ]
        components: list[tuple[Optional[float], float]] = []

        for name, series, weight in configured:
            value = series_cagr(series)
            if value is None:
                category.caveats.append(f"Not enough comparable history for {name} CAGR.")
                components.append((None, weight))
                continue
            years = period_sort_key(series[-1][0])[0] - period_sort_key(series[0][0])[0]
            category.metrics[f"{name} CAGR % ({years or len(series) - 1}y)"] = round(value, 2)
            components.append((growth_score(value), weight))
            if value >= 10:
                category.strengths.append(f"{name.title()} CAGR is a healthy {value:.1f}%.")
            elif value < 5:
                category.risks.append(f"{name.title()} CAGR is weak at {value:.1f}%.")

        for name, series, weight in [
            ("revenue", revenue, 0.15),
            ("net profit", net_profit, 0.20),
        ]:
            value = latest_growth(series)
            if value is None:
                components.append((None, weight))
                continue
            category.metrics[f"latest {name} growth %"] = round(value, 2)
            components.append((growth_score(value), weight))
            if value >= 10:
                category.strengths.append(f"Latest {name} growth accelerated to {value:.1f}%.")
            elif value < 0:
                category.risks.append(f"Latest {name} declined {abs(value):.1f}%.")

        revenue_cagr = series_cagr(revenue)
        profit_cagr = series_cagr(net_profit)
        if revenue_cagr is not None and profit_cagr is not None and profit_cagr > revenue_cagr + 1:
            category.strengths.append("Profit has compounded faster than revenue, indicating improving earnings conversion.")

        eps = self.statement_series("income_statement", "EPS - Basic")
        if len(eps) >= 2 and len(net_profit) >= 2:
            eps_moves = [latest_growth(eps[: index + 1]) for index in range(1, len(eps))]
            profit_moves = [latest_growth(net_profit[: index + 1]) for index in range(1, len(net_profit))]
            for eps_move, profit_move in zip(eps_moves, profit_moves):
                if eps_move is not None and profit_move is not None and abs(eps_move - profit_move) > 30:
                    category.caveats.append(
                        "EPS history has a structural break versus profit growth (possibly a split/bonus/share-count change), so EPS CAGR was not scored."
                    )
                    break

        category.score = weighted_average(components)
        return category

    def analyze_financial_health(
        self, ratios: dict[str, tuple[Optional[float], Optional[float]]]
    ) -> CategoryScore:
        category = CategoryScore("Financial health", None, 15.0)
        # Balance-sheet headline history is a list of records, not categorized series.
        balance_data = self.data("balance_sheet")
        assets = []
        liabilities = []
        if isinstance(balance_data, dict) and isinstance(balance_data.get("history"), list):
            for row in balance_data["history"]:
                if not isinstance(row, dict):
                    continue
                period = str(row.get("period") or "Unknown")
                asset = parse_number(row.get("total_asset"))
                liability = parse_number(row.get("total_liability"))
                if asset is not None:
                    assets.append((period, asset))
                if liability is not None:
                    liabilities.append((period, liability))
            assets.sort(key=lambda item: period_sort_key(item[0]))
            liabilities.sort(key=lambda item: period_sort_key(item[0]))

        equity = self.statement_series("balance_sheet", "Equity Capital")
        current_assets = self.statement_series("balance_sheet", "Current Assets")
        current_liabilities = self.statement_series("balance_sheet", "Current Liabilities")
        net_current_assets = self.statement_series("balance_sheet", "Net Current Asset")
        components: list[tuple[Optional[float], float]] = []

        current_ratio = safe_divide(latest_value(current_assets), latest_value(current_liabilities))
        if current_ratio is not None:
            category.metrics["current ratio"] = round(current_ratio, 2)
            components.append((interpolate_score(
                current_ratio,
                [(0, 0), (0.75, 25), (1.0, 55), (1.25, 75), (1.5, 90), (2.0, 100), (3.0, 85)],
            ), 0.25))
            if current_ratio >= 1.25:
                category.strengths.append(f"Current ratio of {current_ratio:.2f} provides a reasonable liquidity buffer.")
            elif current_ratio < 1:
                category.risks.append(f"Current ratio of {current_ratio:.2f} is below 1.0.")
            else:
                category.caveats.append(f"Current ratio of {current_ratio:.2f} is positive but the buffer is modest.")
        else:
            components.append((None, 0.25))

        quick, quick_sector = ratios.get("QUICK RATIO", (None, None))
        if quick is not None:
            category.metrics["quick ratio"] = round(quick, 2)
            components.append((interpolate_score(
                quick,
                [(0, 0), (0.30, 10), (0.50, 25), (0.75, 50), (1.0, 75), (1.5, 95), (2.0, 100)],
            ), 0.20))
            if quick_sector is not None:
                category.metrics["quick ratio sector"] = round(quick_sector, 2)
                if quick > quick_sector:
                    category.strengths.append(f"Quick ratio of {quick:.2f} is above the sector's {quick_sector:.2f}.")
            if quick < 1:
                category.caveats.append("Quick ratio is below 1.0, so near-term liquidity depends partly on inventory/turnover.")
        else:
            components.append((None, 0.20))

        liability_to_equity = safe_divide(latest_value(liabilities), latest_value(equity))
        if liability_to_equity is not None:
            category.metrics["total liabilities/equity"] = round(liability_to_equity, 2)
            components.append((interpolate_score(
                liability_to_equity,
                [(0.25, 100), (0.50, 85), (1.0, 60), (1.5, 35), (2.0, 15), (3.0, 0)],
            ), 0.30))
            if liability_to_equity > 1.5:
                category.risks.append(f"Total liabilities/equity is elevated at {liability_to_equity:.2f}x.")
            elif liability_to_equity <= 1:
                category.strengths.append(f"Total liabilities/equity is contained at {liability_to_equity:.2f}x.")
        else:
            components.append((None, 0.30))

        working_capital = latest_value(net_current_assets)
        if working_capital is not None:
            category.metrics["net current assets"] = round(working_capital, 2)
            components.append((80.0 if working_capital > 0 else 10.0, 0.10))
            if working_capital > 0:
                category.strengths.append("Net current assets are positive.")
            else:
                category.risks.append("Net current assets are negative.")
        else:
            components.append((None, 0.10))

        asset_growth = latest_growth(assets)
        liability_growth = latest_growth(liabilities)
        if asset_growth is not None and liability_growth is not None:
            spread = asset_growth - liability_growth
            category.metrics["latest asset growth %"] = round(asset_growth, 2)
            category.metrics["latest liability growth %"] = round(liability_growth, 2)
            components.append((interpolate_score(
                spread,
                [(-15, 0), (-5, 30), (0, 55), (5, 80), (15, 100)],
            ), 0.15))
            if spread < 0:
                category.risks.append(
                    f"Liabilities grew {liability_growth:.1f}%, faster than assets at {asset_growth:.1f}% in the latest year."
                )
            else:
                category.strengths.append("Latest asset growth exceeded liability growth.")
        else:
            components.append((None, 0.15))

        category.score = weighted_average(components)
        category.caveats.append(
            "Total liabilities include operating obligations and must not be interpreted as interest-bearing debt."
        )
        category.caveats.append(
            "Net debt, interest coverage, debt maturity, and contingent liabilities are unavailable."
        )
        return category

    def analyze_cash_flow(self) -> CategoryScore:
        category = CategoryScore("Cash-flow quality", None, 15.0)
        operating = self.category_series("cash_flow", "cash_flow", "operating")
        investing = self.category_series("cash_flow", "cash_flow", "investing")
        net_profit = self.category_series("income_statement", "income_statement", "net_profit")
        total_cash = self.statement_series("cash_flow", "Total Cash Flow")
        ending_cash = self.statement_series("cash_flow", "Cash (End of the year)")
        components: list[tuple[Optional[float], float]] = []

        if operating:
            positive_share = sum(value > 0 for _, value in operating) / len(operating)
            category.metrics["years with positive operating cash flow"] = f"{sum(value > 0 for _, value in operating)}/{len(operating)}"
            components.append((positive_share * 100, 0.25))
            if positive_share == 1:
                category.strengths.append("Operating cash flow is positive in every available year.")
            else:
                category.risks.append("Operating cash flow is not consistently positive.")
        else:
            components.append((None, 0.25))

        conversion = safe_divide(latest_value(operating), latest_value(net_profit))
        if conversion is not None:
            category.metrics["latest operating cash flow/net profit"] = round(conversion, 2)
            components.append((interpolate_score(
                conversion,
                [(0, 0), (0.5, 30), (0.8, 60), (1.0, 85), (1.5, 100), (2.0, 95), (3.0, 80)],
            ), 0.30))
            if conversion >= 1:
                category.strengths.append(
                    f"Operating cash flow covers reported net profit {conversion:.2f}x in the latest year."
                )
            elif conversion < 0.8:
                category.risks.append(
                    f"Operating cash flow/net profit is only {conversion:.2f}x, weakening earnings quality."
                )
        else:
            components.append((None, 0.30))

        operating_cagr = series_cagr(operating)
        if operating_cagr is not None:
            category.metrics["operating cash flow CAGR %"] = round(operating_cagr, 2)
            components.append((growth_score(operating_cagr), 0.20))
            if operating_cagr >= 10:
                category.strengths.append(f"Operating cash flow CAGR is strong at {operating_cagr:.1f}%.")
        else:
            components.append((None, 0.20))

        if operating and investing:
            investing_by_period = dict(investing)
            post_investing = [
                (period, value + investing_by_period[period])
                for period, value in operating
                if period in investing_by_period
            ]
            if post_investing:
                positive_share = sum(value > 0 for _, value in post_investing) / len(post_investing)
                latest_surplus = latest_value(post_investing)
                category.metrics["latest CFO plus investing cash flow"] = round(latest_surplus, 2) if latest_surplus is not None else None
                category.metrics["years CFO plus investing cash flow positive"] = f"{sum(value > 0 for _, value in post_investing)}/{len(post_investing)}"
                components.append((positive_share * 100, 0.15))
                if positive_share == 1:
                    category.strengths.append("Operating cash covered net investing outflows in every available year.")
                else:
                    category.risks.append("Operating cash did not consistently cover net investing outflows.")
            else:
                components.append((None, 0.15))
        else:
            components.append((None, 0.15))

        if total_cash:
            positive_share = sum(value > 0 for _, value in total_cash) / len(total_cash)
            category.metrics["years with positive total cash flow"] = f"{sum(value > 0 for _, value in total_cash)}/{len(total_cash)}"
            components.append((positive_share * 100, 0.10))
        else:
            components.append((None, 0.10))
        if ending_cash:
            category.metrics["ending cash latest"] = round(ending_cash[-1][1], 2)
            cash_cagr = series_cagr(ending_cash)
            if cash_cagr is not None:
                category.metrics["ending cash CAGR %"] = round(cash_cagr, 2)

        category.score = weighted_average(components)
        category.caveats.append(
            "CFO plus investing cash flow is only a coverage proxy; capex is not separately supplied, so true free cash flow cannot be calculated."
        )
        return category

    def analyze_shareholder_returns(self) -> CategoryScore:
        category = CategoryScore("Shareholder returns & ownership", None, 5.0)
        components: list[tuple[Optional[float], float]] = []
        actions = self.data("corporate_actions")
        dividends: list[tuple[datetime, float]] = []
        if isinstance(actions, list):
            for action in actions:
                if not isinstance(action, dict) or str(action.get("name", "")).lower() != "dividend":
                    continue
                amount = parse_number(action.get("amount"))
                date_text = action.get("expiry_date")
                if amount is None or not date_text:
                    continue
                try:
                    date = datetime.strptime(str(date_text), "%d %b %Y")
                except ValueError:
                    continue
                dividends.append((date, amount))
        dividends.sort(key=lambda item: item[0])

        if dividends:
            latest_date, latest_amount = dividends[-1]
            category.metrics["latest dividend per share"] = round(latest_amount, 2)
            category.metrics["latest dividend date"] = latest_date.strftime("%d %b %Y")
            dividend_scores: list[tuple[Optional[float], float]] = [
                (75.0 if latest_amount > 0 else 0.0, 0.40)
            ]
            if len(dividends) >= 2 and dividends[-2][1] != 0:
                dividend_growth = (latest_amount / dividends[-2][1] - 1) * 100
                category.metrics["latest dividend growth %"] = round(dividend_growth, 2)
                dividend_scores.append((growth_score(dividend_growth), 0.60))
                if dividend_growth > 0:
                    category.strengths.append(f"Latest reported dividend increased {dividend_growth:.1f}%.")
                elif dividend_growth < 0:
                    category.risks.append(f"Latest reported dividend decreased {abs(dividend_growth):.1f}%.")
            else:
                category.caveats.append("Only one usable dividend event is available; dividend growth is unscored.")
                dividend_scores.append((None, 0.60))
            components.append((weighted_average(dividend_scores), 0.45))
        else:
            category.caveats.append("No usable dividend actions are available.")
            components.append((None, 0.45))

        holdings = self.data("share_holdings")
        holding_series: dict[str, list[tuple[str, float]]] = {}
        if isinstance(holdings, list):
            for entry in holdings:
                if not isinstance(entry, dict):
                    continue
                holding_series[normalize_name(str(entry.get("category", "")))] = history_series(
                    entry.get("history")
                )

        promoter = holding_series.get("promoters", [])
        if len(promoter) >= 2:
            promoter_change = promoter[-1][1] - promoter[0][1]
            category.metrics["promoter holding latest %"] = round(promoter[-1][1], 2)
            category.metrics["promoter holding change pp"] = round(promoter_change, 2)
            promoter_score = interpolate_score(
                promoter_change,
                [(-5, 0), (-2, 25), (-0.5, 55), (0, 70), (1, 85), (3, 100)],
            )
            components.append((promoter_score, 0.35))
            if promoter_change >= 0.5:
                category.strengths.append(
                    f"Promoter ownership increased {promoter_change:.2f} percentage points across the available quarters."
                )
            elif promoter_change <= -1:
                category.risks.append(
                    f"Promoter ownership fell {abs(promoter_change):.2f} percentage points across the available quarters."
                )
            else:
                category.strengths.append("Promoter ownership is broadly stable across the available quarters.")
        else:
            components.append((None, 0.35))

        institution_categories = ("fii", "otherdii", "mutualfunds")
        institution_by_period: dict[str, float] = {}
        for name in institution_categories:
            for period, value in holding_series.get(name, []):
                institution_by_period[period] = institution_by_period.get(period, 0.0) + value
        institution_series = sorted(institution_by_period.items(), key=lambda item: period_sort_key(item[0]))
        if len(institution_series) >= 2:
            institution_change = institution_series[-1][1] - institution_series[0][1]
            category.metrics["institutional holding latest %"] = round(institution_series[-1][1], 2)
            category.metrics["institutional holding change pp"] = round(institution_change, 2)
            institution_score = interpolate_score(
                institution_change,
                [(-5, 0), (-2, 25), (-0.5, 55), (0, 70), (1, 85), (3, 100)],
            )
            components.append((institution_score, 0.20))
            if institution_change >= 0.5:
                category.strengths.append(
                    f"Combined reported institutional ownership increased {institution_change:.2f} percentage points."
                )
            elif institution_change <= -1:
                category.risks.append(
                    f"Combined reported institutional ownership fell {abs(institution_change):.2f} percentage points."
                )
        else:
            components.append((None, 0.20))

        if self.endpoint_status.get("share_holdings") != "success":
            category.caveats.append(
                "Shareholding data is unavailable; promoter ownership, pledging, and institutional trends are unassessed."
            )
        else:
            category.caveats.append(
                "Upstox provides ownership percentages but not promoter pledging, so pledge risk remains unassessed."
            )
            category.caveats.append(
                "Ownership levels differ naturally for founder-led and professionally managed companies; the score emphasizes changes, not an arbitrary minimum holding."
            )
        category.score = weighted_average(components)
        category.caveats.append("The corporate-action feed may not represent the complete distribution history.")
        return category

    def company_metadata(self) -> tuple[str, str, Optional[str], Optional[str]]:
        profile = self.data("profile")
        name = "Unknown company"
        sector = "Unknown"
        description = None
        if isinstance(profile, dict):
            sector = str(profile.get("sector") or sector)
            description_value = profile.get("company_profile")
            if description_value:
                description = str(description_value)
                first_clause = re.split(r"\s+is\s+", description, maxsplit=1, flags=re.IGNORECASE)[0]
                if 2 <= len(first_clause.split()) <= 12:
                    name = first_clause.strip(" .")

        periods = []
        for endpoint, section, category in [
            ("income_statement", "income_statement", "revenue"),
            ("cash_flow", "cash_flow", "operating"),
        ]:
            period = latest_period(self.category_series(endpoint, section, category))
            if period:
                periods.append(period)
        balance = self.data("balance_sheet")
        if isinstance(balance, dict) and isinstance(balance.get("history"), list):
            period_values = [str(row.get("period")) for row in balance["history"] if isinstance(row, dict) and row.get("period")]
            if period_values:
                periods.append(max(period_values, key=period_sort_key))
        latest = max(periods, key=period_sort_key) if periods else None
        return name, sector, description, latest

    def confidence(self) -> tuple[float, str]:
        score = sum(
            ENDPOINT_CONFIDENCE_WEIGHTS[endpoint]
            for endpoint, status in self.endpoint_status.items()
            if status == "success"
        ) * 100

        # Core statements should describe the same latest reporting period.
        periods = []
        revenue_period = latest_period(
            self.category_series("income_statement", "income_statement", "revenue")
        )
        operating_period = latest_period(self.category_series("cash_flow", "cash_flow", "operating"))
        if revenue_period:
            periods.append(revenue_period)
        if operating_period:
            periods.append(operating_period)
        balance = self.data("balance_sheet")
        if isinstance(balance, dict) and isinstance(balance.get("history"), list):
            balance_periods = [str(row.get("period")) for row in balance["history"] if isinstance(row, dict) and row.get("period")]
            if balance_periods:
                periods.append(max(balance_periods, key=period_sort_key))
        if len(set(periods)) > 1:
            score -= 10
            self.issues.append(DataIssue("periods", "Core statements have different latest reporting periods"))

        score = clamp(score)
        label = "HIGH" if score >= 90 else "MODERATE" if score >= 70 else "LOW"
        return score, label

    def analyze(self) -> dict[str, Any]:
        self._load()
        unavailable = [
            endpoint
            for endpoint in MANDATORY_ENDPOINTS
            if self.endpoint_status.get(endpoint) != "success"
        ]
        if unavailable:
            raise ValueError(
                "Cannot perform fundamental analysis; mandatory data is "
                f"unavailable: {', '.join(unavailable)}"
            )

        ratios = self.ratio_map()
        categories = [
            self.analyze_valuation(ratios),
            self.analyze_profitability(ratios),
            self.analyze_growth(),
            self.analyze_financial_health(ratios),
            self.analyze_cash_flow(),
            self.analyze_shareholder_returns(),
        ]
        overall = weighted_average((category.score, category.weight) for category in categories)
        if overall is None:
            raise ValueError("There is not enough valid fundamental data to produce a score")

        if overall >= 80:
            rating = "STRONG"
        elif overall >= 70:
            rating = "GOOD"
        elif overall >= 55:
            rating = "MIXED"
        elif overall >= 40:
            rating = "WEAK"
        else:
            rating = "POOR"
        decision = "GOOD" if overall >= self.good_threshold else "NOT GOOD ENOUGH"

        name, sector, description, period = self.company_metadata()
        if description and len(re.findall(r"\bsegment\b", description, flags=re.IGNORECASE)) >= 2:
            diversification_note = (
                f"The company is diversified, while Upstox supplies one '{sector}' sector benchmark; "
                "peer-relative scores may therefore be imperfect."
            )
        else:
            diversification_note = None
        competitor_data = self.data("competitors")
        competitor_note = None
        if isinstance(competitor_data, list) and competitor_data:
            competitor_note = (
                f"Upstox returned {len(competitor_data)} competitor profile(s), but the competitor payload has no "
                "company-level financial ratios; it cannot support a full peer ranking."
            )
        confidence_score, confidence_label = self.confidence()

        active_weight = sum(category.weight for category in categories if category.score is not None)
        return {
            "company": name,
            "sector": sector,
            "latest_financial_period": period,
            "units": self._units(),
            "decision": decision,
            "rating": rating,
            "score": round(overall, 2),
            "good_threshold": round(self.good_threshold, 2),
            "confidence": {"score": round(confidence_score, 2), "label": confidence_label},
            "scored_weight_available": round(active_weight, 2),
            "categories": [asdict(category) | {"score": round(category.score, 2) if category.score is not None else None} for category in categories],
            "endpoint_status": self.endpoint_status,
            "data_issues": [asdict(issue) for issue in self.issues],
            "overall_caveats": [
                note
                for note in [
                    diversification_note,
                    competitor_note,
                    "This result is a fundamental screen, not a buy/sell instruction; price trend, management quality, governance, and future estimates are outside the supplied data.",
                ]
                if note
            ],
        }

    def _units(self) -> Optional[str]:
        for endpoint in ("income_statement", "balance_sheet", "cash_flow"):
            data = self.data(endpoint)
            if isinstance(data, dict) and data.get("units_in"):
                return str(data["units_in"])
        return None


def render_text(result: dict[str, Any]) -> str:
    title = f"FUNDAMENTAL ANALYSIS — {result['company']}"
    lines = [
        title,
        "=" * len(title),
        f"Sector: {result['sector']}",
        f"Latest financial period: {result['latest_financial_period'] or 'Unknown'}",
        f"Statement units: {result['units'] or 'Unknown'}",
        "",
        f"VERDICT: {result['decision']} ({result['rating']})",
        f"Fundamental score: {result['score']:.1f}/100 "
        f"(GOOD requires {result['good_threshold']:.1f}+)",
        f"Data confidence: {result['confidence']['score']:.1f}/100 "
        f"({result['confidence']['label']})",
        "",
        "CATEGORY SCORES",
        "---------------",
    ]

    for category in result["categories"]:
        score = "N/A" if category["score"] is None else f"{category['score']:.1f}/100"
        lines.append(f"{category['name']:<22} {score:>9}   weight {category['weight']:.0f}%")

    lines.extend(["", "DETAILED ANALYSIS", "-----------------"])
    for category in result["categories"]:
        score = "N/A" if category["score"] is None else f"{category['score']:.1f}/100"
        lines.extend(["", f"{category['name']} — {score}"])
        if category["metrics"]:
            metric_text = "; ".join(
                f"{name}: {fmt_number(value)}" for name, value in category["metrics"].items()
            )
            lines.append(f"  Metrics: {metric_text}")
        for strength in category["strengths"]:
            lines.append(f"  + {strength}")
        for risk in category["risks"]:
            lines.append(f"  - {risk}")
        for caveat in category["caveats"]:
            lines.append(f"  ! {caveat}")

    lines.extend(["", "DATA QUALITY", "------------"])
    for endpoint, status in result["endpoint_status"].items():
        lines.append(f"  {endpoint:<20} {status}")
    if result["data_issues"]:
        lines.append("  Issues:")
        for issue in result["data_issues"]:
            code = f" [{issue['code']}]" if issue.get("code") else ""
            lines.append(f"  ! {issue['endpoint']}{code}: {issue['message']}")

    lines.extend(["", "LIMITATIONS", "-----------"])
    for caveat in result["overall_caveats"]:
        lines.append(f"  ! {caveat}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze Upstox fundamental JSON files with an explainable score."
    )
    parser.add_argument(
        "folder",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Directory containing the Upstox JSON exports (default: this script's directory)",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        dest="output_format",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--good-threshold",
        type=float,
        default=70.0,
        help="Minimum 0-100 score for the binary GOOD decision (default: 70)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional file to receive the report; stdout is still used when omitted",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 <= args.good_threshold <= 100:
        print("error: --good-threshold must be between 0 and 100", file=sys.stderr)
        return 2
    try:
        result = FundamentalAnalyzer(args.folder, args.good_threshold).analyze()
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.output_format == "json":
        rendered = json.dumps(result, indent=2, ensure_ascii=False)
    else:
        rendered = render_text(result)

    if args.output:
        try:
            args.output.write_text(rendered + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"error: could not write {args.output}: {exc}", file=sys.stderr)
            return 2
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
