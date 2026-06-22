"""transform_gold.py — UNIFIED gold builder for both slices (integration branch).

Supersedes the per-slice transform_star.py (legislation) and transform_meeting_star.py (meeting).
Reads BOTH silver stagings and builds the full milestone-3 star schema (erd/schema.dbml), including
the JOINT fact merge that the cross-slice contract describes.

Design:
  * Self-contained: dims are seeded from SCRAPED data (no bodies.json / persons.json dependency).
    dim_committee from scraped body names; dim_person from scraped names, using the meeting slice's
    real PersonId where available (more robust than name-only).
  * Full rebuild each run, child-first delete then parent-first insert — idempotent, and it satisfies
    DuckDB's FK enforcement (which forbids deleting an FK-referenced parent). Re-run = recovery.
  * The shared scrape/action_types.py is the SINGLE label->code / vote-normalization authority, so the
    fact dedup keys are consistent across slices.

Fact merge (meeting is system-of-record; legislation backfills):
  * fact_matter_action natural key (matter_id, meeting_id, action_type_code); fact_vote (matter_id,
    meeting_id, person_id). Meeting rows are written first (they carry a clean EventId -> meeting_sk);
    legislation rows are added only where the meeting scrape didn't already cover them, with meeting_sk
    resolved best-effort via (committee, action_date) -> dim_meeting.

Usage:
    python warehouse/transform_gold.py            # build gold from whatever staging is loaded
"""

import argparse
import sys
from datetime import date, datetime, time
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
DB_PATH = REPO_ROOT / "warehouse" / "db" / "legislation.duckdb"
DDL_DIR = REPO_ROOT / "warehouse" / "ddl"

from scrape.action_types import DIM_ACTION_TYPE_SEED, normalize_action, normalize_vote  # noqa: E402

SYNTH_PERSON_BASE = 9_000_000   # synthetic person_ids for legislation-only names (no real PersonId)


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except (ValueError, AttributeError):
        return None


def parse_time(s):
    if not s:
        return None
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            return datetime.strptime(s.strip().upper().replace(".", ""), fmt).time()
        except (ValueError, AttributeError):
            continue
    return None


# ── schema / sequences ────────────────────────────────────────────────────────
def ensure_schema(con):
    """Create ALL staging + the shared gold so queries over the other slice's (possibly empty)
    staging never fail, whichever slice loaded data."""
    for ddl in ("01_staging.sql", "03_meeting_staging.sql", "02_star.sql"):
        con.execute((DDL_DIR / ddl).read_text())


def ensure_sequences(con):
    for seq in ("seq_committee_sk", "seq_person_sk", "seq_matter_sk", "seq_subject_sk",
                "seq_document_sk", "seq_meeting_sk", "seq_vote_sk", "seq_matter_action_sk",
                "seq_membership_sk"):
        con.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq} START 1")


def clean_gold(con):
    """Child-first delete of the whole gold star (autocommit: each delete commits before the next,
    so FK checks pass). dim_action_type is static and reseeded idempotently, not deleted."""
    for t in ("bridge_matter_subject", "bridge_matter_sponsor", "bridge_matter_document",
              "bridge_meeting_document", "fact_vote", "fact_matter_action",
              "fact_committee_membership", "dim_meeting", "dim_matter", "dim_document",
              "dim_subject", "dim_person", "dim_committee"):
        con.execute(f"DELETE FROM {t}")


# ── dimensions ────────────────────────────────────────────────────────────────
def seed_action_types(con):
    for code, category, desc in DIM_ACTION_TYPE_SEED:
        if not con.execute("SELECT 1 FROM dim_action_type WHERE action_type_code=?", [code]).fetchone():
            con.execute("INSERT INTO dim_action_type VALUES (?,?,?)", [code, category, desc])


def seed_committees(con):
    """Self-contained: union of every scraped body name (meeting calendar + legislation actions/control)."""
    names = con.execute("""
        SELECT DISTINCT body_name AS n FROM stg_meetings WHERE body_name IS NOT NULL
        UNION SELECT DISTINCT body FROM stg_actions WHERE body IS NOT NULL
        UNION SELECT DISTINCT in_control FROM stg_matters WHERE in_control IS NOT NULL
    """).fetchall()
    n = 0
    for (name,) in names:
        ctype = "Full Board" if name == "Board of Supervisors" else "Standing Committee"
        con.execute("""INSERT INTO dim_committee (committee_sk, committee_id, committee_name,
                       committee_type, is_active) VALUES (nextval('seq_committee_sk'), NULL, ?, ?, true)""",
                    [name, ctype])
        n += 1
    print(f"  dim_committee      {n:>4}")


