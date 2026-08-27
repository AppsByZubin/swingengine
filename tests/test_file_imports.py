from datetime import date
from pathlib import Path
from typing import Any

import pytest

from database.repository import (
    AssetAlreadyExistsError,
    AssetInUseError,
    AssetNotFoundError,
    AssetRecord,
    TrackerEntry,
    TrackerNotFoundError,
)
from slack.file_imports import (
    AssetImportError,
    CsvAssetImporter,
    CsvTrackerImporter,
    TrackerImportError,
)
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
                isin="INE044A01036" if query == "FNOSTOCK" else "",
            )
        ]

    def fno_isins(self) -> frozenset[str]:
        return frozenset({"INE044A01036"})


class FakeRepository:
    def __init__(self, symbols: set[str] | None = None) -> None:
        self.symbols = set(symbols or ())
        self.tracked: set[str] = set()
        self.added: list[str] = []
        self.added_has_fno: dict[str, bool] = {}
        self.deleted: list[str] = []

    def add_asset(
        self, asset: AssetSearchResult, has_fno: bool = False
    ) -> AssetRecord:
        symbol = asset.trading_symbol
        if symbol in self.symbols:
            raise AssetAlreadyExistsError
        self.symbols.add(symbol)
        self.added.append(symbol)
        self.added_has_fno[symbol] = has_fno
        return _asset_record(symbol)

    def delete_asset(self, trading_symbol: str) -> AssetRecord:
        if trading_symbol in self.tracked:
            raise AssetInUseError
        if trading_symbol not in self.symbols:
            raise AssetNotFoundError
        self.symbols.remove(trading_symbol)
        self.deleted.append(trading_symbol)
        return _asset_record(trading_symbol)


class FakeTrackerRepository:
    def __init__(self, missing_symbols: set[str] | None = None) -> None:
        self.missing_symbols = set(missing_symbols or ())
        self.updates: list[tuple[str, bool, float]] = []

    def update_tracker_trade_settings(
        self,
        trading_symbol: str,
        is_approved_for_trade: bool,
        amount_allocated: float,
    ) -> TrackerEntry:
        if trading_symbol in self.missing_symbols:
            raise TrackerNotFoundError
        self.updates.append(
            (
                trading_symbol,
                is_approved_for_trade,
                amount_allocated,
            )
        )
        return TrackerEntry(
            tracker_details_id=1,
            asset_id=2,
            asset_name=f"{trading_symbol} LIMITED",
            trading_symbol=trading_symbol,
            has_momentum=True,
            is_trade_created=False,
            is_approved_for_trade=is_approved_for_trade,
            amount_allocated=amount_allocated,
            added_date=date(2026, 7, 30),
        )


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


def _tracker_importer(
    tmp_path: Path,
    repository: FakeTrackerRepository,
    **kwargs: Any,
) -> CsvTrackerImporter:
    return CsvTrackerImporter(
        tmp_path,
        repository,
        "xoxb-secret",
        **kwargs,
    )


