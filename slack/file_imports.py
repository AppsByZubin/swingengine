"""CSV asset imports initiated from Slack."""

import csv
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Protocol
from urllib.parse import urlparse

import requests
from slack_sdk.errors import SlackApiError, SlackClientError

from database.repository import (
    AssetAlreadyExistsError,
    AssetInUseError,
    AssetNotFoundError,
    AssetRecord,
    RepositoryError,
)
from upstox.assets import AssetCatalogError, AssetSearchResult

LOGGER = logging.getLogger(__name__)

MAX_ASSET_IMPORT_BYTES = 1_000_000
MAX_ASSET_IMPORT_ROWS = 1_000
MAX_REPORTED_ISSUES = 20
DOWNLOAD_TIMEOUT_SECONDS = 30
REQUIRED_COLUMNS = frozenset({"name", "action"})


class AssetImportError(RuntimeError):
    """Raised when an uploaded asset CSV cannot be imported."""


class AssetCatalogService(Protocol):
    def search(self, query: str) -> list[AssetSearchResult]:
        """Find NSE instruments related to a trading symbol."""


class AssetRepository(Protocol):
    def add_asset(self, asset: AssetSearchResult) -> AssetRecord:
        """Save an asset selected from the NSE catalog."""

    def delete_asset(self, trading_symbol: str) -> AssetRecord:
        """Delete and return a saved asset."""


@dataclass(frozen=True, slots=True)
class AssetImportSummary:
    """Counts and row-level issues from an asset CSV import."""

    total: int
    added: int
    deleted: int
    already_present: int
    failed: int
    issues: tuple[str, ...]

    def slack_message(self) -> str:
        icon = ":white_check_mark:" if self.failed == 0 else ":warning:"
        lines = [
            f"{icon} Asset CSV import completed.",
            f"• Rows: {self.total}",
            f"• Added: {self.added}",
            f"• Deleted: {self.deleted}",
            f"• Already present: {self.already_present}",
            f"• Failed: {self.failed}",
        ]
        if self.issues:
            lines.extend(("", "*Issues*", *(f"• {issue}" for issue in self.issues)))
            hidden_count = self.failed - len(self.issues)
            if hidden_count > 0:
                lines.append(f"• …and {hidden_count} more issue(s).")
        return "\n".join(lines)


HttpGet = Callable[..., Any]


