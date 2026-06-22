"""transform_star.py — transform staging tables into the star schema.

Run after load_staging.py has populated the staging tables for a given date.

Dims before facts (FK dependency order):
  dim_committee → dim_person → dim_matter → facts/bridges

meeting_sk is left NULL on all facts — the cross-slice meeting join is a
separate step once the teammate's calendar data is loaded.

Idempotency: completed runs are recorded in pipeline_runs. Re-running the
same date is blocked unless the entry is manually removed.

Usage:
    python warehouse/transform_star.py --date 2026-05-31
    python warehouse/transform_star.py --seed-only   # seed dims without touching facts
"""

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).parent.parent
DB_PATH   = REPO_ROOT / "warehouse" / "db" / "legislation.duckdb"
SPIKE_DIR = REPO_ROOT.parent / "spike" / "data"


def parse_date(s: str | None) -> date | None:
    """Parse Legistar's M/D/YYYY date strings. Returns None on missing or malformed input."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError:
        return None


# ── schema setup ─────────────────────────────────────────────────────────────

def ensure_sequences(con: duckdb.DuckDBPyConnection) -> None:
    for seq in ("seq_committee_sk", "seq_person_sk", "seq_matter_sk",
                "seq_document_sk", "seq_vote_sk", "seq_matter_action_sk"):
        con.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq} START 1")
    con.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            ingest_date  DATE      NOT NULL PRIMARY KEY,
            completed_at TIMESTAMP NOT NULL
        )
    """)


# ── dimension seeds (idempotent: skip rows already in the dim) ────────────────

def seed_committees(con: duckdb.DuckDBPyConnection) -> None:
    """Seed dim_committee from bodies.json (authoritative Legistar BodyId → BodyName).

    bodies.json is stable reference data — committees rarely change. Safe to
    re-seed on every run; rows with an existing committee_id are skipped.
    """
    bodies = json.loads((SPIKE_DIR / "bodies.json").read_text())
    n = 0
    for b in bodies:
        existing = con.execute(
            "SELECT 1 FROM dim_committee WHERE committee_id = ?", [b["BodyId"]]
        ).fetchone()
        if not existing:
            con.execute("""
                INSERT INTO dim_committee
                    (committee_sk, committee_id, committee_name, committee_type, is_active)
                VALUES (nextval('seq_committee_sk'), ?, ?, ?, ?)
            """, [b["BodyId"], b["BodyName"], b.get("BodyTypeName"),
                  bool(b.get("BodyActiveFlag"))])
            n += 1
    print(f"  dim_committee      {n:>4} inserted")


def seed_persons(con: duckdb.DuckDBPyConnection) -> None:
    """Seed dim_person from two sources merged in priority order:

    1. persons.json — Legistar API dump. Frozen at 2020 but has real PersonIds
       for historical supervisors. Inserted first.
    2. Unique names from stg_votes + stg_sponsors — covers post-2020 supervisors
       missing from the API. Assigned synthetic person_ids (starting above the
       max PersonId from source 1, so the namespaces don't collide).

    SCD type 2 versioning (effective_from/to/is_current) is seeded here with
    approximate dates ('2020-01-01'); future district/term changes will add rows.
    """
    persons = json.loads((SPIKE_DIR / "persons.json").read_text())
    n_api = 0
    for p in persons:
        if con.execute("SELECT 1 FROM dim_person WHERE person_id = ?",
                       [p["PersonId"]]).fetchone():
            continue
        con.execute("""
            INSERT INTO dim_person
                (person_sk, person_id, full_name, effective_from, effective_to, is_current)
            VALUES (nextval('seq_person_sk'), ?, ?, '2020-01-01', NULL, true)
        """, [p["PersonId"], p["PersonFullName"]])
        n_api += 1

    # Any name from live vote/sponsor data not already in dim_person.
    # We assign synthetic IDs above the max existing person_id to avoid collisions.
    max_id = con.execute(
        "SELECT COALESCE(MAX(person_id), 1000) FROM dim_person"
    ).fetchone()[0]

    live_names = con.execute("""
        SELECT DISTINCT person_name FROM stg_votes
        UNION
        SELECT DISTINCT sponsor_name  FROM stg_sponsors
    """).fetchall()

    n_live = 0
    for (name,) in live_names:
        if con.execute("SELECT 1 FROM dim_person WHERE full_name = ? AND is_current = true",
                       [name]).fetchone():
            continue
        max_id += 1
        con.execute("""
            INSERT INTO dim_person
                (person_sk, person_id, full_name, effective_from, effective_to, is_current)
            VALUES (nextval('seq_person_sk'), ?, ?, '2020-01-01', NULL, true)
        """, [max_id, name])
        n_live += 1

    print(f"  dim_person         {n_api:>4} from persons.json"
          f", {n_live} from live vote/sponsor names")