def seed_persons(con):
    """Self-contained: one current row per distinct name, preferring the meeting slice's real PersonId."""
    id_by_name = {name: pid for name, pid in con.execute(
        "SELECT DISTINCT person_name, person_id FROM stg_meeting_votes "
        "WHERE person_name IS NOT NULL AND person_id IS NOT NULL").fetchall()}
    names = [r[0] for r in con.execute("""
        SELECT DISTINCT person_name AS n FROM stg_meeting_votes WHERE person_name IS NOT NULL
        UNION SELECT DISTINCT person_name FROM stg_votes WHERE person_name IS NOT NULL
        UNION SELECT DISTINCT sponsor_name FROM stg_sponsors WHERE sponsor_name IS NOT NULL
    """).fetchall()]
    synth = SYNTH_PERSON_BASE
    real = n = 0
    for name in names:
        pid = id_by_name.get(name)
        if pid is None:
            synth += 1
            pid = synth
        else:
            real += 1
        con.execute("""INSERT INTO dim_person (person_sk, person_id, full_name, effective_from,
                       effective_to, is_current) VALUES (nextval('seq_person_sk'), ?, ?, '2020-01-01', NULL, true)""",
                    [pid, name])
        n += 1
    print(f"  dim_person         {n:>4} ({real} with real PersonId from meeting scrape)")


def build_matters(con):
    """dim_matter from legislation staging (flat, latest per matter), plus html_stub rows for matter
    files a meeting agenda references but legislation hasn't scraped (prevents orphaned facts)."""
    rows = con.execute("""
        SELECT matter_id, matter_file, title, name, matter_type, introduced_raw, in_control, detail_url
        FROM stg_matters
        QUALIFY ROW_NUMBER() OVER (PARTITION BY matter_file ORDER BY ingest_date DESC, scraped_at DESC)=1
    """).fetchall()
    leg = stub = 0
    for (mid, mfile, title, name, mtype, intro, in_control, url) in rows:
        committee = con.execute("SELECT committee_sk FROM dim_committee WHERE committee_name=?",
                                [in_control]).fetchone()
        con.execute("""INSERT INTO dim_matter (matter_sk, matter_id, matter_file, matter_title,
            matter_name, matter_type, introduction_date, controlling_committee_sk, legistar_url,
            matter_source) VALUES (nextval('seq_matter_sk'),?,?,?,?,?,?,?,?, 'legislation')""",
            [mid, mfile, title, name, mtype, parse_date(intro),
             committee[0] if committee else None, url])
        leg += 1
    # stubs from meeting agenda items
    stub_rows = con.execute("""
        SELECT matter_file, MAX(title) t, MAX(matter_name) n, MAX(matter_type) ty
        FROM stg_meeting_agenda_items
        WHERE matter_file IS NOT NULL
          AND matter_file NOT IN (SELECT matter_file FROM dim_matter WHERE matter_file IS NOT NULL)
        GROUP BY matter_file
    """).fetchall()
    for (mfile, title, name, mtype) in stub_rows:
        con.execute("""INSERT INTO dim_matter (matter_sk, matter_id, matter_file, matter_title,
            matter_name, matter_type, matter_source)
            VALUES (nextval('seq_matter_sk'), NULL, ?, ?, ?, ?, 'html_stub')""",
            [mfile, title, name, mtype])
        stub += 1
    print(f"  dim_matter         {leg:>4} legislation, {stub} html_stub")


def build_meetings(con):
    rows = con.execute("""
        SELECT meeting_id, event_guid, body_name, meeting_date_raw, meeting_time_raw, location,
               meeting_subtype, agenda_status, minutes_status, agenda_url, minutes_url, video_clip_id
        FROM stg_meetings
        QUALIFY ROW_NUMBER() OVER (PARTITION BY meeting_id ORDER BY ingest_date DESC, scraped_at DESC)=1
    """).fetchall()
    for (mid, guid, body, d, t, loc, sub, ast, mst, aurl, murl, clip) in rows:
        committee = con.execute("SELECT committee_sk FROM dim_committee WHERE committee_name=?",
                                [body]).fetchone()
        con.execute("""INSERT INTO dim_meeting (meeting_sk, meeting_id, event_guid, committee_sk,
            meeting_date, meeting_time, location, meeting_subtype, agenda_status, minutes_status,
            agenda_url, minutes_url, video_clip_id)
            VALUES (nextval('seq_meeting_sk'),?,?,?,?,?,?,?,?,?,?,?,?)""",
            [mid, guid, committee[0] if committee else None, parse_date(d), parse_time(t),
             loc, sub, ast, mst, aurl, murl, clip])
    print(f"  dim_meeting        {len(rows):>4}")


