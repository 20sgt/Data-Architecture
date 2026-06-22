"""transform_meeting_star.py — build the meeting slice's uncontested gold tables.

Populates (milestone-3 ERD): dim_action_type, dim_committee (provisional seed),
dim_meeting, dim_document (meeting docs), bridge_meeting_document.

Does NOT build fact_matter_action / fact_vote — those are assembled in the joint
cross-slice merge (see 04_meeting_star.sql contract block + docs/meeting_pipeline_design.md).

Idempotent by design: dim_meeting/dim_document upsert on their natural keys, so re-running
(or re-scraping a meeting whose minutes went Draft -> Final) UPDATES in place. Operates on
the latest scrape per meeting across all loaded partitions.

Usage:
    python warehouse/transform_meeting_star.py
"""

import argparse
import sys
from datetime import date, datetime, time
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))   # so `scrape` is importable when run as a script
DB_PATH = REPO_ROOT / "warehouse" / "db" / "legislation.duckdb"

from scrape.action_types import DIM_ACTION_TYPE_SEED  # noqa: E402


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def parse_time(s: str | None) -> time | None:
    if not s:
        return None
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            return datetime.strptime(s.strip().upper().replace(".", ""), fmt).time()
        except ValueError:
            continue
    return None


def ensure_sequences(con: duckdb.DuckDBPyConnection) -> None:
    for seq in ("seq_committee_sk", "seq_meeting_sk", "seq_document_sk"):
        con.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq} START 1")


# ── lookups / dims ────────────────────────────────────────────────────────────
def seed_action_types(con: duckdb.DuckDBPyConnection) -> None:
    """Seed dim_action_type from the shared scrape/action_types.py module (idempotent)."""
    n = 0
    for code, category, desc in DIM_ACTION_TYPE_SEED:
        if not con.execute("SELECT 1 FROM dim_action_type WHERE action_type_code = ?",
                           [code]).fetchone():
            con.execute("INSERT INTO dim_action_type VALUES (?,?,?)", [code, category, desc])
            n += 1
    print(f"  dim_action_type    {n:>4} inserted")


def seed_committees_provisional(con: duckdb.DuckDBPyConnection) -> None:
    """Provisionally seed dim_committee from distinct meeting body names (committee_id NULL).

    The legislation slice owns the authoritative seed (bodies.json with real BodyIds); these
    rows are reconciled on name at merge. Lets dim_meeting.committee_sk resolve standalone.
    """
    names = con.execute("""
        SELECT DISTINCT body_name FROM stg_meetings WHERE body_name IS NOT NULL
    """).fetchall()
    n = 0
    for (name,) in names:
        if con.execute("SELECT 1 FROM dim_committee WHERE committee_name = ?", [name]).fetchone():
            continue
        ctype = "Full Board" if name == "Board of Supervisors" else "Standing Committee"
        con.execute("""
            INSERT INTO dim_committee (committee_sk, committee_id, committee_name,
                committee_type, is_active)
            VALUES (nextval('seq_committee_sk'), NULL, ?, ?, true)
        """, [name, ctype])
        n += 1
    print(f"  dim_committee      {n:>4} provisional inserted")


MEETING_DOC_SOURCES = ("meeting_agenda", "meeting_minutes", "transcript")


def rebuild_meeting_gold(con: duckdb.DuckDBPyConnection) -> None:
    """Full refresh of the meeting-owned gold subgraph from the latest staging.

    Child-first delete then insert-only. This sidesteps DuckDB's limitation (can't UPDATE a
    row referenced by an FK) and makes re-scrapes trivially correct: the latest ingest of each
    meeting fully replaces the prior gold rows. The subgraph (dim_meeting <- bridge ->
    meeting docs in dim_document) is self-contained in scope B, so rebuilding it together is
    consistent; matter_attachment docs are left untouched.
    """
    # NOT wrapped in an explicit transaction on purpose: DuckDB cannot delete an FK-referenced
    # parent (dim_document/dim_meeting) inside a transaction that also deletes its children — the
    # child deletes aren't visible to the parent's FK check until commit. In autocommit, child-first
    # deletes each commit before the parent delete. Atomicity isn't needed because the refresh is
    # idempotent: a mid-run failure is recovered by simply re-running the transform.
    placeholders = ",".join("?" * len(MEETING_DOC_SOURCES))
    con.execute("DELETE FROM bridge_meeting_document")          # child first
    con.execute(f"DELETE FROM dim_document WHERE document_source IN ({placeholders})",
                list(MEETING_DOC_SOURCES))
    con.execute("DELETE FROM dim_meeting")
    _insert_meetings(con)
    _insert_documents(con)


