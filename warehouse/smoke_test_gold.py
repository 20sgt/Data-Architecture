"""smoke_test_gold.py — offline CROSS-SLICE check of the unified gold build.

Feeds BOTH stagings into one in-memory DuckDB and runs warehouse/transform_gold.py, then asserts the
joint-merge contract. The legislation fixture is crafted to overlap the meeting fixture (Land Use 6/15
committee meeting, matter 260422) and exercise the cross-slice dedup, which is keyed on the
MatterHistory id (history_id) shared by both slices:

  - SAME history entry under a DIFFERENT code (APPROVED vs the meeting's RECOMMENDED) -> deduped;
  - DISTINCT history entry at the same meeting (incl. a voter the roll-call missed) -> preserved;
  - committee name variants (NBSP, '&'/'and') collapse to one committee;
  - matter 260422 is legislation-sourced, 260239 is an html_stub; sponsor + attachment land;
  - re-load + rebuild is idempotent (stable counts).

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
from warehouse.transform_gold import _canon_committee  # noqa: E402
from warehouse.load_meeting_staging import load_partition as load_meetings  # noqa: E402
from warehouse.load_staging import load_partition as load_matters  # noqa: E402
from warehouse.smoke_test_meetings import assemble_committee_meeting, write_partition  # noqa: E402

_failures: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


# Synthetic legislation matter (legistar_scrape.py JSON shape). history_url IDs are the cross-slice
# key; the meeting fixture's item 260422 has history_id 36969551.
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
        # SAME history entry as the meeting's RECOMMENDED (36969551) but a DIFFERENT code (APPROVED):
        # exact history-id dedup must drop the action AND its overlapping Melgar vote.
        {"date": "6/15/2026", "body": "Land Use & Transportation Committee", "action": "APPROVED",
         "result": "Pass", "history_url": "https://sfgov.legistar.com/HistoryDetail.aspx?ID=36969551&GUID=x",
         "votes": [{"person": "Myrna Melgar", "value": "Aye"}]},
        # DISTINCT history entry (88888888) the meeting never recorded, at the SAME meeting: must be
        # KEPT, including a voter the meeting roll-call missed.
        {"date": "6/15/2026", "body": "Land Use & Transportation Committee", "action": "AMENDED",
         "result": "Pass", "history_url": "https://sfgov.legistar.com/HistoryDetail.aspx?ID=88888888&GUID=x",
         "votes": [{"person": "Some Other Supervisor", "value": "Aye"}]},
        {"date": "5/1/2026", "body": "Board of Supervisors", "action": "REFERRED", "result": "",
         "history_url": "https://sfgov.legistar.com/HistoryDetail.aspx?ID=77777777&GUID=x", "votes": []},
        # two DISTINCT labels that both normalize to OTHER: must NOT collapse into one row
        {"date": "5/1/2026", "body": "Board of Supervisors", "action": "HEARING HELD", "result": "",
         "history_url": "https://sfgov.legistar.com/HistoryDetail.aspx?ID=66666666&GUID=x", "votes": []},
        {"date": "", "body": "Board of Supervisors", "action": "PUBLIC COMMENT CLOSED", "result": "",
         "history_url": "https://sfgov.legistar.com/HistoryDetail.aspx?ID=55555555&GUID=x", "votes": []},
    ],
    "full_text": None,
}


def main():
    # Inject a non-breaking space into the overlapping Land Use action bodies (chr(160), to avoid a
    # literal NBSP in source) — exercises the canonicalizer end-to-end on the regression input.
    nbsp_body = "Land Use" + chr(160) + "& Transportation Committee"
    LEG_MATTER["actions"][0]["body"] = nbsp_body
    LEG_MATTER["actions"][1]["body"] = nbsp_body
    check("_canon_committee collapses NBSP + '&'",
          _canon_committee(nbsp_body) == "land use and transportation committee",
          _canon_committee(nbsp_body))

    con = duckdb.connect()
    gold.ensure_schema(con)

    with tempfile.TemporaryDirectory() as tmp:
        mpart = write_partition([assemble_committee_meeting()], Path(tmp) / "meetings", "2026-06-21")
        load_meetings(con, mpart, date(2026, 6, 21))
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

    # the meeting's RECOMMENDED on 260422 stands (its history entry is system-of-record)
    rec = con.execute("""
        SELECT COUNT(*), MIN(source), MIN(meeting_sk) IS NOT NULL
        FROM fact_matter_action fa JOIN dim_matter m ON m.matter_sk=fa.matter_sk
        WHERE m.matter_file='260422' AND fa.action_type_code='RECOMMENDED'
    """).fetchone()
    check("RECOMMENDED(260422) = 1 row, source=meeting, meeting_sk set", rec == (1, "meeting", True), str(rec))

    # SAME history entry, divergent code (APPROVED) -> deduped by history_id, not double-counted
    n_appr = con.execute("""
        SELECT COUNT(*) FROM fact_matter_action fa JOIN dim_matter m ON m.matter_sk=fa.matter_sk
        WHERE m.matter_file='260422' AND fa.action_type_code='APPROVED'
    """).fetchone()[0]
    check("same-history-entry divergent code (APPROVED) deduped by history_id", n_appr == 0, str(n_appr))

    # DISTINCT history entry (AMENDED) the meeting never recorded -> preserved (not over-suppressed)
    amd = con.execute("""
        SELECT COUNT(*), MIN(source) FROM fact_matter_action fa JOIN dim_matter m ON m.matter_sk=fa.matter_sk
        WHERE m.matter_file='260422' AND fa.action_type_code='AMENDED'
    """).fetchone()
    check("distinct history entry (AMENDED) preserved, source=legislation", amd == (1, "legislation"), str(amd))

    # legislation-only REFERRED backfilled with NULL meeting_sk
    ref = con.execute("""
        SELECT COUNT(*), MIN(source), BOOL_AND(meeting_sk IS NULL)
        FROM fact_matter_action fa JOIN dim_matter m ON m.matter_sk=fa.matter_sk
        WHERE m.matter_file='260422' AND fa.action_type_code='REFERRED'
    """).fetchone()
    check("REFERRED(260422) backfilled: 1 row, source=legislation, meeting_sk NULL",
          ref == (1, "legislation", True), str(ref))

    # two DISTINCT labels normalizing to OTHER must NOT collapse
    n_other = con.execute("""
        SELECT COUNT(*) FROM fact_matter_action fa JOIN dim_matter m ON m.matter_sk=fa.matter_sk
        WHERE m.matter_file='260422' AND fa.action_type_code='OTHER'
    """).fetchone()[0]
    check("two distinct OTHER actions preserved (not collapsed)", n_other == 2, str(n_other))

    # NBSP + '&' committee variants across slices collapse to ONE committee
    n_comm = con.execute("SELECT COUNT(*) FROM dim_committee").fetchone()[0]
    check("committee name variants (NBSP/&) collapse to 1 Land Use + 1 Board", n_comm == 2, str(n_comm))

    # fact_vote: Melgar (same history entry + person) deduped to the meeting row
    mv = con.execute("""
        SELECT COUNT(*), MIN(source), MIN(body_scope)
        FROM fact_vote v JOIN dim_matter m ON m.matter_sk=v.matter_sk JOIN dim_person p ON p.person_sk=v.person_sk
        WHERE m.matter_file='260422' AND p.full_name='Myrna Melgar'
    """).fetchone()
    check("Melgar vote on 260422 deduped to 1 row, source=meeting, committee scope",
          mv == (1, "meeting", "committee"), str(mv))

    # a voter the meeting roll-call MISSED (distinct history entry) must survive
    other_voter = con.execute("""
        SELECT COUNT(*), MIN(source) FROM fact_vote v
        JOIN dim_person p ON p.person_sk=v.person_sk
        WHERE p.full_name='Some Other Supervisor'
    """).fetchone()
    check("distinct voter the meeting missed preserved, source=legislation",
          other_voter == (1, "legislation"), str(other_voter))

    total_votes = con.execute("SELECT COUNT(*) FROM fact_vote").fetchone()[0]
    check("fact_vote total = 4 (3 meeting + 1 distinct legislation voter)", total_votes == 4, str(total_votes))

    # sponsor + attachment from the legislation side
    spons = con.execute("""
        SELECT s.sponsor_type FROM bridge_matter_sponsor s
        JOIN dim_matter m ON m.matter_sk=s.matter_sk JOIN dim_person p ON p.person_sk=s.person_sk
        WHERE m.matter_file='260422' AND p.full_name='Myrna Melgar'
    """).fetchone()
    check("Melgar is Primary sponsor of 260422", spons == ("Primary",), str(spons))
    n_att = con.execute("SELECT COUNT(*) FROM dim_document WHERE document_source='matter_attachment'").fetchone()[0]
    check("matter attachment landed (Leg Ver1)", n_att == 1, str(n_att))
    n_mdocs = con.execute("SELECT COUNT(*) FROM dim_document WHERE document_source LIKE 'meeting%' OR document_source='transcript'").fetchone()[0]
    check("meeting docs present (agenda/minutes/transcript)", n_mdocs == 3, str(n_mdocs))

    # idempotency: re-load both stagings into a LATER partition and rebuild — counts must be identical.
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
