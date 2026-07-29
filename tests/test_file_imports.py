from pathlib import Path
from typing import Any

import pytest

from database.repository import (
    AssetAlreadyExistsError,
    AssetInUseError,
    AssetNotFoundError,
    AssetRecord,
)
from slack.file_imports import AssetImportError, CsvAssetImporter
from upstox.assets import AssetSearchResult


class FakeAssetService:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str) -> list[AssetSearchResult]:
        self.queries.append(query)
        if query == "UNKNOWN":
            return []
        return [
            AssetSearchResult(
                trading_symbol=query,
                name=f"{query} LIMITED",
                segment="NSE_EQ",
                instrument_type="EQ",
                instrument_key=f"NSE_EQ|{query}",
            )
        ]


class FakeRepository:
    def __init__(self, symbols: set[str] | None = None) -> None:
        self.symbols = set(symbols or ())
        self.tracked: set[str] = set()
        self.added: list[str] = []
        self.deleted: list[str] = []

    def add_asset(self, asset: AssetSearchResult) -> AssetRecord:
        symbol = asset.trading_symbol
        if symbol in self.symbols:
            raise AssetAlreadyExistsError
        self.symbols.add(symbol)
        self.added.append(symbol)
        return _asset_record(symbol)

    def delete_asset(self, trading_symbol: str) -> AssetRecord:
        if trading_symbol in self.tracked:
            raise AssetInUseError
        if trading_symbol not in self.symbols:
            raise AssetNotFoundError
        self.symbols.remove(trading_symbol)
        self.deleted.append(trading_symbol)
        return _asset_record(trading_symbol)


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int) -> list[bytes]:
        assert chunk_size == 64 * 1024
        return [self._content]

    def close(self) -> None:
        self.closed = True


def _asset_record(symbol: str) -> AssetRecord:
    return AssetRecord(
        asset_id=1,
        asset_name=f"{symbol} LIMITED",
        trading_symbol=symbol,
        instrument_key=f"NSE_EQ|{symbol}",
    )


def _importer(
    tmp_path: Path,
    asset_service: FakeAssetService,
    repository: FakeRepository,
    **kwargs: Any,
) -> CsvAssetImporter:
    return CsvAssetImporter(
        tmp_path,
        asset_service,
        repository,
        "xoxb-secret",
        **kwargs,
    )


def test_import_applies_sample_adds_and_delete_with_uppercase_symbols(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "assets.csv"
    csv_path.write_text(
        "name,action\n"
        "reliance,add\n"
        "tcs,delete\n"
        "infosys,add\n"
        "tmvc,add\n"
        "tmpv,add\n",
        encoding="utf-8",
    )
    asset_service = FakeAssetService()
    repository = FakeRepository({"TCS"})

    summary = _importer(
        tmp_path,
        asset_service,
        repository,
    ).import_path(csv_path)

    assert summary.total == 5
    assert summary.added == 4
    assert summary.deleted == 1
    assert summary.already_present == 0
    assert summary.failed == 0
    assert repository.added == ["RELIANCE", "INFOSYS", "TMVC", "TMPV"]
    assert repository.deleted == ["TCS"]
    assert asset_service.queries == ["RELIANCE", "INFOSYS", "TMVC", "TMPV"]


def test_import_reports_duplicates_and_row_errors(tmp_path: Path) -> None:
    csv_path = tmp_path / "assets.csv"
    csv_path.write_text(
        "name,action\n"
        "reliance,add\n"
        "missing,delete\n"
        "unknown,add\n"
        "tcs,hold\n"
        ",add\n",
        encoding="utf-8",
    )
    repository = FakeRepository({"RELIANCE"})

    summary = _importer(
        tmp_path,
        FakeAssetService(),
        repository,
    ).import_path(csv_path)

    assert summary.total == 5
    assert summary.already_present == 1
    assert summary.failed == 4
    assert len(summary.issues) == 4
    assert "asset is not present" in summary.issues[0]
    assert "no exact NSE trading symbol" in summary.issues[1]
    assert "must be `add` or `delete`" in summary.issues[2]
    assert "trading symbol is empty" in summary.issues[3]
    assert "Already present: 1" in summary.slack_message()


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("symbol,action\nTCS,add\n", "exactly `name,action`"),
        ("name,name,action\nTCS,TCS,add\n", "exactly `name,action`"),
        ("name,action\n", "contains no asset rows"),
        ("name,action\nTCS,\n", "action must be"),
    ],
)
def test_import_validates_csv_structure(
    tmp_path: Path,
    contents: str,
    message: str,
) -> None:
    csv_path = tmp_path / "assets.csv"
    csv_path.write_text(contents, encoding="utf-8")
    importer = _importer(
        tmp_path,
        FakeAssetService(),
        FakeRepository(),
    )

    if contents == "name,action\nTCS,\n":
        summary = importer.import_path(csv_path)
        assert summary.failed == 1
        assert message in summary.issues[0]
    else:
        with pytest.raises(AssetImportError, match=message):
            importer.import_path(csv_path)