# ── matter SCD type 2 ────────────────────────────────────────────────────────

def transform_matters(con: duckdb.DuckDBPyConnection, ingest_date: date) -> None:
    """Upsert dim_matter using SCD type 2 on status.

    - New matter (no current row):  insert with is_current=true.
    - Status changed:               close old row (effective_to, is_current=false),
                                    insert new row with updated status.
    - Status unchanged:             skip (no duplicate version created).

    Facts always join to the current matter row (is_current=true). The full
    version history in dim_matter answers "when did this bill change status."
    """
    rows = con.execute("""
        SELECT matter_id, matter_file, detail_url, name, title, matter_type,
               status, lifecycle, introduced_raw, final_action_raw,
               enactment_date_raw, enactment_number, in_control
        FROM stg_matters WHERE ingest_date = ?
    """, [ingest_date]).fetchall()

    new_n = versioned_n = skipped_n = 0
    for (matter_id, matter_file, detail_url, name, title, matter_type,
         status, lifecycle, introduced_raw, final_action_raw,
         enactment_date_raw, enactment_number, in_control) in rows:

        introduced   = parse_date(introduced_raw)
        final_action = parse_date(final_action_raw)
        enactment    = parse_date(enactment_date_raw)

        committee_row = con.execute(
            "SELECT committee_sk FROM dim_committee WHERE committee_name = ?",
            [in_control]
        ).fetchone()
        committee_sk = committee_row[0] if committee_row else None

        current = con.execute("""
            SELECT matter_sk, status FROM dim_matter
            WHERE matter_id = ? AND is_current = true
        """, [matter_id]).fetchone()

        if current is None:
            con.execute("""
                INSERT INTO dim_matter (matter_sk, matter_id, matter_file, matter_title,
                    matter_name, matter_type, introduction_date, controlling_committee_sk,
                    legistar_url, status, lifecycle, final_action_date, enactment_date,
                    enactment_number, effective_from, effective_to, is_current)
                VALUES (nextval('seq_matter_sk'),?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,true)
            """, [matter_id, matter_file, title, name, matter_type, introduced,
                  committee_sk, detail_url, status, lifecycle,
                  final_action, enactment, enactment_number, ingest_date])
            new_n += 1

        elif current[1] != status:
            # Close the stale row, open a new version.
            con.execute("""
                UPDATE dim_matter SET effective_to = ?, is_current = false
                WHERE matter_id = ? AND is_current = true
            """, [ingest_date - timedelta(days=1), matter_id])
            con.execute("""
                INSERT INTO dim_matter (matter_sk, matter_id, matter_file, matter_title,
                    matter_name, matter_type, introduction_date, controlling_committee_sk,
                    legistar_url, status, lifecycle, final_action_date, enactment_date,
                    enactment_number, effective_from, effective_to, is_current)
                VALUES (nextval('seq_matter_sk'),?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,true)
            """, [matter_id, matter_file, title, name, matter_type, introduced,
                  committee_sk, detail_url, status, lifecycle,
                  final_action, enactment, enactment_number, ingest_date])
            versioned_n += 1

        else:
            skipped_n += 1

    print(f"  dim_matter         {new_n:>4} new, {versioned_n} versioned, {skipped_n} unchanged")


