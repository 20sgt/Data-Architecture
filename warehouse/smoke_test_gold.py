"""smoke_test_gold.py — offline CROSS-SLICE check of the unified gold build.

Feeds BOTH stagings into one in-memory DuckDB and runs warehouse/transform_gold.py:
  * meeting side: the Land Use 6/15 committee meeting (from fixtures) — matter 260422 RECOMMENDED,
    3 roll-call votes incl. Myrna Melgar (real PersonId 60155); plus matter 260239 CONTINUED.
  * legislation side: a synthetic matter 260422 that OVERLAPS the meeting (same RECOMMENDED action
    on the same committee+date, Melgar Aye, Melgar as sponsor) PLUS a legislation-only REFERRED
    action at the Board on a date with no scraped meeting.

Asserts the joint-merge contract:
  - the overlapping action/vote dedup to ONE row (meeting is system-of-record, source='meeting');
  - the legislation-only action is backfilled with meeting_sk NULL, source='legislation';
  - one shared person (Melgar) = one dim_person row carrying the REAL meeting PersonId;
  - matter 260422 is legislation-sourced, matter 260239 is an html_stub (meeting-only);
  - sponsor bridge + matter attachment land from the legislation side.

Run:  python warehouse/smoke_test_gold.py
"""

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

import warehouse.transform_gold as gold  # noqa: E402
from warehouse.load_meeting_staging import load_partition as load_meetings  # noqa: E402
from warehouse.load_staging import load_partition as load_matters  # noqa: E402
from warehouse.smoke_test_meetings import assemble_committee_meeting, write_partition  # noqa: E402

_failures: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


# A synthetic legislation matter matching legistar_scrape.py's JSON shape, crafted to overlap the
# meeting fixture (file 260422, RECOMMENDED 6/15 Land Use, Melgar Aye) + one legislation-only action.
LEG_MATTER = {
    "file_number": "260422",
    "detail_url": "https://sfgov.legistar.com/LegislationDetail.aspx?ID=7992675&GUID=2DA61E0A-1803-4A",
    "name": "Commemorative Street Name", "title": "Resolution adding the commemorative street name",
    "type": "Resolution", "status": "Passed", "lifecycle": "passed",
    "introduced": "5/1/2026", "on_agenda": None, "final_action": None,
    "enactment_date": None, "enactment_number": None,
    "in_control": "Land Use and Transportation Committee",
    "sponsors": ["Myrna Melgar"], "related_files": [],
    "attachments": [{"name": "Leg Ver1",
                     "url": "https://sfgov.legistar.com/View.ashx?M=F&ID=55501&GUID=ABC"}],
    "actions": [
        # overlaps the meeting — body uses the "&" variant to prove canonical committee matching
        {"date": "6/15/2026", "body": "Land Use & Transportation Committee",
         "action": "RECOMMENDED", "result": "Pass", "history_url": "h1",
         "votes": [{"person": "Myrna Melgar", "value": "Aye"}]},
        {"date": "5/1/2026", "body": "Board of Supervisors",
         "action": "REFERRED", "result": "", "history_url": "h2", "votes": []},     # legislation-only
        # two DISTINCT labels that both normalize to OTHER — must NOT collapse into one row
        {"date": "5/1/2026", "body": "Board of Supervisors",
         "action": "HEARING HELD", "result": "", "history_url": "h3", "votes": []},
        {"date": "", "body": "Board of Supervisors",
         "action": "PUBLIC COMMENT CLOSED", "result": "", "history_url": "h4", "votes": []},  # dateless
    ],
    "full_text": None,
}


