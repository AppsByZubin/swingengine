from io import BytesIO
import gzip
import json
from pathlib import Path

import pytest
import requests

from upstox.assets import (
    DEFAULT_ASSET_URL,
    AssetCatalog,
    AssetCatalogError,
    AssetCatalogSettings,
    AssetConfigurationError,
)


class FakeRaw(BytesIO):
    decode_content = True


class FakeResponse:
    def __init__(self, body: bytes, status_code: int = 200):
        self.raw = FakeRaw(body)
        self.status_code = status_code
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self) -> None:
        self.closed = True


def settings(catalog_file: Path, search_limit: int = 20) -> AssetCatalogSettings:
    return AssetCatalogSettings(
        source_url=DEFAULT_ASSET_URL,
        catalog_file=catalog_file,
        request_timeout_seconds=12,
        search_limit=search_limit,
    )


def test_asset_settings_have_persistent_defaults() -> None:
    configured = AssetCatalogSettings.from_env({})

    assert configured.source_url == DEFAULT_ASSET_URL
    assert configured.catalog_file == Path("/var/lib/swingengine/NSE.json")
    assert configured.request_timeout_seconds == 30
    assert configured.search_limit == 20


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("UPSTOX_ASSET_URL", "http://example.test/NSE.json.gz"),
        ("UPSTOX_ASSET_FILE", "NSE.json"),
        ("UPSTOX_ASSET_REQUEST_TIMEOUT_SECONDS", "zero"),
        ("UPSTOX_ASSET_SEARCH_LIMIT", "0"),
    ],
)
def test_invalid_asset_settings_are_rejected(name: str, value: str) -> None:
    with pytest.raises(AssetConfigurationError):
        AssetCatalogSettings.from_env({name: value})


def test_refresh_downloads_unpacks_validates_and_replaces_catalog(
    tmp_path: Path,
) -> None:
    catalog_file = tmp_path / "NSE.json"
    catalog_file.write_text('[{"trading_symbol":"OLD"}]', encoding="utf-8")
    expected_assets = [
        {
            "segment": "NSE_EQ",
            "name": "SUN PHARMACEUTICAL IND L",
            "instrument_type": "EQ",
            "instrument_key": "NSE_EQ|INE044A01036",
            "trading_symbol": "SUNPHARMA",
        },
        {
            "segment": "NSE_EQ",
            "name": "SUN RETAIL LTD",
            "instrument_type": "EQ",
            "instrument_key": "NSE_EQ|INE000000002",
            "trading_symbol": "SUNRETAIL",
        },
    ]
    response = FakeResponse(gzip.compress(json.dumps(expected_assets).encode()))
    calls: list[dict[str, object]] = []

    def http_get(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return response

    catalog = AssetCatalog(settings(catalog_file), http_get=http_get)

    assert catalog.refresh() == 2
    assert json.loads(catalog_file.read_text(encoding="utf-8")) == expected_assets
    assert catalog_file.stat().st_mode & 0o777 == 0o600
    assert response.closed
    assert response.raw.decode_content is False
    assert calls == [
        {
            "url": DEFAULT_ASSET_URL,
            "timeout": 12,
            "stream": True,
            "headers": {"User-Agent": "swingengine/1.0"},
        }
    ]
    assert list(tmp_path.glob(".NSE.json.*")) == []


def test_failed_refresh_preserves_the_existing_catalog(tmp_path: Path) -> None:
    catalog_file = tmp_path / "NSE.json"
    old_content = '[{"trading_symbol":"OLD"}]'
    catalog_file.write_text(old_content, encoding="utf-8")
    response = FakeResponse(gzip.compress(b'{"not":"an array"}'))
    catalog = AssetCatalog(
        settings(catalog_file),
        http_get=lambda *args, **kwargs: response,
    )

    with pytest.raises(AssetCatalogError, match="Unable to refresh"):
        catalog.refresh()

    assert catalog_file.read_text(encoding="utf-8") == old_content
    assert list(tmp_path.glob(".NSE.json.*")) == []


def test_search_is_case_insensitive_ranked_and_limited(tmp_path: Path) -> None:
    catalog_file = tmp_path / "NSE.json"
    assets = [
        {
            "segment": "NSE_FO",
            "name": "SUN PHARMACEUTICAL IND L",
            "instrument_type": "CE",
            "instrument_key": "NSE_FO|100",
            "trading_symbol": "SUNPHARMA 1800 CE",
        },
        {
            "segment": "NSE_EQ",
            "name": "SUN PHARMACEUTICAL IND L",
            "instrument_type": "EQ",
            "instrument_key": "NSE_EQ|SUN",
            "trading_symbol": "SUNPHARMA",
        },
        {
            "segment": "NSE_EQ",
            "name": "Sun Techno Overseas Ltd",
            "instrument_type": "EQ",
            "instrument_key": "NSE_EQ|TECH",
            "trading_symbol": "TECH",
        },
    ]
    catalog_file.write_text(json.dumps(assets), encoding="utf-8")
    catalog = AssetCatalog(settings(catalog_file, search_limit=2))

    matches = catalog.search("  SuN ")

    assert [match.trading_symbol for match in matches] == [
        "SUNPHARMA",
        "TECH",
    ]
    assert matches[0].instrument_key == "NSE_EQ|SUN"


def test_search_matches_instrument_key_and_isin(tmp_path: Path) -> None:
    catalog_file = tmp_path / "NSE.json"
    catalog_file.write_text(
        json.dumps(
            [
                {
                    "segment": "NSE_EQ",
                    "name": "Example",
                    "isin": "INE123456789",
                    "instrument_key": "NSE_EQ|INE123456789",
                    "trading_symbol": "EXAMPLE",
                }
            ]
        ),
        encoding="utf-8",
    )
    catalog = AssetCatalog(settings(catalog_file))

    assert catalog.search("ine123456789")[0].trading_symbol == "EXAMPLE"


def test_search_requires_a_refreshed_catalog(tmp_path: Path) -> None:
    catalog = AssetCatalog(settings(tmp_path / "NSE.json"))

    with pytest.raises(AssetCatalogError, match="instrument refresh"):
        catalog.search("sun")