TRACKER_HEADER = (
    "asset_name,trading_symbol,has_momentum,is_trade_created,"
    "is_approved_for_trade,amount_allocated,added_date\n"
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
    assert not any(repository.added_has_fno.values())


def test_import_marks_fno_eligible_symbols_as_such(tmp_path: Path) -> None:
    csv_path = tmp_path / "assets.csv"
    csv_path.write_text(
        "name,action\nfnostock,add\nreliance,add\n",
        encoding="utf-8",
    )
    repository = FakeRepository()

    summary = _importer(
        tmp_path,
        FakeAssetService(),
        repository,
    ).import_path(csv_path)

    assert summary.added == 2
    assert repository.added_has_fno["FNOSTOCK"] is True
    assert repository.added_has_fno["RELIANCE"] is False


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


def test_tracker_import_updates_only_approval_and_allocation(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "tracker.csv"
    csv_path.write_text(
        TRACKER_HEADER
        + "Tampered Name,tcs,False,True,True,5000.01,1999-01-01\n"
        + "Another Name,infy,True,False,False,0,2030-12-31\n",
        encoding="utf-8",
    )
    repository = FakeTrackerRepository()

    summary = _tracker_importer(
        tmp_path,
        repository,
    ).import_path(csv_path)

    assert summary.total == 2
    assert summary.updated == 2
    assert summary.failed == 0
    assert repository.updates == [
        ("TCS", True, 5000.01),
        ("INFY", False, 0.0),
    ]
    assert "Updated: 2" in summary.slack_message()


def test_tracker_import_accepts_the_exported_has_fno_and_side_columns(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "tracker.csv"
    csv_path.write_text(
        "asset_name,trading_symbol,has_momentum,is_trade_created,"
        "is_approved_for_trade,amount_allocated,added_date,has_fno,side\n"
        "Tata Consultancy,TCS,False,False,True,5000.01,2026-07-30,"
        "True,buy\n",
        encoding="utf-8",
    )
    repository = FakeTrackerRepository()

    summary = _tracker_importer(
        tmp_path,
        repository,
    ).import_path(csv_path)

    assert summary.total == 1
    assert summary.updated == 1
    assert summary.failed == 0
    assert repository.updates == [("TCS", True, 5000.01)]


def test_tracker_import_validates_approval_amount_and_duplicates(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "tracker.csv"
    csv_path.write_text(
        TRACKER_HEADER
        + "TCS,TCS,True,False,True,1499,2026-07-30\n"
        + "INFY,INFY,True,False,yes,8000,2026-07-30\n"
        + "RELIANCE,RELIANCE,True,False,False,-1,2026-07-30\n"
        + "TCS,TCS,True,False,False,0,2026-07-30\n"
        + "WIPRO,WIPRO,True,False,False,0,2026-07-30\n",
        encoding="utf-8",
    )
    repository = FakeTrackerRepository()

    summary = _tracker_importer(
        tmp_path,
        repository,
    ).import_path(csv_path)

    assert summary.total == 5
    assert summary.updated == 1
    assert summary.failed == 4
    assert repository.updates == [("WIPRO", False, 0.0)]
    assert "must be at least 1500" in summary.issues[0]
    assert "must be `True` or `False`" in summary.issues[1]
    assert "must be a nonnegative number" in summary.issues[2]
    assert "duplicate trading symbol" in summary.issues[3]


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (
            "trading_symbol,is_approved_for_trade,amount_allocated\n"
            "TCS,True,6000\n",
            "header must contain",
        ),
        (TRACKER_HEADER, "contains no tracker rows"),
    ],
)
def test_tracker_import_requires_the_exported_csv_structure(
    tmp_path: Path,
    contents: str,
    message: str,
) -> None:
    csv_path = tmp_path / "tracker.csv"
    csv_path.write_text(contents, encoding="utf-8")

    with pytest.raises(TrackerImportError, match=message):
        _tracker_importer(
            tmp_path,
            FakeTrackerRepository(),
        ).import_path(csv_path)


def test_tracker_import_reports_symbols_that_are_not_tracked(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "tracker.csv"
    csv_path.write_text(
        TRACKER_HEADER
        + "Unknown,missing,True,False,False,0,2026-07-30\n",
        encoding="utf-8",
    )

    summary = _tracker_importer(
        tmp_path,
        FakeTrackerRepository({"MISSING"}),
    ).import_path(csv_path)

    assert summary.updated == 0
    assert summary.failed == 1
    assert "asset is not tracked" in summary.issues[0]


def test_tracker_slack_file_is_downloaded_and_imported(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    response = FakeResponse(
        (
            TRACKER_HEADER
            + "TCS,tcs,True,False,True,7500,2026-07-30\n"
        ).encode()
    )

    def http_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append((url, kwargs))
        return response

    repository = FakeTrackerRepository()
    summary = _tracker_importer(
        tmp_path,
        repository,
        http_get=http_get,
    ).import_slack_file(
        {
            "id": "F789",
            "name": "tracker-list.csv",
            "size": 200,
            "url_private_download": (
                "https://files.slack.com/files-pri/T123-F789/tracker-list.csv"
            ),
        },
        client=object(),
    )

    assert summary.updated == 1
    assert repository.updates == [("TCS", True, 7500.0)]
    assert calls[0][1]["headers"] == {
        "Authorization": "Bearer xoxb-secret"
    }
    assert response.closed is True
    assert (tmp_path / "tracker-import-F789.csv").exists()
