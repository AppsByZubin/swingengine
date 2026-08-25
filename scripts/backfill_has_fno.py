"""One-off backfill for public.assets.has_fno / public.tracker.has_fno.

Every asset saved before the F&O flag was actually persisted (see the
`add_asset` fix in database/repository.py) has has_fno stuck at the column
default of FALSE. This recomputes the flag for existing rows from the
already-refreshed local NSE instrument catalog and corrects both tables.

Run `/swingengine instrument refresh` (or otherwise populate
UPSTOX_ASSET_FILE) before running this script - it does not download the
catalog itself.

Usage:
    python3 scripts/backfill_has_fno.py [--dry-run]
"""

import argparse
import logging

import psycopg

from database.config import DatabaseSettings
from upstox.assets import AssetCatalog, AssetCatalogError, AssetCatalogSettings

LOGGER = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the rows that would change without updating the database.",
    )
    args = parser.parse_args()

    catalog = AssetCatalog(AssetCatalogSettings.from_env())
    try:
        fno_isins = catalog.fno_isins()
    except AssetCatalogError as error:
        raise SystemExit(f"Unable to read the NSE instrument catalog: {error}")
    print(f"Loaded {len(fno_isins):,} F&O-eligible underlying ISINs.")

    database_settings = DatabaseSettings.from_env()
    with psycopg.connect(
        database_settings.database_url,
        connect_timeout=database_settings.connect_timeout_seconds,
    ) as connection:
        rows = connection.execute(
            """
            SELECT asset_id, trading_symbol, instrument_key, has_fno
            FROM public.assets
            ORDER BY trading_symbol
            """
        ).fetchall()

        changed = 0
        for asset_id, trading_symbol, instrument_key, current_has_fno in rows:
            isin = (instrument_key or "").rpartition("|")[2].strip().upper()
            correct_has_fno = isin in fno_isins
            if correct_has_fno == current_has_fno:
                continue

            changed += 1
            print(
                f"{trading_symbol}: has_fno {current_has_fno} -> "
                f"{correct_has_fno}"
            )
            if not args.dry_run:
                connection.execute(
                    "UPDATE public.assets SET has_fno = %s WHERE asset_id = %s",
                    (correct_has_fno, asset_id),
                )
                connection.execute(
                    "UPDATE public.tracker SET has_fno = %s WHERE asset_id = %s",
                    (correct_has_fno, asset_id),
                )

        if not args.dry_run:
            connection.commit()

    verb = "Would update" if args.dry_run else "Updated"
    print(f"{verb} {changed:,} of {len(rows):,} saved asset(s).")


if __name__ == "__main__":
    main()