# ── dim_meeting (flat, insert-only after rebuild's delete) ────────────────────
def _insert_meetings(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute("""
        SELECT meeting_id, event_guid, body_name, meeting_date_raw, meeting_time_raw,
               location, meeting_subtype, agenda_status, minutes_status,
               agenda_url, minutes_url, video_clip_id
        FROM stg_meetings
        QUALIFY ROW_NUMBER() OVER (PARTITION BY meeting_id
                                   ORDER BY ingest_date DESC, scraped_at DESC) = 1
    """).fetchall()

    n = 0
    for (meeting_id, event_guid, body_name, mdate, mtime, location, subtype,
         astatus, mstatus, agenda_url, minutes_url, clip) in rows:
        committee = con.execute(
            "SELECT committee_sk FROM dim_committee WHERE committee_name = ?", [body_name]
        ).fetchone()
        con.execute("""
            INSERT INTO dim_meeting (meeting_sk, meeting_id, event_guid, committee_sk,
                meeting_date, meeting_time, location, meeting_subtype, agenda_status,
                minutes_status, agenda_url, minutes_url, video_clip_id)
            VALUES (nextval('seq_meeting_sk'),?,?,?,?,?,?,?,?,?,?,?,?)
        """, [meeting_id, event_guid, committee[0] if committee else None,
              parse_date(mdate), parse_time(mtime), location, subtype, astatus, mstatus,
              agenda_url, minutes_url, clip])
        n += 1
    print(f"  dim_meeting        {n:>4} rebuilt")


# ── meeting documents + bridge (insert-only after rebuild's delete) ───────────
def _insert_documents(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute("""
        SELECT meeting_id, document_source, document_title, document_url, body_text
        FROM stg_meeting_documents
        QUALIFY ROW_NUMBER() OVER (PARTITION BY meeting_id, document_source
                                   ORDER BY ingest_date DESC, scraped_at DESC) = 1
    """).fetchall()

    doc_n = bridge_n = 0
    for (meeting_id, source, title, url, body_text) in rows:
        meeting = con.execute(
            "SELECT meeting_sk FROM dim_meeting WHERE meeting_id = ?", [meeting_id]).fetchone()
        if not meeting:
            continue
        dtype = {"meeting_agenda": "Agenda", "meeting_minutes": "Minutes",
                 "transcript": "Transcript"}.get(source)
        con.execute("""
            INSERT INTO dim_document (document_sk, document_id, document_source,
                document_title, document_url, document_type, body_text, scraped_at)
            VALUES (nextval('seq_document_sk'), NULL, ?, ?, ?, ?, ?, ?)
        """, [source, title, url, dtype, body_text, datetime.now()])
        doc_sk = con.execute("SELECT currval('seq_document_sk')").fetchone()[0]
        con.execute("INSERT INTO bridge_meeting_document VALUES (?,?)", [meeting[0], doc_sk])
        doc_n += 1
        bridge_n += 1
    print(f"  dim_document       {doc_n:>4} inserted | bridge_meeting_document {bridge_n} inserted")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the meeting slice's uncontested gold tables")
    ap.add_argument("--db", default=str(DB_PATH), help=f"DuckDB file (default: {DB_PATH})")
    args = ap.parse_args()

    con = duckdb.connect(args.db)
    try:
        ensure_sequences(con)
        print("Transforming meeting gold:")
        seed_action_types(con)
        seed_committees_provisional(con)
        rebuild_meeting_gold(con)
        print("Done.")
    finally:
        con.close()


if __name__ == "__main__":
    main()