def main():
    con = duckdb.connect()
    gold.ensure_schema(con)

    with tempfile.TemporaryDirectory() as tmp:
        # meeting staging
        mpart = write_partition([assemble_committee_meeting()], Path(tmp) / "meetings", "2026-06-21")
        load_meetings(con, mpart, date(2026, 6, 21))
        # legislation staging
        lpart = Path(tmp) / "matters" / "ingest_date=2026-06-21"
        lpart.mkdir(parents=True, exist_ok=True)
        (lpart / "260422.json").write_text(json.dumps(LEG_MATTER))
        load_matters(con, lpart, date(2026, 6, 21))

    gold.build(con)

    print("── cross-slice gold assertions ──")
    # matter provenance
    src_422 = con.execute("SELECT matter_source, matter_id FROM dim_matter WHERE matter_file='260422'").fetchone()
    check("matter 260422 is legislation-sourced (matter_id from URL)",
          src_422 == ("legislation", 7992675), str(src_422))
    src_239 = con.execute("SELECT matter_source FROM dim_matter WHERE matter_file='260239'").fetchone()
    check("matter 260239 is an html_stub (meeting-only)", src_239 == ("html_stub",), str(src_239))

    # shared person -> one row, real PersonId
    melgar = con.execute("SELECT COUNT(*), MIN(person_id) FROM dim_person WHERE full_name='Myrna Melgar'").fetchone()
    check("Myrna Melgar = 1 dim_person row w/ real PersonId 60155", melgar == (1, 60155), str(melgar))

    # fact_matter_action dedup: RECOMMENDED on 260422 appears ONCE, attributed to the meeting
    rec = con.execute("""
        SELECT COUNT(*), MIN(source), MIN(meeting_sk) IS NOT NULL
        FROM fact_matter_action fa JOIN dim_matter m ON m.matter_sk=fa.matter_sk
        WHERE m.matter_file='260422' AND fa.action_type_code='RECOMMENDED'
    """).fetchone()
    check("RECOMMENDED(260422) deduped to 1 row, source=meeting, meeting_sk set",
          rec == (1, "meeting", True), str(rec))

    # legislation-only REFERRED backfilled with NULL meeting_sk
    ref = con.execute("""
        SELECT COUNT(*), MIN(source), BOOL_AND(meeting_sk IS NULL)
        FROM fact_matter_action fa JOIN dim_matter m ON m.matter_sk=fa.matter_sk
        WHERE m.matter_file='260422' AND fa.action_type_code='REFERRED'
    """).fetchone()
    check("REFERRED(260422) backfilled: 1 row, source=legislation, meeting_sk NULL",
          ref == (1, "legislation", True), str(ref))

    # two DISTINCT labels normalizing to OTHER must NOT collapse (review finding #1)
    n_other = con.execute("""
        SELECT COUNT(*) FROM fact_matter_action fa JOIN dim_matter m ON m.matter_sk=fa.matter_sk
        WHERE m.matter_file='260422' AND fa.action_type_code='OTHER'
    """).fetchone()[0]
    check("two distinct OTHER actions preserved (not collapsed)", n_other == 2, str(n_other))

    # fact_vote dedup: Melgar on 260422 appears once (the overlapping legislation vote is dropped)
    mv = con.execute("""
        SELECT COUNT(*), MIN(source), MIN(body_scope)
        FROM fact_vote v JOIN dim_matter m ON m.matter_sk=v.matter_sk JOIN dim_person p ON p.person_sk=v.person_sk
        WHERE m.matter_file='260422' AND p.full_name='Myrna Melgar'
    """).fetchone()
    check("Melgar vote on 260422 deduped to 1 row, source=meeting, committee scope",
          mv == (1, "meeting", "committee"), str(mv))
    total_votes = con.execute("SELECT COUNT(*) FROM fact_vote").fetchone()[0]
    check("fact_vote total = 3 (no legislation duplicate added)", total_votes == 3, str(total_votes))

    # sponsor + attachment from the legislation side
    spons = con.execute("""
        SELECT s.sponsor_type FROM bridge_matter_sponsor s
        JOIN dim_matter m ON m.matter_sk=s.matter_sk JOIN dim_person p ON p.person_sk=s.person_sk
        WHERE m.matter_file='260422' AND p.full_name='Myrna Melgar'
    """).fetchone()
    check("Melgar is Primary sponsor of 260422", spons == ("Primary",), str(spons))
    n_att = con.execute("SELECT COUNT(*) FROM dim_document WHERE document_source='matter_attachment'").fetchone()[0]
    check("matter attachment landed (Leg Ver1)", n_att == 1, str(n_att))
    # meeting docs still present
    n_mdocs = con.execute("SELECT COUNT(*) FROM dim_document WHERE document_source LIKE 'meeting%' OR document_source='transcript'").fetchone()[0]
    check("meeting docs present (agenda/minutes/transcript)", n_mdocs == 3, str(n_mdocs))

    # idempotency: re-load both stagings into a LATER partition (incl. the dateless action) and
    # rebuild — counts must be identical (review finding #2: no dup on rescrape / NULL-date dedup).
    fma0 = con.execute("SELECT COUNT(*) FROM fact_matter_action").fetchone()[0]
    fv0 = con.execute("SELECT COUNT(*) FROM fact_vote").fetchone()[0]
    with tempfile.TemporaryDirectory() as tmp:
        mpart = write_partition([assemble_committee_meeting()], Path(tmp) / "meetings", "2026-07-01")
        load_meetings(con, mpart, date(2026, 7, 1))
        lpart = Path(tmp) / "matters" / "ingest_date=2026-07-01"
        lpart.mkdir(parents=True, exist_ok=True)
        (lpart / "260422.json").write_text(json.dumps(LEG_MATTER))
        load_matters(con, lpart, date(2026, 7, 1))
    gold.build(con)
    fma1 = con.execute("SELECT COUNT(*) FROM fact_matter_action").fetchone()[0]
    fv1 = con.execute("SELECT COUNT(*) FROM fact_vote").fetchone()[0]
    check("idempotent rebuild: fact_matter_action count stable", fma0 == fma1, f"{fma0}->{fma1}")
    check("idempotent rebuild: fact_vote count stable", fv0 == fv1, f"{fv0}->{fv1}")

    con.close()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        sys.exit(1)
    print("All cross-slice checks passed.")


if __name__ == "__main__":
    main()