# ── fact loads ───────────────────────────────────────────────────────────────

def transform_actions(con: duckdb.DuckDBPyConnection, ingest_date: date) -> None:
    rows = con.execute("""
        SELECT matter_id, action_date_raw, body, action, result
        FROM stg_actions WHERE ingest_date = ?
    """, [ingest_date]).fetchall()

    n = skipped = 0
    for (matter_id, action_date_raw, body, action, result) in rows:
        action_date  = parse_date(action_date_raw)
        matter_row   = con.execute(
            "SELECT matter_sk FROM dim_matter WHERE matter_id = ? AND is_current = true",
            [matter_id]
        ).fetchone()
        if not matter_row:
            skipped += 1
            continue

        # Idempotency guard: skip if (matter_sk, action_type_code, action_date) exists.
        if con.execute("""
            SELECT 1 FROM fact_matter_action
            WHERE matter_sk = ? AND action_type_code = ? AND action_date = ?
        """, [matter_row[0], action, action_date]).fetchone():
            skipped += 1
            continue

        con.execute("""
            INSERT INTO fact_matter_action
                (matter_action_sk, matter_sk, meeting_sk, action_type_code,
                 action_date, action_result)
            VALUES (nextval('seq_matter_action_sk'), ?, NULL, ?, ?, ?)
        """, [matter_row[0], action, action_date, result or None])
        n += 1

    print(f"  fact_matter_action {n:>4} inserted, {skipped} skipped")


def transform_votes(con: duckdb.DuckDBPyConnection, ingest_date: date) -> None:
    rows = con.execute("""
        SELECT sv.matter_id, sv.person_name, sv.vote_value, sa.action_date_raw
        FROM stg_votes sv
        JOIN stg_actions sa ON sa.matter_id  = sv.matter_id
                            AND sa.action_seq = sv.action_seq
                            AND sa.ingest_date = sv.ingest_date
        WHERE sv.ingest_date = ?
    """, [ingest_date]).fetchall()

    n = skipped = 0
    for (matter_id, person_name, vote_value, action_date_raw) in rows:
        vote_date  = parse_date(action_date_raw)
        matter_row = con.execute(
            "SELECT matter_sk FROM dim_matter WHERE matter_id = ? AND is_current = true",
            [matter_id]
        ).fetchone()
        person_row = con.execute(
            "SELECT person_sk FROM dim_person WHERE full_name = ? AND is_current = true",
            [person_name]
        ).fetchone()

        if not matter_row or not person_row:
            print(f"  [WARN] unresolved vote — matter_id={matter_id} person='{person_name}'")
            skipped += 1
            continue

        if con.execute("""
            SELECT 1 FROM fact_vote
            WHERE matter_sk = ? AND person_sk = ? AND vote_date = ?
        """, [matter_row[0], person_row[0], vote_date]).fetchone():
            skipped += 1
            continue

        con.execute("""
            INSERT INTO fact_vote (vote_sk, matter_sk, meeting_sk, person_sk, vote_date, vote_value)
            VALUES (nextval('seq_vote_sk'), ?, NULL, ?, ?, ?)
        """, [matter_row[0], person_row[0], vote_date, vote_value])
        n += 1

    print(f"  fact_vote          {n:>4} inserted, {skipped} skipped")


