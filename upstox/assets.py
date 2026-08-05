"""Download, persist, and search the Upstox NSE instrument catalog."""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
import gzip
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
from threading import RLock
from typing import Any
from urllib.parse import urlparse

import requests

DEFAULT_ASSET_URL = (
    "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
)

LOGGER = logging.getLogger(__name__)


class AssetConfigurationError(ValueError):
    """Raised when asset catalog configuration is invalid."""


class AssetCatalogError(RuntimeError):
    """Raised when the asset catalog cannot be refreshed or searched."""


@dataclass(frozen=True)
class AssetCatalogSettings:
    """Environment-backed asset catalog settings."""

    source_url: str
    catalog_file: Path
    request_timeout_seconds: int
    search_limit: int

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "AssetCatalogSettings":
        values = os.environ if env is None else env
        source_url = values.get("UPSTOX_ASSET_URL", DEFAULT_ASSET_URL).strip()
        catalog_file = Path(
            values.get(
                "UPSTOX_ASSET_FILE",
                "/var/lib/swingengine/NSE.json",
            ).strip()
        )
        errors: list[str] = []

        parsed_url = urlparse(source_url)
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            errors.append("UPSTOX_ASSET_URL must be an absolute HTTPS URL")
        if not catalog_file.is_absolute():
            errors.append("UPSTOX_ASSET_FILE must be an absolute path")

        request_timeout_seconds = _positive_int(
            values,
            "UPSTOX_ASSET_REQUEST_TIMEOUT_SECONDS",
            30,
            errors,
        )
        search_limit = _positive_int(
            values,
            "UPSTOX_ASSET_SEARCH_LIMIT",
            20,
            errors,
        )

        if errors:
            raise AssetConfigurationError("; ".join(errors))
        return cls(
            source_url=source_url,
            catalog_file=catalog_file,
            request_timeout_seconds=request_timeout_seconds,
            search_limit=search_limit,
        )


def _positive_int(
    values: Mapping[str, str],
    name: str,
    default: int,
    errors: list[str],
) -> int:
    try:
        value = int(values.get(name, str(default)).strip())
    except ValueError:
        value = 0
    if value <= 0:
        errors.append(f"{name} must be a positive integer")
        return default
    return value


@dataclass(frozen=True, slots=True)
class AssetSearchResult:
    """The fields needed to identify an instrument returned by a search."""

    trading_symbol: str
    name: str
    segment: str
    instrument_type: str
    instrument_key: str
    isin: str = ""


@dataclass(frozen=True, slots=True)
class _CatalogAsset:
    result: AssetSearchResult
    searchable_text: str


HttpGet = Callable[..., requests.Response]


