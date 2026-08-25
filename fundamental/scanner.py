"""NSE-wide explainable fundamental screening for Slack CSV exports."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from math import isfinite
import re
from threading import Lock
from time import monotonic, sleep
from typing import Any, Protocol

from fundamental.analyzer import FundamentalAnalyzer, MANDATORY_ENDPOINTS
from upstox.assets import AssetCatalogError, AssetSearchResult
from upstox.client import UpstoxAPIError
from upstox.store import TokenState, TokenStateError

LOGGER = logging.getLogger(__name__)

DEFAULT_GOOD_THRESHOLD = 50.0
DEFAULT_REQUEST_INTERVAL_SECONDS = 0.125
DEFAULT_PROGRESS_INTERVAL = 100
ISIN_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{10}$")

# Analyzer key, Upstox endpoint, and documented query parameters.
FUNDAMENTAL_REQUESTS: tuple[
    tuple[str, str, Mapping[str, str] | None], ...
] = (
    ("profile", "profile", None),
    ("key_ratios", "key-ratios", None),
    (
        "balance_sheet",
        "balance-sheet",
        {"type": "consolidated", "fs": "true"},
    ),
    (
        "income_statement",
        "income-statement",
        {
            "type": "consolidated",
            "time_period": "yearly",
            "fs": "true",
        },
    ),
    (
        "cash_flow",
        "cash-flow",
        {"type": "consolidated", "fs": "true"},
    ),
    ("corporate_actions", "corporate-actions", None),
    ("share_holdings", "share-holdings", None),
    ("competitors", "competitors", None),
)


class FundamentalScanError(RuntimeError):
    """Raised when the catalogue-wide fundamental scan cannot complete."""


@dataclass(frozen=True, slots=True)
class FundamentalStock:
    """One NSE equity accepted by the supplied fundamental analyzer."""

    asset_name: str
    trading_symbol: str
    isin: str
    score: float
    rating: str
    confidence: float
    sector: str
    latest_financial_period: str
    has_fno: bool
    instrument_key: str = ""


@dataclass(frozen=True, slots=True)
class FundamentalScanResult:
    """Summary and export rows produced by an NSE fundamental scan."""

    catalog_instruments: int
    equity_assets: int
    evaluated: int
    failed: int
    skipped: int
    endpoint_failures: int
    stocks: tuple[FundamentalStock, ...]
    good_threshold: float = DEFAULT_GOOD_THRESHOLD


class EquityCatalog(Protocol):
    def refresh(self) -> int:
        """Refresh the NSE instrument catalogue and return its row count."""

    def list_equities(self) -> list[AssetSearchResult]:
        """Return normal NSE equities from the refreshed catalogue."""

    def fno_isins(self) -> frozenset[str]:
        """Return ISINs of NSE equities with at least one F&O contract."""


class AssetLookup(Protocol):
    def search(
        self, query: str, limit: int | None = None
    ) -> list[AssetSearchResult]:
        """Find NSE instruments by trading symbol, name, key, or ISIN."""


class FundamentalClient(Protocol):
    def get_fundamental_data(
        self,
        access_token: str,
        isin: str,
        endpoint: str,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return one company-fundamentals endpoint payload."""


class AccessTokenStore(Protocol):
    def load(self) -> TokenState:
        """Load the current Upstox access token."""


AnalyzePayloads = Callable[[Mapping[str, Any], float], dict[str, Any]]


