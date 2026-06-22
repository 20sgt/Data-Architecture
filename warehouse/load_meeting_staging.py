"""load_meeting_staging.py — load a directory of per-meeting JSON files into staging.

Idempotent: deletes all staging rows for the given ingest_date before inserting, so
re-running a partition (a retry, or a re-scrape once Draft minutes go Final) is safe.

Expected source layout (one JSON file per meeting, as written by legistar_meetings.py):
    raw/meetings/ingest_date=YYYY-MM-DD/<EventId>.json

Usage:
    python warehouse/load_meeting_staging.py --src raw/meetings/ingest_date=2026-06-21 \\
        --date 2026-06-21
"""

import argparse
import json
from datetime import date, datetime
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).parent.parent
DB_PATH = REPO_ROOT / "warehouse" / "db" / "legislation.duckdb"
DDL_DIR = REPO_ROOT / "warehouse" / "ddl"

STG_TABLES = ("stg_meetings", "stg_meeting_agenda_items",
              "stg_meeting_votes", "stg_meeting_documents")


def _int(x):
    """Coerce a numeric-string/None id to int (staging ids are BIGINT)."""
    if x is None or x == "":
        return None
    return int(x)


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create the meeting staging + shared gold tables if absent. Safe on every run."""
    con.execute((DDL_DIR / "03_meeting_staging.sql").read_text())
    con.execute((DDL_DIR / "02_star.sql").read_text())


def delete_partition(con: duckdb.DuckDBPyConnection, ingest_date: date) -> None:
    for table in STG_TABLES:
        n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE ingest_date = ?",
                        [ingest_date]).fetchone()[0]
        con.execute(f"DELETE FROM {table} WHERE ingest_date = ?", [ingest_date])
        if n:
            print(f"  [idempotent] removed {n} existing rows from {table}")


def _insert_meeting(con: duckdb.DuckDBPyConnection, raw: dict, ingest_date: date) -> dict:
    meeting_id = _int(raw["meeting_id"])
    scraped_at = datetime.now()

    con.execute("""
        INSERT INTO stg_meetings (meeting_id, event_guid, body_name, meeting_date_raw,
            meeting_time_raw, location, meeting_subtype, agenda_status, minutes_status,
            agenda_url, minutes_url, video_clip_id, ingest_date, scraped_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [meeting_id, raw["event_guid"], raw["body_name"], raw["meeting_date"],
          raw["meeting_time"], raw["location"], raw["meeting_subtype"],
          raw["agenda_status"], raw["minutes_status"], raw["agenda_url"],
          raw["minutes_url"], raw["video_clip_id"], ingest_date, scraped_at])

    items_n = votes_n = docs_n = 0

    for item in raw["agenda_items"]:
        con.execute("""
            INSERT INTO stg_meeting_agenda_items (meeting_id, item_seq, matter_file,
                agenda_number, matter_name, matter_type, matter_status, title, action_raw,
                action_result, history_id, history_url, action_text, ingest_date, scraped_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [meeting_id, item["item_seq"], item["matter_file"], item["agenda_number"],
              item["matter_name"], item["matter_type"], item["matter_status"], item["title"],
              item["action_raw"], item["action_result"], _int(item["history_id"]),
              item["history_url"], item["action_text"], ingest_date, scraped_at])
        items_n += 1

        for vote in item["votes"]:
            con.execute("""
                INSERT INTO stg_meeting_votes (meeting_id, item_seq, person_id, person_name,
                    vote_value_raw, ingest_date, scraped_at)
                VALUES (?,?,?,?,?,?,?)
            """, [meeting_id, item["item_seq"], _int(vote["person_id"]),
                  vote["person_name"], vote["vote_value"], ingest_date, scraped_at])
            votes_n += 1

    for seq, doc in enumerate(raw["documents"]):
        con.execute("""
            INSERT INTO stg_meeting_documents (meeting_id, doc_seq, document_source,
                document_title, document_url, body_text, ingest_date, scraped_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, [meeting_id, seq, doc["document_source"], doc["document_title"],
              doc["document_url"], doc.get("body_text"), ingest_date, scraped_at])
        docs_n += 1

    return {"meetings": 1, "agenda_items": items_n, "votes": votes_n, "documents": docs_n}


def load_partition(con: duckdb.DuckDBPyConnection, src_dir: Path, ingest_date: date) -> None:
    files = sorted(p for p in src_dir.glob("*.json") if p.stem != "_index")
    if not files:
        raise FileNotFoundError(f"No meeting JSON files in {src_dir}")

    con.execute("BEGIN TRANSACTION")          # delete+insert atomically: a mid-load crash
    try:                                      # leaves the prior partition intact, not half-loaded
        delete_partition(con, ingest_date)
        totals: dict[str, int] = {}
        for path in files:
            raw = json.loads(path.read_text())
            if "meeting_id" not in raw:
                continue
            for k, v in _insert_meeting(con, raw, ingest_date).items():
                totals[k] = totals.get(k, 0) + v
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    print(f"Loaded {len(files)} files -> ingest_date={ingest_date}")
    width = max(len(k) for k in totals)
    for k, v in totals.items():
        print(f"  stg_meeting_{k:<{width}}  {v:>4} rows")


def main() -> None:
    ap = argparse.ArgumentParser(description="Load meeting JSON files into DuckDB staging")
    ap.add_argument("--src", required=True, help="directory of per-meeting JSON files")
    ap.add_argument("--date", dest="ingest_date", default=date.today().isoformat(),
                    help="partition date YYYY-MM-DD (default: today)")
    ap.add_argument("--db", default=str(DB_PATH), help=f"DuckDB file (default: {DB_PATH})")
    args = ap.parse_args()

    ingest_date = date.fromisoformat(args.ingest_date)
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(db_path))
    try:
        ensure_schema(con)
        load_partition(con, Path(args.src), ingest_date)
    finally:
        con.close()


if __name__ == "__main__":
    main()
