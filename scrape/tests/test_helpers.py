"""Unit tests for the pure scalar helpers — offline, no network, no browser, no fixtures.

These are the small deterministic transforms that feed gold but had no coverage:
  * bucket()             — status string -> lifecycle stage (a derived gold column)
  * _split_names()       — sponsor / related-file string -> list (handles ; , and " and ")
  * _meeting_in_window() — calendar date string -> in/out of a [start, end] window

Run from the repo root:  pytest scrape/tests/test_helpers.py -v
"""

from datetime import date

from scrape.legistar_scrape import bucket, _split_names
from scrape.legistar_meetings import _meeting_in_window


# --------------------------------------------------------------------------- bucket (lifecycle)
def test_bucket_passed_in_works_and_other():
    # every PASSED literal must classify as "passed" — so a typo in the source set is caught, not silent
    for s in ("Passed", "Approved", "Adopted", "Finally Passed", "Ordinance Enacted", "Mayor Approved"):
        assert bucket(s) == "passed", s
    assert bucket("  Ordinance Enacted ") == "passed"          # trimmed + lowercased before lookup
    # representative IN_WORKS literals (flat set lookup; a sample proves the branch + normalization)
    for s in ("In Committee", "Continued", "First Reading", "Assigned"):
        assert bucket(s) == "in_works", s
    assert bucket("Filed") == "other"                          # closed WITHOUT passage -> not "passed"
    assert bucket("") == "other"                               # unknown / empty falls through
    assert bucket("some brand new status") == "other"


# --------------------------------------------------------------------------- _split_names
def test_split_names_separators_and_blanks():
    assert _split_names(None) == []
    assert _split_names("") == []
    assert _split_names("Melgar") == ["Melgar"]
    # the three separators the site mixes: comma, semicolon, and the literal " and "
    assert _split_names("Melgar, Chen and Mahmood") == ["Melgar", "Chen", "Mahmood"]
    assert _split_names("Walton; Preston; Safai") == ["Walton", "Preston", "Safai"]
    # trailing/empty fragments (e.g. "A, " or "A,,B") are dropped, not emitted as ""
    assert _split_names("Dorsey, ") == ["Dorsey"]
    assert _split_names("A,,B") == ["A", "B"]


# --------------------------------------------------------------------------- _meeting_in_window
def test_meeting_in_window_bounds_and_bad_dates():
    s, e = date(2026, 6, 11), date(2026, 6, 25)
    assert _meeting_in_window("6/16/2026", s, e) is True
    assert _meeting_in_window("6/11/2026", s, e) is True       # inclusive lower bound
    assert _meeting_in_window("6/25/2026", s, e) is True       # inclusive upper bound
    assert _meeting_in_window("6/10/2026", s, e) is False      # before window
    assert _meeting_in_window("6/26/2026", s, e) is False      # after window
    # no window set -> everything passes (the un-filtered enumeration path)
    assert _meeting_in_window("6/16/2026", None, None) is True
    # blank / unparseable dates are EXCLUDED when a window is active (never crash the filter)
    assert _meeting_in_window(None, s, e) is False
    assert _meeting_in_window("not a date", s, e) is False
    # one-sided windows
    assert _meeting_in_window("6/16/2026", s, None) is True
    assert _meeting_in_window("6/10/2026", s, None) is False