class NSEFundamentalScanner:
    """Refresh NSE.json and score each distinct normal equity by ISIN."""

    def __init__(
        self,
        catalog: EquityCatalog,
        client: FundamentalClient,
        token_store: AccessTokenStore,
        *,
        good_threshold: float = DEFAULT_GOOD_THRESHOLD,
        request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
        progress_interval: int = DEFAULT_PROGRESS_INTERVAL,
        sleep_function: Callable[[float], None] = sleep,
        analyze_payloads: AnalyzePayloads | None = None,
    ) -> None:
        if not isfinite(good_threshold) or not 0 <= good_threshold <= 100:
            raise ValueError("good_threshold must be between 0 and 100")
        if (
            not isfinite(request_interval_seconds)
            or request_interval_seconds < 0
        ):
            raise ValueError(
                "request_interval_seconds must be finite and non-negative"
            )
        if progress_interval <= 0:
            raise ValueError("progress_interval must be positive")

        self.catalog = catalog
        self.client = client
        self.token_store = token_store
        self.good_threshold = good_threshold
        self.request_interval_seconds = request_interval_seconds
        self.progress_interval = progress_interval
        self._sleep = sleep_function
        self._analyze_payloads = analyze_payloads or analyze_fundamental_payloads
        self._scan_lock = Lock()

    def scan(self, *, now: datetime | None = None) -> FundamentalScanResult:
        """Run one complete, non-overlapping NSE fundamental scan."""
        if not self._scan_lock.acquire(blocking=False):
            raise FundamentalScanError(
                "An NSE fundamental scan is already running."
            )

        started_at = monotonic()
        LOGGER.info(
            "Starting NSE equity fundamental scan good_threshold=%.2f "
            "request_interval_seconds=%.3f",
            self.good_threshold,
            self.request_interval_seconds,
        )
        try:
            catalog_instruments, equities, fno_isins = self._refresh_equities()
            access_token = _valid_access_token(
                self.token_store, now or datetime.now(UTC)
            )
            result = self._evaluate(
                catalog_instruments,
                equities,
                fno_isins,
                access_token,
                started_at,
            )
            LOGGER.info(
                "Completed NSE equity fundamental scan "
                "catalog_instruments=%d equity_assets=%d evaluated=%d "
                "good=%d skipped=%d failed=%d endpoint_failures=%d "
                "elapsed_seconds=%.2f",
                result.catalog_instruments,
                result.equity_assets,
                result.evaluated,
                len(result.stocks),
                result.skipped,
                result.failed,
                result.endpoint_failures,
                monotonic() - started_at,
            )
            return result
        except FundamentalScanError:
            LOGGER.exception(
                "NSE equity fundamental scan did not complete "
                "elapsed_seconds=%.2f",
                monotonic() - started_at,
            )
            raise
        except Exception as error:
            LOGGER.exception(
                "Unexpected NSE equity fundamental scan failure "
                "elapsed_seconds=%.2f",
                monotonic() - started_at,
            )
            raise FundamentalScanError(
                "Unable to complete the NSE equity fundamental scan."
            ) from error
        finally:
            self._scan_lock.release()

    def _refresh_equities(
        self,
    ) -> tuple[int, list[AssetSearchResult], frozenset[str]]:
        try:
            catalog_instruments = self.catalog.refresh()
            equities = self.catalog.list_equities()
            fno_isins = self.catalog.fno_isins()
        except AssetCatalogError as error:
            raise FundamentalScanError(str(error)) from error
        if not equities:
            raise FundamentalScanError(
                "The refreshed catalogue contains no NSE equity instruments."
            )
        return catalog_instruments, equities, fno_isins

    def _evaluate(
        self,
        catalog_instruments: int,
        equities: list[AssetSearchResult],
        fno_isins: frozenset[str],
        access_token: str,
        started_at: float,
    ) -> FundamentalScanResult:
        stocks: list[FundamentalStock] = []
        evaluated = 0
        failed = 0
        skipped = 0
        endpoint_failures = 0
        seen_isins: set[str] = set()
        request_count = 0

        for index, asset in enumerate(equities, start=1):
            isin = _asset_isin(asset)
            if isin is None or isin in seen_isins:
                skipped += 1
                LOGGER.warning(
                    "Skipping NSE equity fundamental analysis index=%d/%d "
                    "trading_symbol=%r instrument_key=%r isin=%r",
                    index,
                    len(equities),
                    asset.trading_symbol,
                    asset.instrument_key,
                    asset.isin,
                )
                continue
            seen_isins.add(isin)

            LOGGER.info(
                "Evaluating NSE equity fundamentals index=%d/%d "
                "trading_symbol=%r asset_name=%r isin=%r",
                index,
                len(equities),
                asset.trading_symbol,
                asset.name,
                isin,
            )

            payloads: dict[str, Any] = {}
            for analyzer_key, endpoint, params in FUNDAMENTAL_REQUESTS:
                if request_count:
                    self._sleep(self.request_interval_seconds)
                request_count += 1
                try:
                    payloads[analyzer_key] = self.client.get_fundamental_data(
                        access_token,
                        isin,
                        endpoint,
                        params,
                    )
                except UpstoxAPIError as error:
                    if error.status_code in {401, 403}:
                        raise FundamentalScanError(
                            "Upstox rejected the access token during the "
                            "fundamental scan. Set a valid token with "
                            "`/swingengine auth set <token>`."
                        ) from error
                    endpoint_failures += 1
                    LOGGER.warning(
                        "Fundamentals endpoint failed index=%d/%d "
                        "trading_symbol=%r isin=%r endpoint=%r error=%s",
                        index,
                        len(equities),
                        asset.trading_symbol,
                        isin,
                        endpoint,
                        error,
                    )
                    payloads[analyzer_key] = {
                        "status": "error",
                        "errors": [{"message": str(error)}],
                    }

            unavailable = _unavailable_mandatory_payloads(payloads)
            if unavailable:
                skipped += 1
                LOGGER.warning(
                    "Skipping NSE equity fundamental analysis because "
                    "mandatory data is unavailable index=%d/%d "
                    "trading_symbol=%r isin=%r endpoints=%s",
                    index,
                    len(equities),
                    asset.trading_symbol,
                    isin,
                    ",".join(unavailable),
                )
                continue

            try:
                analysis = self._analyze_payloads(
                    payloads,
                    self.good_threshold,
                )
                stock = _accepted_stock(
                    asset, isin, analysis, isin in fno_isins
                )
            except Exception:
                failed += 1
                LOGGER.warning(
                    "Unable to analyze NSE equity fundamentals index=%d/%d "
                    "trading_symbol=%r isin=%r",
                    index,
                    len(equities),
                    asset.trading_symbol,
                    isin,
                    exc_info=True,
                )
            else:
                evaluated += 1
                if stock is not None:
                    stocks.append(stock)

            if index % self.progress_interval == 0 or index == len(equities):
                LOGGER.info(
                    "NSE equity fundamental scan progress processed=%d/%d "
                    "evaluated=%d good=%d skipped=%d failed=%d "
                    "endpoint_failures=%d elapsed_seconds=%.2f",
                    index,
                    len(equities),
                    evaluated,
                    len(stocks),
                    skipped,
                    failed,
                    endpoint_failures,
                    monotonic() - started_at,
                )

        stocks.sort(
            key=lambda stock: (
                -stock.score,
                stock.trading_symbol.casefold(),
            )
        )
        return FundamentalScanResult(
            catalog_instruments=catalog_instruments,
            equity_assets=len(equities),
            evaluated=evaluated,
            failed=failed,
            skipped=skipped,
            endpoint_failures=endpoint_failures,
            stocks=tuple(stocks),
            good_threshold=self.good_threshold,
        )


