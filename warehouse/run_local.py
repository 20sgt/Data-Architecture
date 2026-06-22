"""run_local.py — one command for the full local pipeline: raw -> staging -> unified gold.

Loads whichever raw partitions you point it at (either slice or both), then builds the shared gold
star with warehouse/transform_gold.py. Idempotent: safe to re-run; the gold is fully rebuilt each time.

Typical use (after scraping):
    python -m scrape.legistar_meetings --current-month --raw-dir raw/meetings --date 2026-06-21
    python scrape/legistar_scrape.py --from 2026-06-01 --to 2026-06-21 --raw-dir raw/matters/ingest_date=2026-06-21
    python warehouse/run_local.py \\
        --meeting-raw raw/meetings/ingest_date=2026-06-21 \\
        --matters-raw raw/matters/ingest_date=2026-06-21 \\
        --date 2026-06-21
"""

import argparse
from datetime import date
from pathlib import Path

import duckdb

from warehouse.load_meeting_staging import load_partition as load_meetings
from warehouse.load_staging import load_partition as load_matters
from warehouse.transform_gold import DB_PATH, build, ensure_schema


def main():
    ap = argparse.ArgumentParser(description="Run the full local pipeline (raw -> staging -> gold)")
    ap.add_argument("--meeting-raw", help="meeting bronze partition dir (one JSON per meeting)")
    ap.add_argument("--matters-raw", help="legislation bronze partition dir (one JSON per matter)")
    ap.add_argument("--date", dest="ingest_date", default=date.today().isoformat(),
                    help="ingest partition date YYYY-MM-DD (default: today)")
    ap.add_argument("--db", default=str(DB_PATH), help=f"DuckDB file (default: {DB_PATH})")
    args = ap.parse_args()
    if not (args.meeting_raw or args.matters_raw):
        ap.error("provide --meeting-raw and/or --matters-raw")

    ingest = date.fromisoformat(args.ingest_date)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(args.db)
    try:
        ensure_schema(con)
        if args.meeting_raw:
            print(f"\n[load] meetings <- {args.meeting_raw}")
            load_meetings(con, Path(args.meeting_raw), ingest)
        if args.matters_raw:
            print(f"\n[load] matters  <- {args.matters_raw}")
            load_matters(con, Path(args.matters_raw), ingest)
        print()
        build(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
