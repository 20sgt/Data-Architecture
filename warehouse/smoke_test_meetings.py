"""smoke_test_meetings.py — offline check for the meeting slice (parsers + meeting-only gold).

No network: parses the committed HTML fixtures with the real scraper parsers, then runs the
bronze JSON -> meeting staging -> unified gold (transform_gold) path on meeting-only data in an
in-memory DuckDB. The cross-slice fact merge is covered separately by smoke_test_gold.py.

Covers:
  A. Parsers vs fixtures (calendar, Final meeting, Draft-gating, roll-calls, novel-literal capture).
  B. action_types contract (comma variant, AS-AMENDED ordering, normalize_vote).
  C. Load + build -> dim_meeting / dim_committee / dim_action_type / dim_document / bridge.
  D. Re-scrape (Draft -> Final): latest ingest wins, still one row.
  E. Doc dedup survives a changed agenda URL.

Run:  python warehouse/smoke_test_meetings.py
"""

import dataclasses
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scrape.action_types import normalize_action, normalize_vote  # noqa: E402
from scrape.legistar_meetings import (  # noqa: E402
    Meeting, parse_calendar, parse_meeting_detail, parse_history, _build_documents,
)
from warehouse.load_meeting_staging import load_partition  # noqa: E402
import warehouse.transform_gold as gold  # noqa: E402

FIX = REPO_ROOT / "scrape" / "fixtures"
_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def assemble_committee_meeting() -> Meeting:
    """Build a Meeting for Land Use 6/15 from fixtures (no network)."""
    cal = {r.meeting_id: r for r in parse_calendar((FIX / "calendar.html").read_text())}["1422963"]
    header, items = parse_meeting_detail((FIX / "meeting_committee_1422963.html").read_text())
    hist = {"36969551": (FIX / "history_committee_36969551.html").read_text()}
    for it in items:
        if it.history_id in hist and it.action_raw:
            a, res, txt, votes = parse_history(hist[it.history_id])
            it.action_text, it.votes = txt, votes
    m = Meeting(
        meeting_id="1422963", event_guid=cal.event_guid, body_name=header["body_name"],
        meeting_date=header["meeting_date"], meeting_time=header["meeting_time"],
        location=header["location"], meeting_subtype=header["meeting_subtype"],
        agenda_status=header["agenda_status"], minutes_status=header["minutes_status"],
        agenda_url=header["agenda_url"], minutes_url=header["minutes_url"],
        video_clip_id=cal.video_clip_id, documents=[], agenda_items=items,
    )
    m.documents = _build_documents(m, with_text=False)
    return m


def write_partition(meetings, root: Path, ingest: str) -> Path:
    out = root / f"ingest_date={ingest}"
    out.mkdir(parents=True, exist_ok=True)
    for m in meetings:
        (out / f"{m.meeting_id}.json").write_text(
            json.dumps(dataclasses.asdict(m), indent=2, ensure_ascii=False))
    return out


