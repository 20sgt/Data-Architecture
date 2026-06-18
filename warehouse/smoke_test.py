"""smoke_test.py — validate DDL against the 5 spike sample JSONs.

Creates both DDL files in an in-memory DuckDB, loads the samples into
staging tables, then runs two queries:
  1. Row counts against our pre-run predictions (5 matters, 11 actions, 9 votes).
  2. The known roll call for file 260439 (Chan/Dorsey/Sauter Aye) to verify
     the join between stg_votes and stg_matters is correct.

Not a pipeline — just a schema sanity check.
"""

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

import duckdb

REPO_ROOT  = Path(__file__).parent.parent          # Data-Architecture/
SAMPLES_DIR = REPO_ROOT.parent / "spike" / "data" / "samples"
DDL_DIR     = REPO_ROOT / "warehouse" / "ddl"

EXPECTED = {"stg_matters": 5, "stg_actions": 11, "stg_votes": 9}


def extract_id(url: str | None) -> int | None:
    m = re.search(r"[?&]ID=(\d+)", url or "")
    return int(m.group(1)) if m else None


def load_samples(con: duckdb.DuckDBPyConnection) -> None:
    index = json.loads((SAMPLES_DIR / "_index.json").read_text())
    ingest_date = date.today()
    scraped_at  = datetime.now()

    for entry in index["samples"]:
        raw = json.loads((SAMPLES_DIR / entry["file"]).read_text())
        matter_id = extract_id(raw["detail_url"])

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

        for seq, action in enumerate(raw["actions"]):
            con.execute("""
                INSERT INTO stg_actions (matter_id, action_seq, action_date_raw, body,
                    action, result, history_url, ingest_date, scraped_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, [matter_id, seq, action["date"], action["body"], action["action"],
                  action["result"], action["history_url"], ingest_date, scraped_at])

            for vote in action["votes"]:
                con.execute("""
                    INSERT INTO stg_votes (matter_id, action_seq, person_name,
                        vote_value, ingest_date, scraped_at)
                    VALUES (?,?,?,?,?,?)
                """, [matter_id, seq, vote["person"], vote["value"],
                      ingest_date, scraped_at])

        for seq, att in enumerate(raw["attachments"]):
            con.execute("""
                INSERT INTO stg_attachments (matter_id, attachment_seq, attachment_name,
                    attachment_url, document_id, ingest_date, scraped_at)
                VALUES (?,?,?,?,?,?,?)
            """, [matter_id, seq, att["name"], att["url"],
                  extract_id(att["url"]), ingest_date, scraped_at])

        for pos, name in enumerate(raw["sponsors"]):
            con.execute("""
                INSERT INTO stg_sponsors (matter_id, sponsor_pos, sponsor_name,
                    ingest_date, scraped_at)
                VALUES (?,?,?,?,?)
            """, [matter_id, pos, name, ingest_date, scraped_at])


def check_counts(con: duckdb.DuckDBPyConnection) -> bool:
    print("\n── Row count predictions ─────────────────────────────────")
    ok = True
    for table, expected in EXPECTED.items():
        actual = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        status = "✓" if actual == expected else "✗"
        print(f"  {status}  {table:<20} expected {expected:>3}  got {actual:>3}")
        if actual != expected:
            ok = False
    return ok


def check_rollcall(con: duckdb.DuckDBPyConnection) -> bool:
    print("\n── Roll call for 260439 (expected: Chan/Dorsey/Sauter Aye) ──")
    rows = con.execute("""
        SELECT sv.person_name, sv.vote_value
        FROM   stg_votes    sv
        JOIN   stg_matters  sm ON sm.matter_id  = sv.matter_id
        JOIN   stg_actions  sa ON sa.matter_id  = sv.matter_id
                               AND sa.action_seq = sv.action_seq
        WHERE  sm.matter_file = '260439'
        ORDER  BY sv.person_name
    """).fetchall()

    known = {("Connie Chan", "Aye"), ("Matt Dorsey", "Aye"), ("Danny Sauter", "Aye")}
    actual = set(rows)
    ok = actual == known
    for row in rows:
        status = "✓" if row in known else "✗"
        print(f"  {status}  {row[0]:<20} {row[1]}")
    if not ok:
        print(f"  ✗  mismatch — expected {known}")
    return ok


def main() -> None:
    con = duckdb.connect()

    con.execute((DDL_DIR / "01_staging.sql").read_text())
    con.execute((DDL_DIR / "02_star.sql").read_text())
    print("DDL executed — all tables created.")

    load_samples(con)
    print(f"Loaded {SAMPLES_DIR} into staging.")

    counts_ok   = check_counts(con)
    rollcall_ok = check_rollcall(con)

    print()
    if counts_ok and rollcall_ok:
        print("All checks passed.")
    else:
        print("One or more checks failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
