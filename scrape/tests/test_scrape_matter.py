"""Offline coverage for scrape_matter — the legislation extraction contract.

scrape_matter() fetches internally (the LegislationDetail page + one HistoryDetail page per acted
row), so it can't be a pure-parser golden test like the meeting slice's parse_meeting_detail. We
monkeypatch the module's `get` to serve fixtures instead: a synthetic LegislationDetail page
(authored here — see fixtures/legislation_260422.html) whose two action rows point at the two REAL
captured HistoryDetail fixtures. This locks label extraction, attachment capture, sponsor splitting,
the radopen -> history_id wiring, and the per-action vote attachment — all offline, no network.

Run from the repo root:  pytest scrape/tests/test_scrape_matter.py -v
"""

from pathlib import Path

import scrape.legistar_scrape as ls

FIX = Path(__file__).parent / "fixtures"

# history_id -> the real captured HistoryDetail fixture the synthetic legislation page links to.
_HISTORY = {
    "36969551": "history_committee_36969551.html",   # 3x Aye
    "36861771": "history_board_36861771.html",        # 10x Aye, 1x Excused
}


def _fake_get(url: str) -> str:
    if "LegislationDetail" in url:
        return (FIX / "legislation_260422.html").read_text(encoding="utf-8")
    for hid, fname in _HISTORY.items():
        if f"HistoryDetail.aspx?ID={hid}" in url:
            return (FIX / fname).read_text(encoding="utf-8")
    raise AssertionError(f"unexpected fetch in test (no live network may leak in): {url}")


def test_scrape_matter_extracts_metadata_actions_and_wires_votes(monkeypatch):
    monkeypatch.setattr(ls, "get", _fake_get)
    m = ls.scrape_matter("https://x/LegislationDetail.aspx?ID=7423230&GUID=g")

    # header metadata read from the value labels
    assert m.file_number == "260422"
    assert m.type == "Resolution"
    assert m.status == "Passed"
    assert m.lifecycle == "passed"                 # derived via bucket() off status
    assert m.enactment_number == "R-0123-26"
    assert m.in_control == "Land Use and Transportation Committee"

    # sponsor / related-file splitting (feeds the dim_matter bridges) — the ", " + " and " mix
    assert m.sponsors == ["Myrna Melgar", "Chyanne Chen", "Bilal Mahmood"]
    assert m.related_files == ["260100", "260200"]

    # attachments captured in document order, with the FULL href (query tail intact), not just BASE
    assert [a.name for a in m.attachments] == ["Leg Ver1", "Referral FYI"]
    assert [a.url for a in m.attachments] == [
        ls.BASE + "View.ashx?M=F&ID=9001&GUID=AAAA1111-2222-3333-4444-555566667777",
        ls.BASE + "View.ashx?M=F&ID=9002&GUID=BBBB1111-2222-3333-4444-555566667777",
    ]

    # two acted rows, each carrying its history_id (the meeting<->fact join key) + body/label/result.
    # body + action + result are read from distinct columns (cells[2]/[3]/[4]) — asserting all three
    # positionally pins column integrity, so a one-column shift in the history grid is caught.
    assert len(m.actions) == 2
    assert [a.history_id for a in m.actions] == ["36969551", "36861771"]
    assert [a.body for a in m.actions] == ["Land Use and Transportation Committee", "Board of Supervisors"]
    assert [a.action for a in m.actions] == ["RECOMMENDED", "FINALLY PASSED"]
    assert [a.result for a in m.actions] == ["Pass", "Pass"]

    # each row's roll-call votes attached from ITS HistoryDetail page (the wiring under test)
    by_id = {a.history_id: a for a in m.actions}
    assert len(by_id["36969551"].votes) == 3
    assert all(v.vote_value == "Aye" for v in by_id["36969551"].votes)
    assert len(by_id["36861771"].votes) == 11
    assert sum(v.vote_value == "Excused" for v in by_id["36861771"].votes) == 1


def test_scrape_matter_no_history_grid_yields_zero_actions(monkeypatch):
    # control-id drift / a page with no history grid -> emit with 0 actions, never crash
    monkeypatch.setattr(ls, "get", lambda url: "<html><body>no grid, no labels</body></html>")
    m = ls.scrape_matter("https://x/LegislationDetail.aspx?ID=1&GUID=g")
    assert m.actions == []
    assert m.file_number is None
    assert m.attachments == []
