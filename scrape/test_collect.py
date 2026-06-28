"""Checks for the concurrency + resumability plumbing (fetch rate gate, matter resume logic).

Offline, no network, no browser. The golden parser tests (test_parsers.py) prove extraction is
unchanged; these prove the collect()/fetch() machinery behaves.
"""

import time
import threading
from datetime import date

from scrape import fetch
import scrape.legistar_scrape as ls
from scrape.legistar_scrape import _matter_id, _done_matter_ids


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
    assert len(ids) == 600                                            # 10*60, fully recovered past the 100 cap


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
