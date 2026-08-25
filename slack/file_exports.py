"""Local file storage and CSV exports for Slack commands."""

import csv
import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping

from database.repository import AssetRecord, TrackerEntry
from fundamental.scanner import FundamentalStock
from tracker.momentum_scanner import MomentumStock

LOGGER = logging.getLogger(__name__)

DEFAULT_FILES_DIRECTORY = Path(__file__).resolve().parent.parent / "files"
FILES_DIRECTORY_ENVIRONMENT_VARIABLE = "SWINGENGINE_FILES_DIR"


class FileStorageError(RuntimeError):
    """Raised when SwingEngine cannot initialize its local file storage."""


class FileExportError(RuntimeError):
    """Raised when SwingEngine cannot generate a requested export."""


def configured_files_directory(
    env: Mapping[str, str] | None = None,
) -> Path:
    """Return the runtime file root configured for this process."""
    values = os.environ if env is None else env
    configured_path = values.get(
        FILES_DIRECTORY_ENVIRONMENT_VARIABLE,
        "",
    ).strip()
    return (
        Path(configured_path)
        if configured_path
        else DEFAULT_FILES_DIRECTORY
    )


@dataclass(frozen=True, slots=True)
class FileDirectories:
    """Directories used for incoming and generated files."""

    input: Path
    output: Path

    @classmethod
    def create(
        cls,
        root: str | Path = DEFAULT_FILES_DIRECTORY,
    ) -> "FileDirectories":
        root_path = Path(root)
        input_directory = root_path / "input"
        output_directory = root_path / "output"
        try:
            input_directory.mkdir(parents=True, exist_ok=True)
            output_directory.mkdir(parents=True, exist_ok=True)
            for directory in (input_directory, output_directory):
                with NamedTemporaryFile(
                    prefix=".swingengine-write-test-",
                    dir=directory,
                ):
                    pass
        except OSError as error:
            LOGGER.exception(
                "File directory is unavailable or read-only root=%s "
                "input=%s output=%s",
                root_path,
                input_directory,
                output_directory,
            )
            raise FileStorageError(
                f"File directory {root_path} is unavailable or read-only. "
                f"Set {FILES_DIRECTORY_ENVIRONMENT_VARIABLE} to a writable "
                "location."
            ) from error
        return cls(input=input_directory, output=output_directory)


@dataclass(frozen=True, slots=True)
class SlackFileUpload:
    """A generated file and the Slack metadata used to share it."""

    path: Path
    title: str
    initial_comment: str


class CsvFileExporter:
    """Generate atomic CSV snapshots in the output directory."""

    def __init__(self, output_directory: str | Path):
        self._output_directory = Path(output_directory)

    def export_assets(
        self,
        assets: Sequence[AssetRecord],
    ) -> SlackFileUpload:
        path = self._write_csv(
            "asset-list.csv",
            ("asset_id", "asset_name", "trading_symbol", "instrument_key"),
            (
                (
                    asset.asset_id,
                    asset.asset_name,
                    asset.trading_symbol,
                    asset.instrument_key or "",
                )
                for asset in assets
            ),
        )
        return SlackFileUpload(
            path=path,
            title="SwingEngine saved assets",
            initial_comment="Saved assets exported by SwingEngine.",
        )

    def export_tracker(
        self,
        entries: Sequence[TrackerEntry],
    ) -> SlackFileUpload:
        path = self._write_csv(
            "tracker-list.csv",
            (
                "asset_name",
                "trading_symbol",
                "has_momentum",
                "is_trade_created",
                "is_approved_for_trade",
                "amount_allocated",
                "added_date",
                "has_fno",
            ),
            (
                (
                    entry.asset_name,
                    entry.trading_symbol,
                    entry.has_momentum,
                    entry.is_trade_created,
                    entry.is_approved_for_trade,
                    entry.amount_allocated,
                    entry.added_date.isoformat(),
                    entry.has_fno,
                )
                for entry in entries
            ),
        )
        return SlackFileUpload(
            path=path,
            title="SwingEngine tracker",
            initial_comment="Tracked assets exported by SwingEngine.",
        )

    def export_momentum(
        self,
        stocks: Sequence[MomentumStock],
    ) -> SlackFileUpload:
        path = self._write_csv(
            "momentum-list.csv",
            ("assetname", "trading_symbol", "ltp"),
            (
                (
                    stock.asset_name,
                    stock.trading_symbol,
                    stock.ltp,
                )
                for stock in stocks
            ),
        )
        return SlackFileUpload(
            path=path,
            title="SwingEngine NSE momentum stocks",
            initial_comment="NSE momentum stocks exported by SwingEngine.",
        )

    def export_fundamental(
        self,
        stocks: Sequence[FundamentalStock],
    ) -> SlackFileUpload:
        path = self._write_csv(
            "fundamental-list.csv",
            (
                "assetname",
                "trading_symbol",
                "isin",
                "fundamental_score",
                "rating",
                "confidence",
                "sector",
                "latest_financial_period",
                "has_fno",
            ),
            (
                (
                    stock.asset_name,
                    stock.trading_symbol,
                    stock.isin,
                    stock.score,
                    stock.rating,
                    stock.confidence,
                    stock.sector,
                    stock.latest_financial_period,
                    stock.has_fno,
                )
                for stock in stocks
            ),
        )
        return SlackFileUpload(
            path=path,
            title="SwingEngine NSE fundamental stocks",
            initial_comment=(
                "Fundamentally decent NSE stocks exported by SwingEngine."
            ),
        )

    def _write_csv(
        self,
        filename: str,
        headings: Sequence[str],
        rows: Iterable[Sequence[Any]],
    ) -> Path:
        target = self._output_directory / filename
        temporary_path: Path | None = None
        LOGGER.info(
            "Starting CSV export filename=%r output_directory=%s target=%s",
            filename,
            self._output_directory,
            target,
        )
        try:
            self._output_directory.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                prefix=f".{filename}.",
                suffix=".tmp",
                dir=self._output_directory,
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                writer = csv.writer(temporary_file, lineterminator="\n")
                writer.writerow(headings)
                writer.writerows(
                    tuple(_safe_csv_cell(value) for value in row)
                    for row in rows
                )
            os.replace(temporary_path, target)
        except (csv.Error, OSError) as error:
            LOGGER.exception(
                "Unable to generate CSV export filename=%r "
                "output_directory=%s target=%s temporary_path=%s",
                filename,
                self._output_directory,
                target,
                temporary_path,
            )
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    LOGGER.exception(
                        "Unable to remove failed CSV export temporary file path=%s",
                        temporary_path,
                    )
            raise FileExportError(
                f"Unable to generate {filename}."
            ) from error
        LOGGER.info(
            "Completed CSV export filename=%r target=%s",
            filename,
            target,
        )
        return target


def _safe_csv_cell(value: Any) -> Any:
    """Prevent spreadsheet programs from interpreting text as formulas."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{value}"
    return value
