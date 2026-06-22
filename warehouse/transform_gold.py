"""transform_gold.py — UNIFIED gold builder for both slices (integration branch).

Supersedes the per-slice transforms. Reads BOTH silver stagings and builds the full milestone-3 star
(erd/schema.dbml), including the JOINT fact merge. This is the LOCAL/DuckDB builder; the Databricks
path consumes exported Parquet (see warehouse/export_parquet.py), it does not run this transform.

Design:
  * Self-contained: dims seeded from SCRAPED data (no bodies.json/persons.json dependency).
    dim_committee from scraped body names; dim_person from scraped names, preferring the meeting
    slice's real PersonId. Committee + person names are matched through ONE canonicalizer
    (Python only — see _canon_*), via in-memory caches, so there is no Python-vs-SQL parity gap and
    the two independently-scraped sources don't split into duplicate dims.
  * Full rebuild each run, child-first delete then parent-first insert — idempotent (re-run = recovery)
    and FK-safe under DuckDB (which forbids deleting an FK-referenced parent inside a transaction).
  * scrape/action_types.py is the SINGLE label->code / vote normalization authority.

Fact merge (meeting is system-of-record; legislation backfills):
  * Distinct actions are PRESERVED: each agenda item (meeting) / history entry (legislation) is its own
    row — dedup is per logical source row (QUALIFY on the natural row id), not by normalized code.
  * Cross-slice suppression is SOURCE-BASED, not code-based: the meeting slice is authoritative for any
    (matter, meeting) it recorded, so a legislation row is skipped when the meeting already covered that
    pair — regardless of how each slice normalized the label (avoids double-counting one physical event
    under two codes). When the meeting_sk can't be resolved (ambiguous/unmatched committee+date), a
    legislation row is instead suppressed if the meeting already recorded the same (matter, date[,person]).
"""

import argparse
import re
import sys
from datetime import date, datetime, time
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
DB_PATH = REPO_ROOT / "warehouse" / "db" / "legislation.duckdb"
DDL_DIR = REPO_ROOT / "warehouse" / "ddl"

from scrape.action_types import DIM_ACTION_TYPE_SEED, normalize_action, normalize_vote  # noqa: E402

_WS = re.compile(r"\s+")


def _canon_committee(name):
    """Canonical committee key (Unicode-whitespace safe via Python \\s/str.strip — incl. NBSP)."""
    if not name:
        return None
    c = _WS.sub(" ", name.strip().lower().replace("&", "and"))
    return c or None


def _canon_person(name):
    if not name:
        return None
    c = _WS.sub(" ", name.strip()).lower()
    return c or None


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


# ── canonical resolution caches (built once after seeding; Python-only canon) ──
def build_committee_cache(con):
    return {c: sk for sk, n in con.execute("SELECT committee_sk, committee_name FROM dim_committee").fetchall()
            if (c := _canon_committee(n))}


def build_person_cache(con):
    return {c: sk for sk, n in con.execute(
        "SELECT person_sk, full_name FROM dim_person WHERE is_current=true").fetchall()
        if (c := _canon_person(n))}


def resolve_committee_sk(ccache, name):
    return ccache.get(_canon_committee(name))


def resolve_person_sk(pcache, name):
    return pcache.get(_canon_person(name))


def _msk_by_file(con, mfile):
    if not mfile:
        return None
    r = con.execute("SELECT matter_sk FROM dim_matter WHERE matter_file=?", [mfile]).fetchone()
    return r[0] if r else None


def _msk_by_id(con, mid):
    if mid is None:
        return None
    r = con.execute("SELECT matter_sk FROM dim_matter WHERE matter_id=?", [mid]).fetchone()
    return r[0] if r else None


def _meeting_sk(con, ccache, body_name, d):
    """(committee, date) -> meeting_sk. Resolved via canonical committee_sk. Returns None if no match
    OR if AMBIGUOUS (a body meeting twice on one date) — never guesses an arbitrary meeting."""
    csk = resolve_committee_sk(ccache, body_name)
    if csk is None or d is None:
        return None
    rows = con.execute("SELECT meeting_sk FROM dim_meeting WHERE committee_sk=? AND meeting_date=?",
                       [csk, d]).fetchall()
    return rows[0][0] if len(rows) == 1 else None


# ── dimensions ────────────────────────────────────────────────────────────────
def seed_action_types(con):
    for code, category, desc in DIM_ACTION_TYPE_SEED:
        if not con.execute("SELECT 1 FROM dim_action_type WHERE action_type_code=?", [code]).fetchone():
            con.execute("INSERT INTO dim_action_type VALUES (?,?,?)", [code, category, desc])