class AssetCatalog:
    """Atomically refresh and search a local Upstox instrument catalog."""

    def __init__(
        self,
        settings: AssetCatalogSettings,
        http_get: HttpGet | None = None,
    ):
        self.settings = settings
        self._http_get = http_get or requests.get
        self._lock = RLock()
        self._cache: tuple[_CatalogAsset, ...] | None = None
        self._cache_signature: tuple[int, int] | None = None

    def refresh(self) -> int:
        """Download the gzip catalog and atomically replace the JSON file."""
        with self._lock:
            parent = self.settings.catalog_file.parent
            compressed_path: Path | None = None
            json_path: Path | None = None
            LOGGER.info(
                "Starting Upstox NSE asset catalog refresh source_url=%s "
                "catalog_file=%s",
                self.settings.source_url,
                self.settings.catalog_file,
            )
            try:
                parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                compressed_path = _temporary_path(
                    parent, f".{self.settings.catalog_file.name}.download."
                )
                json_path = _temporary_path(
                    parent, f".{self.settings.catalog_file.name}.unpacked."
                )
                self._download(compressed_path)
                self._unpack(compressed_path, json_path)
                assets = self._read_catalog(json_path)

                os.chmod(json_path, 0o600)
                stat = json_path.stat()
                os.replace(json_path, self.settings.catalog_file)
                json_path = None
                self._cache = assets
                self._cache_signature = (stat.st_mtime_ns, stat.st_size)
                LOGGER.info(
                    "Completed Upstox NSE asset catalog refresh "
                    "catalog_file=%s asset_count=%d",
                    self.settings.catalog_file,
                    len(assets),
                )
                return len(assets)
            except (
                OSError,
                ValueError,
                TypeError,
                json.JSONDecodeError,
                requests.RequestException,
            ) as error:
                LOGGER.exception(
                    "Unable to refresh Upstox NSE asset catalog "
                    "source_url=%s catalog_file=%s",
                    self.settings.source_url,
                    self.settings.catalog_file,
                )
                raise AssetCatalogError(
                    "Unable to refresh the Upstox NSE asset catalog"
                ) from error
            finally:
                for temporary_path in (compressed_path, json_path):
                    if temporary_path is not None:
                        try:
                            temporary_path.unlink(missing_ok=True)
                        except OSError:
                            pass

    def search(
        self, query: str, limit: int | None = None
    ) -> list[AssetSearchResult]:
        """Return ranked, case-insensitive substring matches."""
        normalized_query = query.strip().casefold()
        if not normalized_query:
            raise ValueError("Asset search query cannot be empty")

        result_limit = self.settings.search_limit if limit is None else limit
        if result_limit <= 0:
            raise ValueError("Asset search limit must be positive")

        with self._lock:
            assets = self._load_catalog()

        ranked_matches: list[
            tuple[tuple[int, int, int, str, str, str], AssetSearchResult]
        ] = []
        for asset in assets:
            if normalized_query not in asset.searchable_text:
                continue

            result = asset.result
            ranked_matches.append(
                (_match_rank(normalized_query, result), result)
            )

        ranked_matches.sort(key=lambda match: match[0])
        return [result for _, result in ranked_matches[:result_limit]]

    def list_equities(self) -> list[AssetSearchResult]:
        """Return all NSE equity instruments in trading-symbol order."""
        with self._lock:
            assets = self._load_catalog()

        equities = [
            asset.result
            for asset in assets
            if asset.result.segment.casefold() == "nse_eq"
            and asset.result.instrument_type.casefold() == "eq"
        ]
        equities.sort(
            key=lambda asset: (
                asset.trading_symbol.casefold(),
                asset.name.casefold(),
                asset.instrument_key.casefold(),
            )
        )
        LOGGER.info(
            "Selected NSE equity instruments catalog_file=%s "
            "equity_count=%d",
            self.settings.catalog_file,
            len(equities),
        )
        return equities

    def _download(self, destination: Path) -> None:
        response = self._http_get(
            self.settings.source_url,
            timeout=self.settings.request_timeout_seconds,
            stream=True,
            headers={"User-Agent": "swingengine/1.0"},
        )
        try:
            response.raise_for_status()
            # Preserve the gzip bytes even if the server also supplies a
            # Content-Encoding header.
            if hasattr(response.raw, "decode_content"):
                response.raw.decode_content = False
            with destination.open("wb") as handle:
                shutil.copyfileobj(response.raw, handle)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            response.close()

    @staticmethod
    def _unpack(source: Path, destination: Path) -> None:
        with gzip.open(source, "rb") as compressed:
            with destination.open("wb") as unpacked:
                shutil.copyfileobj(compressed, unpacked)
                unpacked.flush()
                os.fsync(unpacked.fileno())

    def _load_catalog(self) -> tuple[_CatalogAsset, ...]:
        try:
            stat = self.settings.catalog_file.stat()
        except FileNotFoundError as error:
            raise AssetCatalogError(
                "Asset catalog is not available. Run "
                "`/swingengine instrument refresh` first."
            ) from error
        except OSError as error:
            raise AssetCatalogError(
                "Unable to read the Upstox NSE asset catalog"
            ) from error

        signature = (stat.st_mtime_ns, stat.st_size)
        if self._cache is not None and self._cache_signature == signature:
            return self._cache

        try:
            assets = self._read_catalog(self.settings.catalog_file)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise AssetCatalogError(
                "Unable to read the Upstox NSE asset catalog"
            ) from error
        self._cache = assets
        self._cache_signature = signature
        return assets

    @staticmethod
    def _read_catalog(path: Path) -> tuple[_CatalogAsset, ...]:
        with path.open(encoding="utf-8") as handle:
            raw_assets = json.load(handle)
        if not isinstance(raw_assets, list) or not raw_assets:
            raise ValueError("asset catalog must be a non-empty JSON array")
        if not all(isinstance(asset, dict) for asset in raw_assets):
            raise ValueError("each asset catalog entry must be a JSON object")
        return tuple(_catalog_asset(asset) for asset in raw_assets)


def _temporary_path(parent: Path, prefix: str) -> Path:
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=prefix, dir=parent
    )
    os.close(file_descriptor)
    return Path(temporary_name)


def _searchable_fields(asset: Mapping[str, Any]) -> Iterator[str]:
    for field_name in (
        "trading_symbol",
        "name",
        "asset_symbol",
        "underlying_symbol",
        "instrument_key",
        "isin",
    ):
        value = asset.get(field_name)
        if value is not None:
            yield str(value).casefold()


def _catalog_asset(asset: Mapping[str, Any]) -> _CatalogAsset:
    result = AssetSearchResult(
        trading_symbol=str(asset.get("trading_symbol", "")).strip(),
        name=str(asset.get("name", "")).strip(),
        segment=str(asset.get("segment", "")).strip(),
        instrument_type=str(asset.get("instrument_type", "")).strip(),
        instrument_key=str(asset.get("instrument_key", "")).strip(),
        isin=str(asset.get("isin", "")).strip(),
    )
    return _CatalogAsset(
        result=result,
        searchable_text="\0".join(_searchable_fields(asset)),
    )


def _match_rank(
    normalized_query: str, asset: AssetSearchResult
) -> tuple[int, int, int, str, str, str]:
    symbol = asset.trading_symbol.casefold()
    name = asset.name.casefold()
    if symbol == normalized_query:
        text_rank = 0
    elif symbol.startswith(normalized_query):
        text_rank = 1
    elif name.startswith(normalized_query):
        text_rank = 2
    elif any(
        word.startswith(normalized_query)
        for word in f"{symbol} {name}".split()
    ):
        text_rank = 3
    else:
        text_rank = 4

    segment_rank = 0 if asset.segment == "NSE_EQ" else 1
    return (
        0 if text_rank == 0 else 1,
        segment_rank,
        text_rank,
        symbol,
        name,
        asset.instrument_key.casefold(),
    )