def transform_documents(con: duckdb.DuckDBPyConnection, ingest_date: date) -> None:
    rows = con.execute("""
        SELECT matter_id, document_id, attachment_name, attachment_url
        FROM stg_attachments
        WHERE ingest_date = ? AND document_id IS NOT NULL
    """, [ingest_date]).fetchall()

    n_doc = n_bridge = 0
    for (matter_id, document_id, name, url) in rows:
        doc_row = con.execute(
            "SELECT document_sk FROM dim_document WHERE document_id = ?", [document_id]
        ).fetchone()
        if not doc_row:
            con.execute("""
                INSERT INTO dim_document (document_sk, document_id, document_title, document_url)
                VALUES (nextval('seq_document_sk'), ?, ?, ?)
            """, [document_id, name, url])
            doc_sk = con.execute(
                "SELECT document_sk FROM dim_document WHERE document_id = ?", [document_id]
            ).fetchone()[0]
            n_doc += 1
        else:
            doc_sk = doc_row[0]

        matter_row = con.execute(
            "SELECT matter_sk FROM dim_matter WHERE matter_id = ? AND is_current = true",
            [matter_id]
        ).fetchone()
        if not matter_row:
            continue

        if not con.execute("""
            SELECT 1 FROM bridge_matter_document WHERE matter_sk = ? AND document_sk = ?
        """, [matter_row[0], doc_sk]).fetchone():
            con.execute(
                "INSERT INTO bridge_matter_document VALUES (?, ?)",
                [matter_row[0], doc_sk]
            )
            n_bridge += 1

    print(f"  dim_document       {n_doc:>4} inserted"
          f" | bridge_matter_document {n_bridge} inserted")


def transform_sponsors(con: duckdb.DuckDBPyConnection, ingest_date: date) -> None:
    rows = con.execute("""
        SELECT matter_id, sponsor_pos, sponsor_name
        FROM stg_sponsors WHERE ingest_date = ?
        ORDER BY matter_id, sponsor_pos
    """, [ingest_date]).fetchall()

    n = skipped = 0
    for (matter_id, pos, name) in rows:
        matter_row = con.execute(
            "SELECT matter_sk FROM dim_matter WHERE matter_id = ? AND is_current = true",
            [matter_id]
        ).fetchone()
        person_row = con.execute(
            "SELECT person_sk FROM dim_person WHERE full_name = ? AND is_current = true",
            [name]
        ).fetchone()
        if not matter_row or not person_row:
            print(f"  [WARN] unresolved sponsor — matter_id={matter_id} name='{name}'")
            skipped += 1
            continue

        if not con.execute("""
            SELECT 1 FROM bridge_matter_sponsor WHERE matter_sk = ? AND person_sk = ?
        """, [matter_row[0], person_row[0]]).fetchone():
            con.execute("""
                INSERT INTO bridge_matter_sponsor (matter_sk, person_sk, sponsor_type)
                VALUES (?, ?, ?)
            """, [matter_row[0], person_row[0], "primary" if pos == 0 else "co"])
            n += 1
        else:
            skipped += 1

    print(f"  bridge_matter_sponsor {n:>2} inserted, {skipped} skipped")


# ── orchestration ────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Transform staging tables into the star schema")
    ap.add_argument("--date", dest="ingest_date",
                    default=date.today().isoformat(),
                    help="partition date to transform YYYY-MM-DD (default: today)")
    ap.add_argument("--db", default=str(DB_PATH),
                    help=f"DuckDB file (default: {DB_PATH})")
    ap.add_argument("--seed-only", action="store_true",
                    help="seed dim_committee and dim_person only, skip facts")
    args = ap.parse_args()

    ingest_date = date.fromisoformat(args.ingest_date)
    con = duckdb.connect(args.db)
    ensure_sequences(con)

    already_run = con.execute(
        "SELECT completed_at FROM pipeline_runs WHERE ingest_date = ?", [ingest_date]
    ).fetchone()
    if already_run and not args.seed_only:
        print(f"[SKIP] {ingest_date} already transformed at {already_run[0]}.")
        print("       Remove the pipeline_runs row manually to re-run.")
        con.close()
        return

    print(f"Transforming {ingest_date}:")
    seed_committees(con)
    seed_persons(con)

    if not args.seed_only:
        transform_matters(con, ingest_date)
        transform_actions(con, ingest_date)
        transform_votes(con, ingest_date)
        transform_documents(con, ingest_date)
        transform_sponsors(con, ingest_date)

        con.execute("DELETE FROM pipeline_runs WHERE ingest_date = ?", [ingest_date])
        con.execute("INSERT INTO pipeline_runs VALUES (?, ?)", [ingest_date, datetime.now()])
        print(f"Done. Run recorded in pipeline_runs.")

    con.close()


if __name__ == "__main__":
    main()
