"""Golden tests for the pure HTML parsers — offline, no network, no browser.

Locks the parse contract against the committed fixtures (scrape/tests/fixtures/) so the
concurrency + skip-existing refactor (and any future site-layout drift) can't
silently change what we extract. Only the PURE HTML parsers are tested here; the
enumerate_*/collect Playwright paths are not. scrape_matter's extraction (it fetches
internally) is covered offline in test_scrape_matter.py; the pure scalar helpers
(bucket, _split_names, _meeting_in_window) in test_helpers.py.

Run from the repo root:  pytest scrape/tests/test_parsers.py -v
Counts below were verified against the live-captured fixtures (see fixtures/README.md).
"""

from collections import Counter
from pathlib import Path

from scrape.history_detail import parse_history_detail
from scrape.legistar_meetings import parse_calendar, parse_meeting_detail, _pager_pages, _pager_total

FIX = Path(__file__).parent / "fixtures"


def _html(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- roll-call (votes)
def test_history_committee_three_ayes_with_person_ids():
    votes = parse_history_detail(_html("history_committee_36969551.html")).votes
    assert len(votes) == 3
    assert all(v.vote_value == "Aye" for v in votes)
    # every structural roll-call row must yield a real (digit) PersonId — the matter↔person join key
    assert all(v.person_id and v.person_id.isdigit() for v in votes)
    assert [v.person_name for v in votes] == ["Myrna Melgar", "Chyanne Chen", "Bilal Mahmood"]


def test_history_board_ten_aye_one_excused():
    votes = parse_history_detail(_html("history_board_36861771.html")).votes
    assert len(votes) == 11
    assert Counter(v.vote_value for v in votes) == {"Aye": 10, "Excused": 1}
    assert [v.person_name for v in votes if v.vote_value == "Excused"] == ["Jackie Fielder"]
    assert all(v.person_id and v.person_id.isdigit() for v in votes)


# --------------------------------------------------------------------------- calendar grid
def test_calendar_rows_and_invariants():
    rows = parse_calendar(_html("calendar.html"))
    assert len(rows) == 21
    # GUID is mandatory for every downstream fetch and is uppercased by the parser
    assert all(r.event_guid and r.event_guid == r.event_guid.upper() for r in rows)
    assert all(r.meeting_id and r.meeting_id.isdigit() for r in rows)
    assert sum(r.has_minutes for r in rows) == 13
    assert sum(r.video_clip_id is not None for r in rows) == 13
    # split_subtype coverage (suffix stripped off the location string)
    assert Counter(r.meeting_subtype for r in rows) == {"Regular": 12, "Special": 3, "Recessed": 3, None: 3}


# --------------------------------------------------------------------------- meeting detail
def test_meeting_committee_acted_rows_carry_history_id():
    header, items = parse_meeting_detail(_html("meeting_committee_1422963.html"))
    assert header["body_name"] == "Land Use and Transportation Committee"
    assert header["minutes_status"] == "Final"
    assert len(items) == 2
    # acted rows expose the meeting→fact join key (history_id) and an action label
    assert all(i.action_raw for i in items)
    assert all(i.history_id for i in items)
    assert Counter(i.action_raw for i in items) == {"RECOMMENDED": 1, "CONTINUED": 1}


def test_calendar_pager_parsing():
    # the year-enumeration paginates; these drive the page loop + the "got everything" assertion
    line = "Page 1 of 2, items 1 to 100 of 189."
    assert _pager_pages(line) == 2
    assert _pager_total(line) == 189
    assert _pager_total("Page 1 of 3, items 1 to 100 of 1,234.") == 1234   # comma-grouped total
    assert _pager_pages("no pager — single page") == 1                     # default when absent
    assert _pager_total("no pager — single page") is None


def test_meeting_board_draft_has_no_actions():
    # the gating case: Draft minutes => agenda parsed, but ZERO actions/votes posted yet
    header, items = parse_meeting_detail(_html("meeting_board_1423292.html"))
    assert header["minutes_status"] == "Draft"
    assert len(items) == 50
    assert sum(bool(i.action_raw) for i in items) == 0
    assert sum(bool(i.history_id) for i in items) == 0
