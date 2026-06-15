"""load_staging.py — load a directory of per-matter JSON files into staging tables.

Idempotent: deletes all staging rows for the given ingest_date before inserting,
so re-running the same partition date is always safe (retry-friendly).

Expected source layout (one JSON file per matter):
    raw/matters/ingest_date=YYYY-MM-DD/<file_number>.json

The spike samples (spike/data/samples/) share this layout and can be used
directly for local testing.

Usage:
    # test against spike samples
    python warehouse/load_staging.py \\
        --src spike/data/samples \\
        --date 2026-05-31

    # load a real weekly scrape partition
    python warehouse/load_staging.py \\
        --src raw/matters/ingest_date=2026-06-15 \\
        --date 2026-06-15
"""

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).parent.parent
DB_PATH   = REPO_ROOT / "warehouse" / "db" / "legislation.duckdb"
DDL_DIR   = REPO_ROOT / "warehouse" / "ddl"


def extract_id(url: str | None) -> int | None:
    m = re.search(r"[?&]ID=(\d+)", url or "")
    return int(m.group(1)) if m else None


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create tables if they don't exist. Safe to call on every run."""
    con.execute((DDL_DIR / "01_staging.sql").read_text())
    con.execute((DDL_DIR / "02_star.sql").read_text())


def delete_partition(con: duckdb.DuckDBPyConnection, ingest_date: date) -> None:
    """Remove all staging rows for this date so re-runs are idempotent."""
    for table in ("stg_matters", "stg_actions", "stg_votes",
                  "stg_attachments", "stg_sponsors"):
        n = con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE ingest_date = ?", [ingest_date]
        ).fetchone()[0]
        con.execute(f"DELETE FROM {table} WHERE ingest_date = ?", [ingest_date])
        # Log if rows were actually removed (signals a retry, not a fresh run).
        if n:
            print(f"  [idempotent] removed {n} existing rows from {table}")


def _insert_matter(con: duckdb.DuckDBPyConnection, raw: dict, ingest_date: date) -> dict:
    """Insert one matter and its nested collections. Returns per-table row counts."""
    matter_id  = extract_id(raw["detail_url"])
    scraped_at = datetime.now()

    con.execute("""
        INSERT INTO stg_matters (matter_id, matter_file, detail_url, name, title,
            matter_type, status, lifecycle, introduced_raw, on_agenda_raw,
            final_action_raw, enactment_date_raw, enactment_number, in_control,
            full_text, ingest_date, scraped_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [matter_id, raw["file_number"], raw["detail_url"], raw["name"], raw["title"],
          raw["type"], raw["status"], raw.get("lifecycle"), raw["introduced"],
          raw["on_agenda"], raw["final_action"], raw["enactment_date"],
          raw["enactment_number"], raw["in_control"], raw.get("full_text"),
          ingest_date, scraped_at])

    actions_n = votes_n = attachments_n = sponsors_n = 0

    for seq, action in enumerate(raw["actions"]):
        con.execute("""
            INSERT INTO stg_actions (matter_id, action_seq, action_date_raw, body,
                action, result, history_url, ingest_date, scraped_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, [matter_id, seq, action["date"], action["body"], action["action"],
              action["result"], action["history_url"], ingest_date, scraped_at])
        actions_n += 1

        for vote in action["votes"]:
            con.execute("""
                INSERT INTO stg_votes (matter_id, action_seq, person_name,
                    vote_value, ingest_date, scraped_at)
                VALUES (?,?,?,?,?,?)
            """, [matter_id, seq, vote["person"], vote["value"], ingest_date, scraped_at])
            votes_n += 1

    for seq, att in enumerate(raw["attachments"]):
        con.execute("""
            INSERT INTO stg_attachments (matter_id, attachment_seq, attachment_name,
                attachment_url, document_id, ingest_date, scraped_at)
            VALUES (?,?,?,?,?,?,?)
        """, [matter_id, seq, att["name"], att["url"],
              extract_id(att["url"]), ingest_date, scraped_at])
        attachments_n += 1

    for pos, name in enumerate(raw["sponsors"]):
        con.execute("""
            INSERT INTO stg_sponsors (matter_id, sponsor_pos, sponsor_name,
                ingest_date, scraped_at)
            VALUES (?,?,?,?,?)
        """, [matter_id, pos, name, ingest_date, scraped_at])
        sponsors_n += 1

    return {"matters": 1, "actions": actions_n, "votes": votes_n,
            "attachments": attachments_n, "sponsors": sponsors_n}


def load_partition(con: duckdb.DuckDBPyConnection, src_dir: Path, ingest_date: date) -> None:
    """Delete then re-insert all staging rows for ingest_date from src_dir/*.json."""
    files = sorted(p for p in src_dir.glob("*.json") if p.stem != "_index")
    if not files:
        raise FileNotFoundError(f"No matter JSON files in {src_dir}")

    delete_partition(con, ingest_date)

    totals: dict[str, int] = {}
    for path in files:
        raw = json.loads(path.read_text())
        if "file_number" not in raw:
            continue
        for k, v in _insert_matter(con, raw, ingest_date).items():
            totals[k] = totals.get(k, 0) + v

    print(f"Loaded {len(files)} files → ingest_date={ingest_date}")
    width = max(len(k) for k in totals)
    for k, v in totals.items():
        print(f"  stg_{k:<{width}}  {v:>4} rows")


def main() -> None:
    ap = argparse.ArgumentParser(description="Load matter JSON files into DuckDB staging")
    ap.add_argument("--src",  required=True,
                    help="directory of per-matter JSON files")
    ap.add_argument("--date", dest="ingest_date",
                    default=date.today().isoformat(),
                    help="partition date YYYY-MM-DD (default: today)")
    ap.add_argument("--db",   default=str(DB_PATH),
                    help=f"DuckDB file path (default: {DB_PATH})")
    args = ap.parse_args()

    src_dir     = Path(args.src)
    ingest_date = date.fromisoformat(args.ingest_date)
    db_path     = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(db_path))
    try:
        ensure_schema(con)
        load_partition(con, src_dir, ingest_date)
    finally:
        con.close()


if __name__ == "__main__":
    main()