def seed_committees(con):
    """One row per CANONICAL body name (union of meeting calendar + legislation actions/control), so
    '&'/'and', whitespace (incl. NBSP) and case variants across the two sources don't duplicate."""
    names = [r[0] for r in con.execute("""
        SELECT DISTINCT body_name AS n FROM stg_meetings WHERE body_name IS NOT NULL
        UNION SELECT DISTINCT body FROM stg_actions WHERE body IS NOT NULL
        UNION SELECT DISTINCT in_control FROM stg_matters WHERE in_control IS NOT NULL
    """).fetchall()]
    seen = set()
    n = 0
    for name in names:
        c = _canon_committee(name)
        if not c or c in seen:                       # skip blank / already-seen canonical names
            continue
        seen.add(c)
        ctype = "Full Board" if c == "board of supervisors" else "Standing Committee"
        con.execute("""INSERT INTO dim_committee (committee_sk, committee_id, committee_name,
                       committee_type, is_active) VALUES (nextval('seq_committee_sk'), NULL, ?, ?, true)""",
                    [name.strip(), ctype])
        n += 1
    print(f"  dim_committee      {n:>4}")


def seed_persons(con):
    """One row per CANONICAL name, preferring the meeting slice's real PersonId. Synthetic ids are
    assigned strictly ABOVE any real id seen, so they can never collide with a real PersonId."""
    id_by_canon = {}
    for name, pid in con.execute("SELECT DISTINCT person_name, person_id FROM stg_meeting_votes "
                                 "WHERE person_name IS NOT NULL AND person_id IS NOT NULL").fetchall():
        c = _canon_person(name)
        if c:
            id_by_canon.setdefault(c, pid)
    names = [r[0] for r in con.execute("""
        SELECT DISTINCT person_name AS n FROM stg_meeting_votes WHERE person_name IS NOT NULL
        UNION SELECT DISTINCT person_name FROM stg_votes WHERE person_name IS NOT NULL
        UNION SELECT DISTINCT sponsor_name FROM stg_sponsors WHERE sponsor_name IS NOT NULL
    """).fetchall()]
    synth = max([*id_by_canon.values(), 999_999]) + 1   # strictly above every real PersonId
    seen = set()
    real = n = 0
    for name in names:
        c = _canon_person(name)
        if not c or c in seen:
            continue
        seen.add(c)
        pid = id_by_canon.get(c)
        if pid is None:
            pid = synth
            synth += 1
        else:
            real += 1
        con.execute("""INSERT INTO dim_person (person_sk, person_id, full_name, effective_from,
                       effective_to, is_current) VALUES (nextval('seq_person_sk'), ?, ?, '2020-01-01', NULL, true)""",
                    [pid, name.strip()])
        n += 1
    print(f"  dim_person         {n:>4} ({real} with real PersonId from meeting scrape)")


