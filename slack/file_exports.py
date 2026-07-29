"""Local file storage and CSV exports for Slack commands."""

import csv
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from database.repository import AssetRecord, TrackerEntry

DEFAULT_FILES_DIRECTORY = Path(__file__).resolve().parent.parent / "files"


class FileStorageError(RuntimeError):
    """Raised when SwingEngine cannot initialize its local file storage."""


class FileExportError(RuntimeError):
    """Raised when SwingEngine cannot generate a requested export."""


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
        except OSError as error:
            raise FileStorageError(
                f"Unable to initialize file directories under {root_path}."
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
                "tracker_details_id",
                "asset_id",
                "asset_name",
                "trading_symbol",
                "added_date",
            ),
            (
                (
                    entry.tracker_details_id,
                    entry.asset_id,
                    entry.asset_name,
                    entry.trading_symbol,
                    entry.added_date.isoformat(),
                )
                for entry in entries
            ),
        )
        return SlackFileUpload(
            path=path,
            title="SwingEngine tracker",
            initial_comment="Tracked assets exported by SwingEngine.",
        )

    def _write_csv(
        self,
        filename: str,
        headings: Sequence[str],
        rows: Iterable[Sequence[Any]],
    ) -> Path:
        target = self._output_directory / filename
        temporary_path: Path | None = None
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
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise FileExportError(
                f"Unable to generate {filename}."
            ) from error
        return target


def _safe_csv_cell(value: Any) -> Any:
    """Prevent spreadsheet programs from interpreting text as formulas."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{value}"
    return value