def build_documents(con):
    """matter attachments (legislation) + meeting docs (meeting), into dim_document + bridges."""
    # matter attachments
    att = con.execute("""
        SELECT matter_id, document_id, attachment_name, attachment_url
        FROM stg_attachments WHERE document_id IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY document_id ORDER BY ingest_date DESC, scraped_at DESC)=1
    """).fetchall()
    n_doc = n_br = 0
    for (mid, did, name, url) in att:
        con.execute("""INSERT INTO dim_document (document_sk, document_id, document_source,
            document_title, document_url, scraped_at) VALUES (nextval('seq_document_sk'), ?, 'matter_attachment', ?, ?, ?)""",
            [did, name, url, datetime.now()])
        doc_sk = con.execute("SELECT currval('seq_document_sk')").fetchone()[0]
        m = con.execute("SELECT matter_sk FROM dim_matter WHERE matter_id=?", [mid]).fetchone()
        if m and not con.execute("SELECT 1 FROM bridge_matter_document WHERE matter_sk=? AND document_sk=?",
                                 [m[0], doc_sk]).fetchone():
            con.execute("INSERT INTO bridge_matter_document VALUES (?,?)", [m[0], doc_sk])
            n_br += 1
        n_doc += 1
    # meeting docs (one per meeting+source; latest URL wins)
    mdocs = con.execute("""
        SELECT meeting_id, document_source, document_title, document_url, body_text
        FROM stg_meeting_documents
        QUALIFY ROW_NUMBER() OVER (PARTITION BY meeting_id, document_source ORDER BY ingest_date DESC, scraped_at DESC)=1
    """).fetchall()
    for (mid, source, title, url, body) in mdocs:
        meeting = con.execute("SELECT meeting_sk FROM dim_meeting WHERE meeting_id=?", [mid]).fetchone()
        if not meeting:
            continue
        dtype = {"meeting_agenda": "Agenda", "meeting_minutes": "Minutes", "transcript": "Transcript"}.get(source)
        con.execute("""INSERT INTO dim_document (document_sk, document_id, document_source,
            document_title, document_url, document_type, body_text, scraped_at)
            VALUES (nextval('seq_document_sk'), NULL, ?, ?, ?, ?, ?, ?)""",
            [source, title, url, dtype, body, datetime.now()])
        doc_sk = con.execute("SELECT currval('seq_document_sk')").fetchone()[0]
        con.execute("INSERT INTO bridge_meeting_document VALUES (?,?)", [meeting[0], doc_sk])
        n_doc += 1
        n_br += 1
    print(f"  dim_document       {n_doc:>4} | matter+meeting bridges {n_br}")


def build_sponsors(con):
    rows = con.execute("""
        SELECT matter_id, sponsor_pos, sponsor_name FROM stg_sponsors
        ORDER BY matter_id, sponsor_pos
    """).fetchall()
    n = 0
    for (mid, pos, name) in rows:
        m = con.execute("SELECT matter_sk FROM dim_matter WHERE matter_id=?", [mid]).fetchone()
        p = con.execute("SELECT person_sk FROM dim_person WHERE full_name=? AND is_current=true",
                        [name]).fetchone()
        if not m or not p:
            continue
        stype = "Primary" if pos == 0 else "Co"
        if not con.execute("""SELECT 1 FROM bridge_matter_sponsor
                              WHERE matter_sk=? AND person_sk=? AND sponsor_type=?""",
                           [m[0], p[0], stype]).fetchone():
            con.execute("INSERT INTO bridge_matter_sponsor VALUES (?,?,?)", [m[0], p[0], stype])
            n += 1
    print(f"  bridge_matter_sponsor {n:>2}")