def build_matters(con, ccache):
    """dim_matter from legislation staging (flat, latest per matter), plus html_stub rows for matter
    files a meeting agenda references but legislation hasn't scraped. Blank matter_file -> NULL so two
    file-less matters don't collapse (keyed by matter_id instead)."""
    rows = con.execute("""
        SELECT matter_id, NULLIF(matter_file,'') AS mf, title, name, matter_type, introduced_raw,
               in_control, detail_url
        FROM stg_matters
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY COALESCE(NULLIF(matter_file,''), 'mid:' || CAST(matter_id AS VARCHAR))
            ORDER BY ingest_date DESC, scraped_at DESC)=1
    """).fetchall()
    leg = stub = 0
    for (mid, mfile, title, name, mtype, intro, in_control, url) in rows:
        con.execute("""INSERT INTO dim_matter (matter_sk, matter_id, matter_file, matter_title,
            matter_name, matter_type, introduction_date, controlling_committee_sk, legistar_url,
            matter_source) VALUES (nextval('seq_matter_sk'),?,?,?,?,?,?,?,?, 'legislation')""",
            [mid, mfile, title, name, mtype, parse_date(intro),
             resolve_committee_sk(ccache, in_control), url])
        leg += 1
    stub_rows = con.execute("""
        SELECT matter_file, MAX(title) t, MAX(matter_name) n, MAX(matter_type) ty
        FROM stg_meeting_agenda_items
        WHERE matter_file IS NOT NULL AND matter_file <> ''
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


def build_meetings(con, ccache):
    rows = con.execute("""
        SELECT meeting_id, event_guid, body_name, meeting_date_raw, meeting_time_raw, location,
               meeting_subtype, agenda_status, minutes_status, agenda_url, minutes_url, video_clip_id
        FROM stg_meetings
        QUALIFY ROW_NUMBER() OVER (PARTITION BY meeting_id ORDER BY ingest_date DESC, scraped_at DESC)=1
    """).fetchall()
    for (mid, guid, body, d, t, loc, sub, ast, mst, aurl, murl, clip) in rows:
        con.execute("""INSERT INTO dim_meeting (meeting_sk, meeting_id, event_guid, committee_sk,
            meeting_date, meeting_time, location, meeting_subtype, agenda_status, minutes_status,
            agenda_url, minutes_url, video_clip_id)
            VALUES (nextval('seq_meeting_sk'),?,?,?,?,?,?,?,?,?,?,?,?)""",
            [mid, guid, resolve_committee_sk(ccache, body), parse_date(d), parse_time(t),
             loc, sub, ast, mst, aurl, murl, clip])
    print(f"  dim_meeting        {len(rows):>4}")


def build_documents(con):
    """matter attachments (legislation) + meeting docs (meeting). Attachments with no document_id
    still land (keyed by url), matching the ERD's nullable document_id."""
    att = con.execute("""
        SELECT matter_id, document_id, attachment_name, attachment_url
        FROM stg_attachments WHERE attachment_url IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY matter_id, attachment_url
                                   ORDER BY ingest_date DESC, scraped_at DESC)=1
    """).fetchall()
    n_doc = n_br = 0
    for (mid, did, name, url) in att:
        con.execute("""INSERT INTO dim_document (document_sk, document_id, document_source,
            document_title, document_url, scraped_at)
            VALUES (nextval('seq_document_sk'), ?, 'matter_attachment', ?, ?, ?)""",
            [did, name, url, datetime.now()])
        doc_sk = con.execute("SELECT currval('seq_document_sk')").fetchone()[0]
        m = _msk_by_id(con, mid)
        if m and not con.execute("SELECT 1 FROM bridge_matter_document WHERE matter_sk=? AND document_sk=?",
                                 [m, doc_sk]).fetchone():
            con.execute("INSERT INTO bridge_matter_document VALUES (?,?)", [m, doc_sk])
            n_br += 1
        n_doc += 1
    mdocs = con.execute("""
        SELECT meeting_id, document_source, document_title, document_url, body_text
        FROM stg_meeting_documents
        QUALIFY ROW_NUMBER() OVER (PARTITION BY meeting_id, document_source
                                   ORDER BY ingest_date DESC, scraped_at DESC)=1
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
    print(f"  dim_document       {n_doc:>4} | bridges {n_br}")


def build_sponsors(con, pcache):
    rows = con.execute("""
        SELECT matter_id, sponsor_pos, sponsor_name FROM stg_sponsors
        QUALIFY ROW_NUMBER() OVER (PARTITION BY matter_id, sponsor_pos
                                   ORDER BY ingest_date DESC, scraped_at DESC)=1
        ORDER BY matter_id, sponsor_pos
    """).fetchall()
    n = 0
    for (mid, pos, name) in rows:
        m, p = _msk_by_id(con, mid), resolve_person_sk(pcache, name)
        if not m or not p:
            continue
        stype = "Primary" if pos == 0 else "Co"
        if not con.execute("""SELECT 1 FROM bridge_matter_sponsor
                              WHERE matter_sk=? AND person_sk=? AND sponsor_type=?""",
                           [m, p, stype]).fetchone():
            con.execute("INSERT INTO bridge_matter_sponsor VALUES (?,?,?)", [m, p, stype])
            n += 1
    print(f"  bridge_matter_sponsor {n:>2}")


# ── facts (the joint merge) ───────────────────────────────────────────────────
def build_facts(con, ccache, pcache):
    """Meeting-sourced facts first (authoritative, clean meeting_sk); then legislation backfill.
    Distinct source rows are preserved (dedup is per logical row via QUALIFY). Cross-slice suppression
    is SOURCE-BASED (meeting is system-of-record for any (matter, meeting) it recorded), so the same
    physical event is never double-counted even when the two slices normalize its label differently."""
    fma = fma_leg = fv = fv_leg = 0

    # fact_matter_action — meeting (one row per acted agenda item; distinct items preserved)
    items = con.execute("""
        SELECT i.meeting_id, i.matter_file, i.action_raw, i.action_result, i.action_text
        FROM stg_meeting_agenda_items i
        WHERE i.action_raw IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY i.meeting_id, i.item_seq
                                   ORDER BY i.ingest_date DESC, i.scraped_at DESC)=1
    """).fetchall()
    for (mid, mfile, action_raw, result, text) in items:
        msk = _msk_by_file(con, mfile)
        meeting = con.execute("SELECT meeting_sk, meeting_date FROM dim_meeting WHERE meeting_id=?", [mid]).fetchone()
        if not msk or not meeting:
            continue
        con.execute("""INSERT INTO fact_matter_action (matter_action_sk, matter_sk, meeting_sk,
            action_type_code, action_result, action_date, action_text, source)
            VALUES (nextval('seq_matter_action_sk'),?,?,?,?,?,?, 'meeting')""",
            [msk, meeting[0], normalize_action(action_raw).code, result, meeting[1], text])
        fma += 1

    # fact_matter_action — legislation backfill. Suppress (source-based, code-independent) when the
    # meeting slice already recorded this (matter, meeting); if meeting_sk is unresolved, suppress
    # when the meeting recorded the same (matter, date).
    leg = con.execute("""
        SELECT matter_id, body, action, result, action_date_raw
        FROM stg_actions WHERE action IS NOT NULL AND action <> ''
        QUALIFY ROW_NUMBER() OVER (PARTITION BY matter_id, action_seq
                                   ORDER BY ingest_date DESC, scraped_at DESC)=1
    """).fetchall()
    for (mid, body, action, result, adate_raw) in leg:
        msk = _msk_by_id(con, mid)
        if not msk:
            continue
        adate = parse_date(adate_raw)
        meeting_sk = _meeting_sk(con, ccache, body, adate)
        if meeting_sk is not None:
            if con.execute("""SELECT 1 FROM fact_matter_action
                    WHERE matter_sk=? AND meeting_sk=? AND source='meeting'""",
                    [msk, meeting_sk]).fetchone():
                continue
        elif adate is not None and con.execute("""SELECT 1 FROM fact_matter_action
                WHERE matter_sk=? AND action_date=? AND source='meeting'""",
                [msk, adate]).fetchone():
            continue
        con.execute("""INSERT INTO fact_matter_action (matter_action_sk, matter_sk, meeting_sk,
            action_type_code, action_result, action_date, action_text, source)
            VALUES (nextval('seq_matter_action_sk'),?,?,?,?,?,?, 'legislation')""",
            [msk, meeting_sk, normalize_action(action).code, result or None, adate, action])
        fma_leg += 1

    # fact_vote — meeting (one row per matter+meeting+person, per the ERD grain)
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
        meeting = con.execute("SELECT meeting_sk, meeting_date FROM dim_meeting WHERE meeting_id=?", [mid]).fetchone()
        psk = resolve_person_sk(pcache, person)
        if not msk or not meeting or not psk:
            continue
        scope = "board" if _canon_committee(body) == "board of supervisors" else "committee"
        con.execute("""INSERT INTO fact_vote (vote_sk, matter_sk, meeting_sk, person_sk, body_scope,
            vote_date, vote_value, motion_text, source)
            VALUES (nextval('seq_vote_sk'),?,?,?,?,?,?,?, 'meeting')""",
            [msk, meeting[0], psk, scope, meeting[1], normalize_vote(vraw), motion])
        fv += 1

    # fact_vote — legislation backfill (source-based suppression, mirroring fact_matter_action)
    lvotes = con.execute("""
        SELECT v.matter_id, v.person_name, v.vote_value, a.body, a.action_date_raw
        FROM stg_votes v
        JOIN stg_actions a ON a.matter_id=v.matter_id AND a.action_seq=v.action_seq AND a.ingest_date=v.ingest_date
        QUALIFY ROW_NUMBER() OVER (PARTITION BY v.matter_id, a.action_seq, v.person_name
                                   ORDER BY v.ingest_date DESC, v.scraped_at DESC)=1
    """).fetchall()
    for (mid, person, vraw, body, adate_raw) in lvotes:
        msk, psk = _msk_by_id(con, mid), resolve_person_sk(pcache, person)
        if not msk or not psk:
            continue
        adate = parse_date(adate_raw)
        meeting_sk = _meeting_sk(con, ccache, body, adate)
        if meeting_sk is not None:
            if con.execute("""SELECT 1 FROM fact_vote
                    WHERE matter_sk=? AND meeting_sk=? AND source='meeting'""",
                    [msk, meeting_sk]).fetchone():
                continue
        elif adate is not None and con.execute("""SELECT 1 FROM fact_vote
                WHERE matter_sk=? AND person_sk=? AND vote_date=? AND source='meeting'""",
                [msk, psk, adate]).fetchone():
            continue
        scope = "board" if _canon_committee(body) == "board of supervisors" else "committee"
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
    ccache = build_committee_cache(con)
    seed_persons(con)
    pcache = build_person_cache(con)
    build_matters(con, ccache)
    build_meetings(con, ccache)
    build_documents(con)
    build_sponsors(con, pcache)
    build_facts(con, ccache, pcache)
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
