"""export_parquet.py — export the gold star from DuckDB to Parquet for the Databricks handoff.

The local pipeline (run_local.py / transform_gold.py) builds the gold star in DuckDB. Databricks
does NOT run the transform; it loads these Parquet files (databricks/01_load_legislation.py). This
script is the producer for warehouse/exports/ that the notebook consumes.

Usage:
    python warehouse/export_parquet.py            # -> warehouse/exports/<table>.parquet
"""

import argparse
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).parent.parent
DB_PATH = REPO_ROOT / "warehouse" / "db" / "legislation.duckdb"
EXPORT_DIR = REPO_ROOT / "warehouse" / "exports"

GOLD_TABLES = [
    "dim_person", "dim_committee", "dim_matter", "dim_subject", "dim_document",
    "dim_meeting", "dim_action_type",
    "fact_matter_action", "fact_vote", "fact_committee_membership",
    "bridge_matter_subject", "bridge_matter_sponsor", "bridge_matter_document",
    "bridge_meeting_document",
]


def export(con: duckdb.DuckDBPyConnection, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for t in GOLD_TABLES:
        path = out_dir / f"{t}.parquet"
        con.execute(f"COPY {t} TO '{path.as_posix()}' (FORMAT PARQUET)")
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<28} {n:>6} rows -> {path.name}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Export gold tables to Parquet for Databricks")
    ap.add_argument("--db", default=str(DB_PATH), help=f"DuckDB file (default: {DB_PATH})")
    ap.add_argument("--out", default=str(EXPORT_DIR), help=f"export dir (default: {EXPORT_DIR})")
    args = ap.parse_args()
    con = duckdb.connect(args.db, read_only=True)
    try:
        print(f"Exporting gold -> {args.out}")
        export(con, Path(args.out))
    finally:
        con.close()


if __name__ == "__main__":
    main()
