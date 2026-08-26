"""On-demand momentum analysis for one symbol, saved assets, or the tracker."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import logging
from typing import Protocol
from zoneinfo import ZoneInfo

from database.repository import AssetRecord, MomentumCandidate, RepositoryError
from tracker.config import TrackerEvaluationSettings
from tracker.evaluator import (
    DualTimeframeMomentum,
    IndicatorCalculationError,
    calculate_daily_close_momentum,
    calculate_momentum_indicators,
)
from upstox.assets import AssetCatalogError, AssetSearchResult
from upstox.client import DailyCandle, UpstoxAPIError
from upstox.store import TokenState, TokenStateError

LOGGER = logging.getLogger(__name__)


class MomentumAnalysisError(RuntimeError):
    """Raised when an on-demand momentum analysis request cannot be completed."""


class MomentumAnalysisRepository(Protocol):
    def find_asset_by_trading_symbol(
        self, trading_symbol: str
    ) -> AssetRecord | None:
        """Return one saved asset by trading symbol, or None if unsaved."""

    def list_assets(self) -> list[AssetRecord]:
        """Return every saved asset."""

    def list_tracker_assets(self) -> list[MomentumCandidate]:
        """Return every currently tracked asset with its instrument key."""

    def record_momentum_evaluation(
        self,
        asset_id: int,
        has_momentum: bool,
        evaluation_date: date,
        side: str | None = None,
    ) -> bool:
        """Insert or update eligible tracker state and report persistence."""


class DailyCandleClient(Protocol):
    def get_daily_candles(
        self,
        access_token: str,
        instrument_key: str,
        from_date: date,
        through_date: date,
    ) -> list[DailyCandle]:
        """Return historical candles plus the current trading day."""

    def get_hourly_candles(
        self,
        access_token: str,
        instrument_key: str,
        from_date: date,
        through_date: date,
    ) -> list[DailyCandle]:
        """Return hourly candles plus the current, still-forming hour."""


class AccessTokenStore(Protocol):
    def load(self) -> TokenState:
        """Load the current token state."""


class AssetLookup(Protocol):
    def search(self, query: str) -> list[AssetSearchResult]:
        """Find NSE instruments related to a trading symbol."""


@dataclass(frozen=True, slots=True)
class SymbolMomentumAnalysis:
    """The momentum screen outcome for one asset.

    ``asset_id`` is None when the symbol was resolved from the NSE catalog
    rather than the saved asset table (it is never tracked in that case).
    """

    asset_id: int | None
    asset_name: str
    trading_symbol: str
    has_momentum: bool
    side: str | None
    tracker_updated: bool = False


@dataclass(frozen=True, slots=True)
class MomentumAnalysisBatch:
    """Aggregate outcome from screening multiple saved assets."""

    results: tuple[SymbolMomentumAnalysis, ...]
    failed: int = 0


class MomentumAnalyzer:
    """Screen saved assets and tracker entries for momentum on demand."""

    def __init__(
        self,
        settings: TrackerEvaluationSettings,
        repository: MomentumAnalysisRepository,
        candle_client: DailyCandleClient,
        token_store: AccessTokenStore,
        asset_lookup: AssetLookup | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.candle_client = candle_client
        self.token_store = token_store
        self.asset_lookup = asset_lookup

    def analyze_symbol(
        self,
        trading_symbol: str,
        *,
        update_tracker: bool = False,
        now: datetime | None = None,
    ) -> SymbolMomentumAnalysis:
        """Analyze one trading symbol for momentum.

        Saved assets are analyzed directly. A symbol that is not saved is
        instead resolved from the NSE instrument catalog (backed by
        ``NSE.json``) and can only be analyzed, never used to update the
        tracker, since it has no asset row to update.
        """
        normalized = trading_symbol.strip().upper()
        if not normalized:
            raise MomentumAnalysisError("Provide a trading symbol.")
        try:
            asset = self.repository.find_asset_by_trading_symbol(normalized)
        except RepositoryError as error:
            raise MomentumAnalysisError(str(error)) from error

        if asset is None:
            if update_tracker:
                raise MomentumAnalysisError(
                    f"Asset `{normalized}` is not saved. Add it first with "
                    "`/swingengine asset add <trading_symbol>`."
                )
            return self._analyze_unsaved_symbol(normalized, now)

        if not asset.instrument_key:
            raise MomentumAnalysisError(
                f"Asset `{normalized}` has no instrument key."
            )

        access_token, local_date = self._valid_token_and_date(now)
        result = self._analyze_one(
            asset.asset_id,
            asset.asset_name,
            asset.trading_symbol,
            asset.instrument_key,
            access_token,
            local_date,
            update_tracker=update_tracker,
        )
        if result is None:
            raise MomentumAnalysisError(
                f"Unable to analyze `{normalized}`: not enough daily candles."
            )
        return result

    def _analyze_unsaved_symbol(
        self, normalized: str, now: datetime | None
    ) -> SymbolMomentumAnalysis:
        if self.asset_lookup is None:
            raise MomentumAnalysisError(
                f"Asset `{normalized}` is not saved. Add it first with "
                "`/swingengine asset add <trading_symbol>`."
            )
        try:
            matches = self.asset_lookup.search(normalized)
        except AssetCatalogError as error:
            raise MomentumAnalysisError(str(error)) from error
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
        if asset is None or not asset.instrument_key:
            raise MomentumAnalysisError(
                f"No exact NSE trading symbol found for `{normalized}`."
            )

        access_token, local_date = self._valid_token_and_date(now)
        result = self._analyze_one(
            None,
            asset.name,
            asset.trading_symbol,
            asset.instrument_key,
            access_token,
            local_date,
            update_tracker=False,
        )
        if result is None:
            raise MomentumAnalysisError(
                f"Unable to analyze `{normalized}`: not enough daily candles."
            )
        return result

    def analyze_assets(
        self,
        *,
        update_tracker: bool = False,
        now: datetime | None = None,
    ) -> MomentumAnalysisBatch:
        """Analyze every saved asset for momentum."""
        try:
            assets = self.repository.list_assets()
        except RepositoryError as error:
            raise MomentumAnalysisError(str(error)) from error
        access_token, local_date = self._valid_token_and_date(now)
        return self._analyze_many(
            (
                (
                    asset.asset_id,
                    asset.asset_name,
                    asset.trading_symbol,
                    asset.instrument_key,
                )
                for asset in assets
            ),
            access_token,
            local_date,
            update_tracker=update_tracker,
        )

    def analyze_tracker(
        self, *, now: datetime | None = None
    ) -> MomentumAnalysisBatch:
        """Re-check every tracked asset and clear momentum/side that lapsed."""
        try:
            candidates = self.repository.list_tracker_assets()
        except RepositoryError as error:
            raise MomentumAnalysisError(str(error)) from error
        access_token, local_date = self._valid_token_and_date(now)
        return self._analyze_many(
            (
                (
                    candidate.asset_id,
                    candidate.asset_name,
                    candidate.trading_symbol,
                    candidate.instrument_key,
                )
                for candidate in candidates
            ),
            access_token,
            local_date,
            update_tracker=True,
        )

    def _analyze_many(
        self,
        assets: Iterable[tuple[int, str, str, str | None]],
        access_token: str,
        local_date: date,
        *,
        update_tracker: bool,
    ) -> MomentumAnalysisBatch:
        results: list[SymbolMomentumAnalysis] = []
        failed = 0
        for asset_id, asset_name, trading_symbol, instrument_key in assets:
            if not instrument_key:
                failed += 1
                LOGGER.warning(
                    "Skipping momentum analysis without instrument key "
                    "trading_symbol=%r",
                    trading_symbol,
                )
                continue
            result = self._analyze_one(
                asset_id,
                asset_name,
                trading_symbol,
                instrument_key,
                access_token,
                local_date,
                update_tracker=update_tracker,
            )
            if result is None:
                failed += 1
                continue
            results.append(result)
        return MomentumAnalysisBatch(results=tuple(results), failed=failed)

    def _analyze_one(
        self,
        asset_id: int | None,
        asset_name: str,
        trading_symbol: str,
        instrument_key: str,
        access_token: str,
        local_date: date,
        *,
        update_tracker: bool,
    ) -> SymbolMomentumAnalysis | None:
        from_date = local_date - timedelta(
            days=self.settings.lookback_days - 1
        )
        hourly_from_date = local_date - timedelta(
            days=self.settings.momentum_hourly_lookback_days - 1
        )
        try:
            candles = self.candle_client.get_daily_candles(
                access_token, instrument_key, from_date, local_date
            )
            hourly_candles = self.candle_client.get_hourly_candles(
                access_token, instrument_key, hourly_from_date, local_date
            )
            daily = calculate_daily_close_momentum(
                candles,
                angle_threshold_degrees=(
                    self.settings.momentum_daily_angle_threshold_degrees
                ),
            )
            hourly = calculate_momentum_indicators(
                hourly_candles,
                angle_threshold_degrees=(
                    self.settings.momentum_angle_threshold_degrees
                ),
            )
            combined = DualTimeframeMomentum(daily, hourly)
        except (IndicatorCalculationError, UpstoxAPIError) as error:
            LOGGER.warning(
                "Momentum analysis failed trading_symbol=%r: %s",
                trading_symbol,
                error,
            )
            return None

        has_momentum = combined.has_momentum
        tracker_updated = False
        if update_tracker:
            try:
                tracker_updated = self.repository.record_momentum_evaluation(
                    asset_id, has_momentum, local_date, combined.side
                )
            except RepositoryError as error:
                LOGGER.warning(
                    "Failed to update tracker trading_symbol=%r: %s",
                    trading_symbol,
                    error,
                )
                return None

        return SymbolMomentumAnalysis(
            asset_id=asset_id,
            asset_name=asset_name,
            trading_symbol=trading_symbol,
            has_momentum=has_momentum,
            side=combined.side,
            tracker_updated=tracker_updated,
        )

    def _valid_token_and_date(
        self, now: datetime | None
    ) -> tuple[str, date]:
        current = now or datetime.now(UTC)
        local_date = current.astimezone(
            ZoneInfo(self.settings.timezone_name)
        ).date()
        try:
            token_state = self.token_store.load()
        except TokenStateError as error:
            raise MomentumAnalysisError(
                "Upstox token state cannot be read."
            ) from error
        if not token_state.is_valid(current):
            raise MomentumAnalysisError(
                "A valid Upstox token is required. Use "
                "`/swingengine auth set <token>`."
            )
        return token_state.access_token, local_date