def main() -> None:
    print("── A. Parsers vs fixtures ──")
    cal = parse_calendar((FIX / "calendar.html").read_text())
    check("calendar parsed >= 20 rows", len(cal) >= 20, f"got {len(cal)}")
    board62 = next(r for r in cal if r.meeting_id == "1417764")
    check("calendar subtype extracted", board62.meeting_subtype == "Regular", board62.meeting_subtype)
    check("calendar clip id extracted", board62.video_clip_id == "52550", board62.video_clip_id)

    h, items = parse_meeting_detail((FIX / "meeting_committee_1422963.html").read_text())
    check("committee minutes Final", h["minutes_status"] == "Final", h["minutes_status"])
    check("committee subtype Regular", h["meeting_subtype"] == "Regular", h["meeting_subtype"])
    check("clip id recovered from MeetingDetail (no calendar)",
          h["video_clip_id"] == "52626", h["video_clip_id"])
    check("committee has 2 agenda items", len(items) == 2, str(len(items)))
    check("committee item0 file=260422 RECOMMENDED",
          items[0].matter_file == "260422" and items[0].action_raw == "RECOMMENDED")

    _, draft_items = parse_meeting_detail((FIX / "meeting_board_1423292.html").read_text())
    acted = [i for i in draft_items if i.action_raw]
    check("board Draft gating: 50 items, 0 acted", len(draft_items) == 50 and len(acted) == 0,
          f"{len(draft_items)} items / {len(acted)} acted")

    _, _, _, cvotes = parse_history((FIX / "history_committee_36969551.html").read_text())
    check("committee roll-call: 3 Aye w/ PersonId",
          len(cvotes) == 3 and all(v.vote_value == "Aye" and v.person_id for v in cvotes))
    ba, _, _, bvotes = parse_history((FIX / "history_board_36861771.html").read_text())
    dist = {}
    for v in bvotes:
        dist[v.vote_value] = dist.get(v.vote_value, 0) + 1
    check("board roll-call: 10 Aye / 1 Excused", dist == {"Aye": 10, "Excused": 1}, str(dist))

    # Structural detection: a NOVEL literal on a person-linked row is captured (not dropped);
    # a non-person row is ignored.
    synthetic = """<table class="rgMasterTable"><tbody>
        <tr><td><a href="PersonDetail.aspx?ID=999">Jane Doe</a></td><td>Present</td></tr>
        <tr><td>total</td><td></td></tr>
    </tbody></table>"""
    _, _, _, svotes = parse_history(synthetic)
    check("novel vote literal captured verbatim, junk row skipped",
          len(svotes) == 1 and svotes[0].vote_value == "Present" and svotes[0].person_id == "999",
          str([(v.person_name, v.vote_value) for v in svotes]))

    print("── B. action_types contract ──")
    c1 = normalize_action("PASSED, ON FIRST READING").code
    c2 = normalize_action("PASSED ON FIRST READING").code
    check("comma variant collapses to one code", c1 == c2 == "PASSED_BOARD_1ST_READING", f"{c1}/{c2}")
    check("FINALLY PASSED -> 2nd reading",
          normalize_action(ba).code == "PASSED_BOARD_2ND_READING", normalize_action(ba).code)
    check("'ADOPTED AS AMENDED' -> ADOPTED (disposition, not AMENDED)",
          normalize_action("ADOPTED AS AMENDED").code == "ADOPTED")
    check("'APPROVED AS AMENDED' -> APPROVED", normalize_action("APPROVED AS AMENDED").code == "APPROVED")
    check("bare 'AMENDED' -> AMENDED", normalize_action("AMENDED").code == "AMENDED")
    check("normalize_vote No->Nay, Aye->Aye",
          normalize_vote("No") == "Nay" and normalize_vote("Aye") == "Aye")
    check("unknown -> OTHER", normalize_action("FOO BAR").code == "OTHER")

    print("── C. load + transform -> gold ──")
    con = duckdb.connect()
    gold.ensure_schema(con)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "meetings"
        part = write_partition([assemble_committee_meeting()], root, "2026-06-21")
        load_partition(con, part, date(2026, 6, 21))
    gold.build(con)

    n_meet = con.execute("SELECT COUNT(*) FROM dim_meeting").fetchone()[0]
    check("dim_meeting has 1 row", n_meet == 1, str(n_meet))
    row = con.execute("""
        SELECT m.minutes_status, m.video_clip_id, c.committee_name, c.committee_type
        FROM dim_meeting m LEFT JOIN dim_committee c ON c.committee_sk = m.committee_sk
    """).fetchone()
    check("dim_meeting joined to committee + fields",
          row == ("Final", "52626", "Land Use and Transportation Committee", "Standing Committee"),
          str(row))
    n_at = con.execute("SELECT COUNT(*) FROM dim_action_type").fetchone()[0]
    check("dim_action_type seeded (incl OTHER)",
          n_at >= 12 and con.execute(
              "SELECT 1 FROM dim_action_type WHERE action_type_code='OTHER'").fetchone() is not None,
          str(n_at))
    n_doc = con.execute("SELECT COUNT(*) FROM dim_document").fetchone()[0]
    n_br = con.execute("SELECT COUNT(*) FROM bridge_meeting_document").fetchone()[0]
    sources = {r[0] for r in con.execute("SELECT DISTINCT document_source FROM dim_document").fetchall()}
    check("3 meeting docs (agenda/minutes/transcript) + 3 bridges",
          n_doc == 3 and n_br == 3 and sources == {"meeting_agenda", "meeting_minutes", "transcript"},
          f"docs={n_doc} bridges={n_br} sources={sources}")
    n_votes = con.execute("SELECT COUNT(*) FROM stg_meeting_votes").fetchone()[0]
    check("staging captured 3 votes", n_votes == 3, str(n_votes))

    print("── D. re-scrape upsert (Draft -> Final) ──")
    # Earlier partition with the SAME meeting in Draft; transform must keep the latest (Final).
    draft = assemble_committee_meeting()
    draft.minutes_status = "Draft"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "meetings"
        # load the older Draft first (earlier ingest_date), then re-run transform
        part = write_partition([draft], root, "2026-06-10")
        load_partition(con, part, date(2026, 6, 10))
    gold.build(con)
    n_meet2 = con.execute("SELECT COUNT(*) FROM dim_meeting").fetchone()[0]
    latest = con.execute("SELECT minutes_status FROM dim_meeting WHERE meeting_id=1422963").fetchone()[0]
    check("still one dim_meeting row after re-load", n_meet2 == 1, str(n_meet2))
    check("latest scrape wins (Final, not stale Draft)", latest == "Final", latest)

    print("── E. doc dedup survives a changed agenda URL ──")
    moved = assemble_committee_meeting()
    for d in moved.documents:
        if d.document_source == "meeting_agenda":
            d.document_url = d.document_url + "&v=2"   # URL changes on re-scrape
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "meetings"
        part = write_partition([moved], root, "2026-06-28")
        load_partition(con, part, date(2026, 6, 28))
    gold.build(con)
    n_agenda = con.execute(
        "SELECT COUNT(*) FROM dim_document WHERE document_source='meeting_agenda'").fetchone()[0]
    check("exactly one meeting_agenda doc after URL change", n_agenda == 1, str(n_agenda))
    con.close()

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