def test_row_limit_is_validated_before_any_database_changes(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "assets.csv"
    csv_path.write_text(
        "name,action\n"
        + "".join(f"SYMBOL{index},add\n" for index in range(1_001)),
        encoding="utf-8",
    )
    repository = FakeRepository()
    importer = _importer(
        tmp_path,
        FakeAssetService(),
        repository,
    )

    with pytest.raises(AssetImportError, match="1,000-row"):
        importer.import_path(csv_path)

    assert repository.added == []


def test_slack_file_is_downloaded_with_bot_auth_and_imported(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    response = FakeResponse(b"name,action\nreliance,add\n")

    def http_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append((url, kwargs))
        return response

    importer = _importer(
        tmp_path,
        FakeAssetService(),
        FakeRepository(),
        http_get=http_get,
    )
    summary = importer.import_slack_file(
        {
            "id": "F123",
            "name": "assets.csv",
            "size": 25,
            "url_private_download": (
                "https://files.slack.com/files-pri/T123-F123/assets.csv"
            ),
        },
        client=object(),
    )

    assert summary.added == 1
    assert calls == [
        (
            "https://files.slack.com/files-pri/T123-F123/assets.csv",
            {
                "headers": {"Authorization": "Bearer xoxb-secret"},
                "stream": True,
                "timeout": 30,
            },
        )
    ]
    assert response.closed is True
    assert (tmp_path / "asset-import-F123.csv").read_bytes() == (
        b"name,action\nreliance,add\n"
    )


def test_slack_file_metadata_is_resolved_when_modal_only_supplies_an_id(
    tmp_path: Path,
) -> None:
    class FakeClient:
        def files_info(self, file: str) -> dict[str, Any]:
            assert file == "F456"
            return {
                "file": {
                    "id": file,
                    "name": "assets.csv",
                    "size": 21,
                    "url_private": (
                        "https://files.slack.com/files-pri/T123-F456/assets.csv"
                    ),
                }
            }

    importer = _importer(
        tmp_path,
        FakeAssetService(),
        FakeRepository(),
        http_get=lambda *_args, **_kwargs: FakeResponse(
            b"name,action\ntcs,add\n"
        ),
    )

    summary = importer.import_slack_file({"id": "F456"}, FakeClient())

    assert summary.added == 1


@pytest.mark.parametrize(
    "file_info",
    [
        {
            "id": "F123",
            "name": "assets.txt",
            "url_private": "https://files.slack.com/assets.txt",
        },
        {
            "id": "F123",
            "name": "assets.csv",
            "size": 1_000_001,
            "url_private": "https://files.slack.com/assets.csv",
        },
        {
            "id": "F123",
            "name": "assets.csv",
            "url_private": "https://example.com/assets.csv",
        },
    ],
)
def test_slack_file_type_size_and_url_are_validated(
    tmp_path: Path,
    file_info: dict[str, Any],
) -> None:
    importer = _importer(
        tmp_path,
        FakeAssetService(),
        FakeRepository(),
    )

    with pytest.raises(AssetImportError):
        importer.import_slack_file(file_info, client=object())