@dataclass(frozen=True, slots=True)
class SymbolFundamentalAnalysis:
    """The full explainable analysis for one NSE equity."""

    trading_symbol: str
    asset_name: str
    isin: str
    analysis: dict[str, Any]
    instrument_key: str = ""


class SymbolFundamentalAnalyzer:
    """Score one NSE equity's fundamentals by trading symbol on demand."""

    def __init__(
        self,
        asset_lookup: AssetLookup,
        client: FundamentalClient,
        token_store: AccessTokenStore,
        *,
        good_threshold: float = DEFAULT_GOOD_THRESHOLD,
        request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
        sleep_function: Callable[[float], None] = sleep,
        analyze_payloads: AnalyzePayloads | None = None,
    ) -> None:
        if not isfinite(good_threshold) or not 0 <= good_threshold <= 100:
            raise ValueError("good_threshold must be between 0 and 100")
        if (
            not isfinite(request_interval_seconds)
            or request_interval_seconds < 0
        ):
            raise ValueError(
                "request_interval_seconds must be finite and non-negative"
            )

        self.asset_lookup = asset_lookup
        self.client = client
        self.token_store = token_store
        self.good_threshold = good_threshold
        self.request_interval_seconds = request_interval_seconds
        self._sleep = sleep_function
        self._analyze_payloads = analyze_payloads or analyze_fundamental_payloads

    def analyze(
        self, trading_symbol: str, *, now: datetime | None = None
    ) -> SymbolFundamentalAnalysis:
        """Fetch and score one NSE equity's fundamentals.

        Raises ``FundamentalScanError`` for anything that keeps a usable
        result from being produced (unknown symbol, invalid/expired token,
        or insufficient mandatory fundamentals data).
        """
        asset = self._find_asset(trading_symbol)
        isin = _asset_isin(asset)
        if isin is None:
            raise FundamentalScanError(
                f"`{asset.trading_symbol}` has no usable ISIN for "
                "fundamental analysis."
            )
        access_token = _valid_access_token(
            self.token_store, now or datetime.now(UTC)
        )

        payloads: dict[str, Any] = {}
        for index, (analyzer_key, endpoint, params) in enumerate(
            FUNDAMENTAL_REQUESTS
        ):
            if index:
                self._sleep(self.request_interval_seconds)
            try:
                payloads[analyzer_key] = self.client.get_fundamental_data(
                    access_token,
                    isin,
                    endpoint,
                    params,
                )
            except UpstoxAPIError as error:
                if error.status_code in {401, 403}:
                    raise FundamentalScanError(
                        "Upstox rejected the access token during "
                        "fundamental analysis. Set a valid token with "
                        "`/swingengine auth set <token>`."
                    ) from error
                LOGGER.warning(
                    "Fundamentals endpoint failed trading_symbol=%r "
                    "isin=%r endpoint=%r error=%s",
                    asset.trading_symbol,
                    isin,
                    endpoint,
                    error,
                )
                payloads[analyzer_key] = {
                    "status": "error",
                    "errors": [{"message": str(error)}],
                }

        try:
            analysis = self._analyze_payloads(payloads, self.good_threshold)
        except ValueError as error:
            raise FundamentalScanError(str(error)) from error

        return SymbolFundamentalAnalysis(
            trading_symbol=asset.trading_symbol,
            asset_name=asset.name,
            isin=isin,
            analysis=analysis,
            instrument_key=asset.instrument_key,
        )

    def _find_asset(self, trading_symbol: str) -> AssetSearchResult:
        normalized = trading_symbol.strip().upper()
        if not normalized:
            raise ValueError("trading_symbol cannot be empty")
        try:
            matches = self.asset_lookup.search(normalized)
        except AssetCatalogError as error:
            raise FundamentalScanError(str(error)) from error
        asset = next(
            (
                match
                for match in matches
                if match.trading_symbol.casefold() == normalized.casefold()
                and match.segment.casefold() == "nse_eq"
                and match.instrument_type.casefold() == "eq"
            ),
            None,
        )
        if asset is None:
            raise FundamentalScanError(
                f"No NSE equity found for trading symbol `{normalized}`."
            )
        return asset


