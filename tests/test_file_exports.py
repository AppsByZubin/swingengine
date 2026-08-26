import csv
import logging
from datetime import date
from typing import Any

import pytest
from database.repository import AssetRecord, TrackerEntry
from fundamental.scanner import FundamentalStock
from slack.file_exports import (
    DEFAULT_FILES_DIRECTORY,
    CsvFileExporter,
    FileDirectories,
    FileExportError,
    FileStorageError,
    configured_files_directory,
)
from tracker.momentum_scanner import MomentumStock


def test_file_directory_uses_environment_override(tmp_path) -> None:
    assert configured_files_directory({}) == DEFAULT_FILES_DIRECTORY
    assert configured_files_directory(
        {"SWINGENGINE_FILES_DIR": f"  {tmp_path}  "}
    ) == tmp_path


def test_file_directories_are_created_idempotently(tmp_path) -> None:
    root = tmp_path / "files"

    first = FileDirectories.create(root)
    second = FileDirectories.create(root)

    assert first == second
    assert first.input.is_dir()
    assert first.output.is_dir()


def test_file_directories_reject_a_read_only_location(
    monkeypatch,
    tmp_path,
) -> None:
    def reject_write(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(
        "slack.file_exports.NamedTemporaryFile",
        reject_write,
    )

    with pytest.raises(FileStorageError, match="SWINGENGINE_FILES_DIR"):
        FileDirectories.create(tmp_path / "files")


def test_asset_csv_contains_database_fields(tmp_path) -> None:
    exporter = CsvFileExporter(tmp_path)
    upload = exporter.export_assets(
        [
            AssetRecord(
                asset_id=42,
                asset_name="SUN PHARMACEUTICAL IND L",
                trading_symbol="SUNPHARMA",
                instrument_key="NSE_EQ|INE044A01036",
            )
        ]
    )

    with upload.path.open(encoding="utf-8", newline="") as exported_file:
        rows = list(csv.reader(exported_file))

    assert upload.path == tmp_path / "asset-list.csv"
    assert rows == [
        ["asset_id", "asset_name", "trading_symbol", "instrument_key"],
        [
            "42",
            "SUN PHARMACEUTICAL IND L",
            "SUNPHARMA",
            "NSE_EQ|INE044A01036",
        ],
    ]


def test_tracker_csv_contains_database_fields(tmp_path) -> None:
    exporter = CsvFileExporter(tmp_path)
    upload = exporter.export_tracker(
        [
            TrackerEntry(
                tracker_details_id=7,
                asset_id=42,
                asset_name="SUN PHARMACEUTICAL IND L",
                trading_symbol="SUNPHARMA",
                has_momentum=True,
                is_trade_created=False,
                is_approved_for_trade=True,
                amount_allocated=12500.5,
                added_date=date(2026, 7, 28),
                has_fno=True,
                side="buy",
            )
        ]
    )

    with upload.path.open(encoding="utf-8", newline="") as exported_file:
        rows = list(csv.reader(exported_file))

    assert upload.path == tmp_path / "tracker-list.csv"
    assert rows == [
        [
            "asset_name",
            "trading_symbol",
            "has_momentum",
            "is_trade_created",
            "is_approved_for_trade",
            "amount_allocated",
            "added_date",
            "has_fno",
            "side",
        ],
        [
            "SUN PHARMACEUTICAL IND L",
            "SUNPHARMA",
            "True",
            "False",
            "True",
            "12500.5",
            "2026-07-28",
            "True",
            "buy",
        ],
    ]


def test_momentum_csv_contains_requested_fields(tmp_path) -> None:
    upload = CsvFileExporter(tmp_path).export_momentum(
        [
            MomentumStock(
                asset_name="SUN PHARMACEUTICAL IND L",
                trading_symbol="SUNPHARMA",
                ltp=1789.25,
            )
        ]
    )

    with upload.path.open(encoding="utf-8", newline="") as exported_file:
        rows = list(csv.reader(exported_file))

    assert upload.path == tmp_path / "momentum-list.csv"
    assert rows == [
        ["assetname", "trading_symbol", "ltp"],
        ["SUN PHARMACEUTICAL IND L", "SUNPHARMA", "1789.25"],
    ]


def test_fundamental_csv_contains_score_and_company_context(tmp_path) -> None:
    upload = CsvFileExporter(tmp_path).export_fundamental(
        [
            FundamentalStock(
                asset_name="SUN PHARMACEUTICAL IND L",
                trading_symbol="SUNPHARMA",
                isin="INE044A01036",
                score=82.5,
                rating="STRONG",
                confidence=95.0,
                sector="Pharmaceuticals",
                latest_financial_period="Mar 2026",
                has_fno=True,
            )
        ]
    )

    with upload.path.open(encoding="utf-8", newline="") as exported_file:
        rows = list(csv.reader(exported_file))

    assert upload.path == tmp_path / "fundamental-list.csv"
    assert rows == [
        [
            "assetname",
            "trading_symbol",
            "isin",
            "fundamental_score",
            "rating",
            "confidence",
            "sector",
            "latest_financial_period",
            "has_fno",
        ],
        [
            "SUN PHARMACEUTICAL IND L",
            "SUNPHARMA",
            "INE044A01036",
            "82.5",
            "STRONG",
            "95.0",
            "Pharmaceuticals",
            "Mar 2026",
            "True",
        ],
    ]


def test_empty_export_still_contains_csv_headings(tmp_path) -> None:
    upload = CsvFileExporter(tmp_path).export_assets([])

    assert upload.path.read_text(encoding="utf-8") == (
        "asset_id,asset_name,trading_symbol,instrument_key\n"
    )


def test_export_escapes_spreadsheet_formula_cells(tmp_path) -> None:
    upload = CsvFileExporter(tmp_path).export_assets(
        [
            AssetRecord(
                asset_id=1,
                asset_name="=DANGEROUS()",
                trading_symbol="+SYMBOL",
                instrument_key="@KEY",
            )
        ]
    )

    with upload.path.open(encoding="utf-8", newline="") as exported_file:
        rows = list(csv.reader(exported_file))

    assert rows[1] == ["1", "'=DANGEROUS()", "'+SYMBOL", "'@KEY"]


def test_successful_export_logs_start_and_completion(
    tmp_path, caplog: Any
) -> None:
    with caplog.at_level(logging.INFO, logger="slack.file_exports"):
        CsvFileExporter(tmp_path).export_assets([])

    assert (
        f"Starting CSV export filename='asset-list.csv' "
        f"output_directory={tmp_path} target={tmp_path / 'asset-list.csv'}"
        in caplog.messages
    )
    assert (
        f"Completed CSV export filename='asset-list.csv' "
        f"target={tmp_path / 'asset-list.csv'}"
        in caplog.messages
    )


def test_failed_export_logs_root_cause_and_paths(
    tmp_path, caplog: Any
) -> None:
    output_directory = tmp_path / "output"
    output_directory.write_text("not a directory", encoding="utf-8")

    with (
        caplog.at_level(logging.ERROR, logger="slack.file_exports"),
        pytest.raises(FileExportError, match="Unable to generate asset-list.csv"),
    ):
        CsvFileExporter(output_directory).export_assets([])

    assert "Unable to generate CSV export filename='asset-list.csv'" in caplog.text
    assert f"output_directory={output_directory}" in caplog.text
    assert f"target={output_directory / 'asset-list.csv'}" in caplog.text
    assert "FileExistsError" in caplog.text