# ── facts (the joint merge) ───────────────────────────────────────────────────
def _msk_by_file(con, mfile):
    r = con.execute("SELECT matter_sk FROM dim_matter WHERE matter_file=?", [mfile]).fetchone()
    return r[0] if r else None


def _msk_by_id(con, mid):
    r = con.execute("SELECT matter_sk FROM dim_matter WHERE matter_id=?", [mid]).fetchone()
    return r[0] if r else None


def _person_sk(con, name):
    r = con.execute("SELECT person_sk FROM dim_person WHERE full_name=? AND is_current=true",
                    [name]).fetchone()
    return r[0] if r else None


def _meeting_sk_by_committee_date(con, body_name, d):
    """Best-effort meeting_sk for a legislation action: (committee, date) -> dim_meeting."""
    if not body_name or d is None:
        return None
    r = con.execute("""SELECT m.meeting_sk FROM dim_meeting m JOIN dim_committee c
                       ON c.committee_sk=m.committee_sk
                       WHERE c.committee_name=? AND m.meeting_date=?""", [body_name, d]).fetchone()
    return r[0] if r else None


def build_facts(con):
    """Meeting-sourced facts first (authoritative, clean meeting_sk); then legislation backfill where
    the meeting scrape didn't already cover the (matter, meeting, action/person) tuple."""
    fma = fv = fma_leg = fv_leg = 0

    # --- fact_matter_action: meeting-sourced ---
    items = con.execute("""
        SELECT i.meeting_id, i.matter_file, i.action_raw, i.action_result, i.action_text,
               m.meeting_date_raw, m.body_name
        FROM stg_meeting_agenda_items i
        JOIN stg_meetings m ON m.meeting_id=i.meeting_id AND m.ingest_date=i.ingest_date
        WHERE i.action_raw IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY i.meeting_id, i.matter_file, i.action_raw
                                   ORDER BY i.ingest_date DESC, i.scraped_at DESC)=1
    """).fetchall()
    for (mid, mfile, action_raw, result, text, mdate, body) in items:
        msk = _msk_by_file(con, mfile)
        meeting = con.execute("SELECT meeting_sk FROM dim_meeting WHERE meeting_id=?", [mid]).fetchone()
        if not msk or not meeting:
            continue
        code = normalize_action(action_raw).code
        if con.execute("""SELECT 1 FROM fact_matter_action WHERE matter_sk=? AND meeting_sk=? AND action_type_code=?""",
                       [msk, meeting[0], code]).fetchone():
            continue
        con.execute("""INSERT INTO fact_matter_action (matter_action_sk, matter_sk, meeting_sk,
            action_type_code, action_result, action_date, action_text, source)
            VALUES (nextval('seq_matter_action_sk'),?,?,?,?,?,?, 'meeting')""",
            [msk, meeting[0], code, result, parse_date(mdate), text])
        fma += 1

    # --- fact_matter_action: legislation backfill ---
    leg_actions = con.execute("""
        SELECT a.matter_id, a.body, a.action, a.result, a.action_date_raw
        FROM stg_actions a WHERE a.action IS NOT NULL AND a.action <> ''
    """).fetchall()
    for (mid, body, action, result, adate_raw) in leg_actions:
        msk = _msk_by_id(con, mid)
        if not msk:
            continue
        code = normalize_action(action).code
        adate = parse_date(adate_raw)
        meeting_sk = _meeting_sk_by_committee_date(con, body, adate)
        # already covered by a meeting row?
        if meeting_sk and con.execute("""SELECT 1 FROM fact_matter_action
                WHERE matter_sk=? AND meeting_sk=? AND action_type_code=?""",
                [msk, meeting_sk, code]).fetchone():
            continue
        # dedup legislation-only rows (NULL meeting) on (matter, code, date)
        if meeting_sk is None and con.execute("""SELECT 1 FROM fact_matter_action
                WHERE matter_sk=? AND meeting_sk IS NULL AND action_type_code=? AND action_date=?""",
                [msk, code, adate]).fetchone():
            continue
        con.execute("""INSERT INTO fact_matter_action (matter_action_sk, matter_sk, meeting_sk,
            action_type_code, action_result, action_date, action_text, source)
            VALUES (nextval('seq_matter_action_sk'),?,?,?,?,?,?, 'legislation')""",
            [msk, meeting_sk, code, result or None, adate, action])
        fma_leg += 1

    # --- fact_vote: meeting-sourced ---
    mvotes = con.execute("""
        SELECT v.meeting_id, v.person_name, v.vote_value_raw, i.matter_file, i.action_text, m.body_name
        FROM stg_meeting_votes v
        JOIN stg_meeting_agenda_items i ON i.meeting_id=v.meeting_id AND i.item_seq=v.item_seq AND i.ingest_date=v.ingest_date
        JOIN stg_meetings m ON m.meeting_id=v.meeting_id AND m.ingest_date=v.ingest_date
        QUALIFY ROW_NUMBER() OVER (PARTITION BY v.meeting_id, i.matter_file, v.person_name
                                   ORDER BY v.ingest_date DESC, v.scraped_at DESC)=1
    """).fetchall()
    for (mid, person, vraw, mfile, motion, body) in mvotes:
        msk = _msk_by_file(con, mfile)
        meeting = con.execute("SELECT meeting_sk FROM dim_meeting WHERE meeting_id=?", [mid]).fetchone()
        psk = _person_sk(con, person)
        if not msk or not meeting or not psk:
            continue
        scope = "board" if body == "Board of Supervisors" else "committee"
        if con.execute("SELECT 1 FROM fact_vote WHERE matter_sk=? AND meeting_sk=? AND person_sk=?",
                       [msk, meeting[0], psk]).fetchone():
            continue
        con.execute("""INSERT INTO fact_vote (vote_sk, matter_sk, meeting_sk, person_sk, body_scope,
            vote_date, vote_value, motion_text, source)
            VALUES (nextval('seq_vote_sk'),?,?,?,?,(SELECT meeting_date FROM dim_meeting WHERE meeting_sk=?),?,?, 'meeting')""",
            [msk, meeting[0], psk, scope, meeting[0], normalize_vote(vraw), motion])
        fv += 1

    # --- fact_vote: legislation backfill ---
    lvotes = con.execute("""
        SELECT v.matter_id, v.person_name, v.vote_value, a.body, a.action_date_raw
        FROM stg_votes v
        JOIN stg_actions a ON a.matter_id=v.matter_id AND a.action_seq=v.action_seq AND a.ingest_date=v.ingest_date
    """).fetchall()
    for (mid, person, vraw, body, adate_raw) in lvotes:
        msk = _msk_by_id(con, mid)
        psk = _person_sk(con, person)
        if not msk or not psk:
            continue
        adate = parse_date(adate_raw)
        meeting_sk = _meeting_sk_by_committee_date(con, body, adate)
        scope = "board" if body == "Board of Supervisors" else "committee"
        if meeting_sk and con.execute("SELECT 1 FROM fact_vote WHERE matter_sk=? AND meeting_sk=? AND person_sk=?",
                                      [msk, meeting_sk, psk]).fetchone():
            continue
        if meeting_sk is None and con.execute("""SELECT 1 FROM fact_vote
                WHERE matter_sk=? AND meeting_sk IS NULL AND person_sk=? AND vote_date=?""",
                [msk, psk, adate]).fetchone():
            continue
        con.execute("""INSERT INTO fact_vote (vote_sk, matter_sk, meeting_sk, person_sk, body_scope,
            vote_date, vote_value, motion_text, source)
            VALUES (nextval('seq_vote_sk'),?,?,?,?,?,?,NULL, 'legislation')""",
            [msk, meeting_sk, psk, scope, adate, normalize_vote(vraw)])
        fv_leg += 1

    print(f"  fact_matter_action {fma + fma_leg:>4} ({fma} meeting, {fma_leg} legislation backfill)")
    print(f"  fact_vote          {fv + fv_leg:>4} ({fv} meeting, {fv_leg} legislation backfill)")


# ── orchestration ─────────────────────────────────────────────────────────────
def build(con):
    ensure_schema(con)
    ensure_sequences(con)
    print("Building unified gold:")
    seed_action_types(con)
    clean_gold(con)
    seed_committees(con)
    seed_persons(con)
    build_matters(con)
    build_meetings(con)
    build_documents(con)
    build_sponsors(con)
    build_facts(con)
    print("Done.")


def main():
    ap = argparse.ArgumentParser(description="Build the unified gold star from both slices' staging")
    ap.add_argument("--db", default=str(DB_PATH), help=f"DuckDB file (default: {DB_PATH})")
    args = ap.parse_args()
    con = duckdb.connect(args.db)
    try:
        build(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