class CsvAssetImporter:
    """Download, validate, and apply an uploaded asset action CSV."""

    def __init__(
        self,
        input_directory: str | Path,
        asset_service: AssetCatalogService,
        repository: AssetRepository,
        bot_token: str,
        http_get: HttpGet = requests.get,
    ):
        self._input_directory = Path(input_directory)
        self._asset_service = asset_service
        self._repository = repository
        self._bot_token = bot_token
        self._http_get = http_get

    def import_slack_file(
        self,
        uploaded_file: Mapping[str, Any],
        client: Any,
    ) -> AssetImportSummary:
        """Resolve and download one Slack file before importing its rows."""
        file_info = self._resolve_file_info(uploaded_file, client)
        file_id = str(file_info.get("id", "")).strip()
        filename = str(file_info.get("name", "")).strip()
        download_url = str(
            file_info.get("url_private_download")
            or file_info.get("url_private")
            or ""
        ).strip()

        if not file_id or not filename or not download_url:
            raise AssetImportError("Slack did not provide complete file details.")
        if Path(filename).suffix.casefold() != ".csv":
            raise AssetImportError("Upload a file with a `.csv` extension.")

        declared_size = _nonnegative_int(file_info.get("size"))
        if declared_size is not None and declared_size > MAX_ASSET_IMPORT_BYTES:
            raise AssetImportError(
                "The CSV exceeds the 1 MB asset import limit."
            )
        _validate_slack_download_url(download_url)

        path = self._download(
            file_id=file_id,
            download_url=download_url,
        )
        return self.import_path(path)

    def import_path(self, path: str | Path) -> AssetImportSummary:
        """Validate and apply rows from a local CSV file."""
        csv_path = Path(path)
        added = 0
        deleted = 0
        already_present = 0
        failed = 0
        issues: list[str] = []
        total = 0

        try:
            with csv_path.open(
                mode="r",
                encoding="utf-8-sig",
                newline="",
            ) as csv_file:
                reader = csv.DictReader(csv_file, strict=True)
                columns = _normalized_columns(reader.fieldnames)
                if (
                    len(columns) != len(REQUIRED_COLUMNS)
                    or frozenset(columns) != REQUIRED_COLUMNS
                ):
                    raise AssetImportError(
                        "The CSV header must contain exactly `name,action`."
                    )

                column_names = {
                    str(name).strip().casefold(): str(name)
                    for name in reader.fieldnames or ()
                }
                rows = list(reader)
            total = len(rows)
            if total == 0:
                raise AssetImportError("The CSV contains no asset rows.")
            if total > MAX_ASSET_IMPORT_ROWS:
                raise AssetImportError(
                    "The CSV exceeds the 1,000-row asset import limit."
                )

            for row_number, row in enumerate(rows, start=2):
                if None in row:
                    failed += 1
                    _append_issue(
                        issues,
                        f"Row {row_number}: contains unexpected columns.",
                    )
                    continue
                symbol = str(
                    row.get(column_names["name"]) or ""
                ).strip().upper()
                action = str(
                    row.get(column_names["action"]) or ""
                ).strip().casefold()
                if not symbol:
                    failed += 1
                    _append_issue(
                        issues,
                        f"Row {row_number}: trading symbol is empty.",
                    )
                    continue
                if action not in {"add", "delete"}:
                    failed += 1
                    _append_issue(
                        issues,
                        f"Row {row_number} `{_safe_text(symbol)}`: action "
                        "must be `add` or `delete`.",
                    )
                    continue

                outcome, issue = self._apply_row(
                    row_number,
                    symbol,
                    action,
                )
                if outcome == "added":
                    added += 1
                elif outcome == "deleted":
                    deleted += 1
                elif outcome == "already_present":
                    already_present += 1
                else:
                    failed += 1
                    if issue:
                        _append_issue(issues, issue)
        except AssetImportError:
            raise
        except UnicodeDecodeError as error:
            raise AssetImportError("The CSV must be UTF-8 encoded.") from error
        except (csv.Error, OSError) as error:
            LOGGER.exception("Unable to read asset import CSV path=%s", csv_path)
            raise AssetImportError("Unable to read the uploaded CSV.") from error

        return AssetImportSummary(
            total=total,
            added=added,
            deleted=deleted,
            already_present=already_present,
            failed=failed,
            issues=tuple(issues),
        )

    def _apply_row(
        self,
        row_number: int,
        symbol: str,
        action: str,
    ) -> tuple[str, str]:
        if action == "delete":
            try:
                self._repository.delete_asset(symbol)
            except AssetNotFoundError:
                return (
                    "failed",
                    f"Row {row_number} `{_safe_text(symbol)}`: asset is not "
                    "present.",
                )
            except AssetInUseError:
                return (
                    "failed",
                    f"Row {row_number} `{_safe_text(symbol)}`: asset is still "
                    "tracked; delete its tracker entry first.",
                )
            except RepositoryError as error:
                return (
                    "failed",
                    f"Row {row_number} `{_safe_text(symbol)}`: {error}",
                )
            return "deleted", ""

        try:
            matches = self._asset_service.search(symbol)
        except AssetCatalogError as error:
            return (
                "failed",
                f"Row {row_number} `{_safe_text(symbol)}`: {error}",
            )
        asset = next(
            (
                match
                for match in matches
                if match.trading_symbol.casefold() == symbol.casefold()
            ),
            None,
        )
        if asset is None:
            return (
                "failed",
                f"Row {row_number} `{_safe_text(symbol)}`: no exact NSE "
                "trading symbol found.",
            )

        try:
            self._repository.add_asset(asset)
        except AssetAlreadyExistsError:
            return "already_present", ""
        except RepositoryError as error:
            return (
                "failed",
                f"Row {row_number} `{_safe_text(symbol)}`: {error}",
            )
        return "added", ""

    def _resolve_file_info(
        self,
        uploaded_file: Mapping[str, Any],
        client: Any,
    ) -> Mapping[str, Any]:
        if (
            uploaded_file.get("url_private_download")
            or uploaded_file.get("url_private")
        ):
            return uploaded_file

        file_id = str(uploaded_file.get("id", "")).strip()
        if not file_id:
            raise AssetImportError("Slack did not provide an uploaded file ID.")
        try:
            response = client.files_info(file=file_id)
        except (SlackApiError, SlackClientError) as error:
            LOGGER.exception("Unable to resolve Slack file file_id=%r", file_id)
            raise AssetImportError(
                "Unable to retrieve the uploaded file from Slack."
            ) from error
        file_info = response.get("file")
        if not isinstance(file_info, Mapping):
            raise AssetImportError("Slack did not provide complete file details.")
        return file_info

    def _download(self, file_id: str, download_url: str) -> Path:
        safe_file_id = "".join(
            character
            for character in file_id
            if character.isascii() and (character.isalnum() or character in "_-")
        )
        if not safe_file_id:
            safe_file_id = "uploaded"
        target = self._input_directory / f"asset-import-{safe_file_id}.csv"
        temporary_path: Path | None = None

        try:
            self._input_directory.mkdir(parents=True, exist_ok=True)
            response = self._http_get(
                download_url,
                headers={"Authorization": f"Bearer {self._bot_token}"},
                stream=True,
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
            )
            try:
                response.raise_for_status()
                with NamedTemporaryFile(
                    mode="wb",
                    prefix=".asset-import-",
                    suffix=".csv.tmp",
                    dir=self._input_directory,
                    delete=False,
                ) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                    byte_count = 0
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        byte_count += len(chunk)
                        if byte_count > MAX_ASSET_IMPORT_BYTES:
                            raise AssetImportError(
                                "The CSV exceeds the 1 MB asset import limit."
                            )
                        temporary_file.write(chunk)
                    temporary_file.flush()
                    os.fsync(temporary_file.fileno())
                os.replace(temporary_path, target)
                temporary_path = None
            finally:
                response.close()
        except AssetImportError:
            raise
        except (OSError, requests.RequestException) as error:
            LOGGER.exception(
                "Unable to download Slack asset CSV file_id=%r target=%s",
                file_id,
                target,
            )
            raise AssetImportError(
                "Unable to download the uploaded CSV from Slack."
            ) from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    LOGGER.exception(
                        "Unable to remove asset import temporary file path=%s",
                        temporary_path,
                    )
        return target


def _normalized_columns(fieldnames: list[str] | None) -> tuple[str, ...]:
    if fieldnames is None or any(name is None for name in fieldnames):
        return ()
    return tuple(str(name).strip().casefold() for name in fieldnames)


def _nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _validate_slack_download_url(url: str) -> None:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or not hostname
        or not (hostname == "slack.com" or hostname.endswith(".slack.com"))
    ):
        raise AssetImportError("Slack provided an invalid file download URL.")


def _append_issue(issues: list[str], issue: str) -> None:
    if len(issues) < MAX_REPORTED_ISSUES:
        issues.append(issue)


def _safe_text(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("`", "'")
    )
