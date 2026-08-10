"""Checks for the concurrency + resumability plumbing (fetch rate gate, matter resume logic).

Offline, no network, no browser. The golden parser tests (test_parsers.py) prove extraction is
unchanged; these prove the collect()/fetch() machinery behaves.
"""

import json
import time
import threading
from datetime import date

from scrape import fetch
import scrape.legistar_scrape as ls
from scrape.legistar_scrape import _matter_id, _done_matter_ids, read_agenda_matter_urls


def test_matter_id_extracts_id():
    assert _matter_id("https://x/LegislationDetail.aspx?ID=7423230&GUID=abc") == "7423230"
    assert _matter_id("https://x/LegislationDetail.aspx?Foo=1&ID=42&GUID=g") == "42"
    assert _matter_id("https://x/no-id-here") is None


def test_done_matter_ids_reads_back_ids(tmp_path):
    # output files are named by file_number; resume must recover matter_id from each detail_url
    (tmp_path / "250630.json").write_text(
        '{"file_number": "250630", "detail_url": "https://x/LegislationDetail.aspx?ID=7423230&GUID=g"}')
    (tmp_path / "260001.json").write_text(
        '{"file_number": "260001", "detail_url": "https://x/LegislationDetail.aspx?ID=99&GUID=g"}')
    (tmp_path / "garbage.json").write_text("{ not valid json")          # unreadable -> skipped, not fatal
    assert _done_matter_ids(tmp_path) == {"7423230", "99"}
    assert _done_matter_ids(tmp_path / "missing") == set()             # absent dir -> empty, no crash


def test_read_agenda_matter_urls_dedups_and_preserves_order(tmp_path):
    # the discovery feed: distinct LegislationDetail URLs across a meeting-bronze dir, ORDER-PRESERVING.
    # Expected order (C, A, B) is deliberately NOT sorted, so a stray sorted() can't pass by coincidence:
    # files read in name order (1000 then 1001); within a file, agenda-item order; dups dropped on first.
    U = "https://x/LegislationDetail.aspx?ID="
    (tmp_path / "1000.json").write_text(json.dumps({"agenda_items": [
        {"matter_url": U + "C"},                                       # first file, first item -> head
        {"matter_url": U + "A"},
        {"matter_url": ""},                                            # blank -> excluded
        {"agenda_number": "1"},                                        # no matter_url key -> excluded
    ]}))
    (tmp_path / "1001.json").write_text(json.dumps({"agenda_items": [
        {"matter_url": U + "A"},                                       # dup across files -> deduped
        {"matter_url": U + "B"},                                       # later file sorts BEFORE C above
    ]}))
    (tmp_path / "_index.json").write_text(json.dumps({"agenda_items": [
        {"matter_url": U + "SKIP"}]}))                                 # _index.json -> skipped
    (tmp_path / "garbage.json").write_text("{ not valid json")         # unreadable -> skipped, not fatal

    assert read_agenda_matter_urls(tmp_path) == [U + "C", U + "A", U + "B"]  # insertion order, deduped
    assert read_agenda_matter_urls(tmp_path / "missing") == []         # absent dir -> empty, no crash


def test_enumerate_window_bisects_past_the_cap(monkeypatch):
    # the live search returns <= RESULT_CAP rows and hides the overflow; bisection must recover ALL
    # matters in a dense window. Fake universe: 60 matters/day over 10 days (any multi-day span > cap).
    days = [date(2026, 1, 1 + i) for i in range(10)]
    universe = {d: [f"https://x/LegislationDetail.aspx?ID={i*100+j}" for j in range(60)]
                for i, d in enumerate(days)}

    def fake_search(start, end, page):
        out = [u for d, urls in universe.items() if start <= d <= end for u in urls]
        return out[:ls.RESULT_CAP]                                     # the real search caps here

    monkeypatch.setattr(ls, "enumerate_matters", fake_search)
    got = ls._enumerate_window(days[0], days[-1], page=None)
    ids = {_matter_id(u) for u in got}
    assert len(got) == 600          # no overlap: bisection's disjoint halves never double-count a matter
    assert len(ids) == 600          # 10*60 distinct, fully recovered past the 100 cap


def test_throttle_enforces_aggregate_rate(monkeypatch):
    # the gate must space request STARTS by >= RATE_LIMIT_S across ALL threads (the aggregate ceiling
    # that keeps load on sfgov bounded no matter how many workers run)
    monkeypatch.setattr(fetch, "RATE_LIMIT_S", 0.05)
    fetch._next_request_at = 0.0                                       # reset the gate
    n = 6
    t0 = time.monotonic()
    threads = [threading.Thread(target=fetch._throttle) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - t0
    assert elapsed >= (n - 1) * 0.05 * 0.9                             # n slots spaced by the interval