def _valid_access_token(token_store: AccessTokenStore, current: datetime) -> str:
    try:
        token_state = token_store.load()
    except TokenStateError as error:
        raise FundamentalScanError(
            "Upstox token state cannot be read."
        ) from error
    if not token_state.is_valid(current):
        raise FundamentalScanError(
            "A valid Upstox token is required. Use "
            "`/swingengine auth set <token>`."
        )
    return token_state.access_token


def analyze_fundamental_payloads(
    payloads: Mapping[str, Any],
    good_threshold: float,
) -> dict[str, Any]:
    """Analyze API payloads without relying on a system temp directory."""
    return FundamentalAnalyzer.from_payloads(
        payloads,
        good_threshold,
    ).analyze()


def _unavailable_mandatory_payloads(
    payloads: Mapping[str, Any],
) -> list[str]:
    return [
        endpoint
        for endpoint in MANDATORY_ENDPOINTS
        if not _payload_data_available(payloads.get(endpoint))
    ]


def _payload_data_available(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    if str(payload.get("status", "")).casefold() != "success":
        return False
    data = payload.get("data")
    if isinstance(data, (Mapping, list, tuple, set, str, bytes)):
        return bool(data)
    return data is not None


def _asset_isin(asset: AssetSearchResult) -> str | None:
    candidates = (asset.isin, asset.instrument_key.partition("|")[2])
    for candidate in candidates:
        normalized = candidate.strip().upper()
        if ISIN_PATTERN.fullmatch(normalized):
            return normalized
    return None


def _accepted_stock(
    asset: AssetSearchResult,
    isin: str,
    analysis: Mapping[str, Any],
    has_fno: bool,
) -> FundamentalStock | None:
    score = float(analysis["score"])
    confidence = float(analysis["confidence"]["score"])
    if not isfinite(score) or not isfinite(confidence):
        raise ValueError("analyzer returned a non-finite score")
    if analysis.get("decision") != "GOOD":
        return None
    return FundamentalStock(
        asset_name=asset.name,
        trading_symbol=asset.trading_symbol,
        isin=isin,
        score=score,
        rating=str(analysis["rating"]),
        confidence=confidence,
        sector=str(analysis.get("sector") or "Unknown"),
        latest_financial_period=str(
            analysis.get("latest_financial_period") or ""
        ),
        has_fno=has_fno,
        instrument_key=asset.instrument_key,
    )
