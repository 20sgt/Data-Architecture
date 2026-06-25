"""Shared HistoryDetail page parser — the SINGLE roll-call/action parser for BOTH slices.

A Legistar `HistoryDetail.aspx` page is reached identically from both crawl paths (the meeting
slice via a gridMain agenda row's `radopen()` link; the legislation slice via a LegislationDetail
history-grid `radopen()` link), and both slices need the SAME thing from it: the action label /
result / motion text and the per-member roll-call. Before this module each slice had its own
parser and they had drifted:

  * the meeting parser detected vote rows STRUCTURALLY (a PersonDetail link in the row), captured
    the PersonId, used the LAST cell as the vote value, and warned-but-KEPT unrecognized literals;
  * the legislation parser used a value WHITELIST (`tds[1] in (...)`), so it silently DROPPED any
    literal outside that set, captured no PersonId, and scanned every `<table>` on the page.

This module keeps the meeting (structural) behavior as the single source of truth, so the
legislation slice now gets real PersonIds for free and can never silently drop a vote. Companion to
`scrape/action_types.py`: that module is the one label->code / vote-normalization authority, this
one is the one HistoryDetail parser.

Normalization deliberately does NOT happen here. The raw action label and raw vote literal are
ALWAYS preserved verbatim; canonicalization to `action_type_code` / `vote_value` is the transform's
job (via `scrape/action_types.py`), so there is exactly one normalization authority and the raw
value stays recoverable in bronze.
"""

from __future__ import annotations

import re
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple

from bs4 import BeautifulSoup

log = logging.getLogger("legistar-history")

# ASP.NET control-id prefix on the value labels (lblAction / lblResult / lblActionText).
CP = "ctl00_ContentPlaceHolder1_"

# Roll-call literals confirmed/expected on the HistoryDetail gridVote (Aye/Excused confirmed live;
# the rest expected). Used ONLY to (a) flag a novel literal for review and (b) recognise a vote row
# that has no PersonDetail link — NEVER as a capture filter, so an unanticipated literal on a real
# (person-linked) row is captured and warned, never silently dropped.
KNOWN_VOTE_LITERALS = {"Aye", "No", "Nay", "Absent", "Excused", "Recused", "Present"}

_PERSON_HREF = re.compile(r"PersonDetail\.aspx", re.I)
_PERSON_ID = re.compile(r"[?&]ID=(\d+)", re.I)


@dataclass
class Vote:
    """One per-member roll-call vote. This is the bronze JSON shape shared by BOTH slices.

    `vote_value` is the RAW site literal (e.g. "No"); `normalize_vote()` ("No" -> "Nay") runs in the
    transform, so the raw literal stays recoverable downstream.
    """
    person_id: str | None        # PersonId from PersonDetail.aspx?ID= (robust join key; None if absent)
    person_name: str
    vote_value: str              # raw literal: Aye | No | Excused | Absent | Recused | Present | ...


class ParsedHistory(NamedTuple):
    """A parsed HistoryDetail page. A NamedTuple so the existing
    `action, result, action_text, votes = parse_history_detail(...)` unpacking keeps working, while
    callers that only want the roll-call can read `.votes`."""
    action_raw: str | None
    action_result: str | None
    action_text: str | None
    votes: list[Vote]


def _txt(el) -> str | None:
    if el is None:
        return None
    t = el.get_text(" ", strip=True).replace("\xa0", " ").strip()
    return t or None


def _lbl(soup: BeautifulSoup, name: str) -> str | None:
    """Text of the value label ctl00_ContentPlaceHolder1_<name> (suffix match tolerant)."""
    el = soup.find(id=f"{CP}{name}") or soup.find(id=re.compile(rf"{name}$"))
    return _txt(el)


def parse_history_detail(html: str) -> ParsedHistory:
    """Parse a HistoryDetail page -> (action_raw, action_result, action_text, votes). Pure (no network).

    A vote row is detected STRUCTURALLY: a PersonDetail link in the first cell (every real roll-call
    row has one) — or, as a fallback for a row with no link, a KNOWN literal in the value cell. The
    value is NEVER used as a capture filter, so an unrecognized literal on a real (person-linked) row
    is captured (and warned), never silently dropped. The vote value is the LAST cell, so an extra
    column (e.g. a district) never displaces it.
    """
    soup = BeautifulSoup(html, "lxml")
    action = _lbl(soup, "lblAction")
    result = _lbl(soup, "lblResult")
    action_text = _lbl(soup, "lblActionText")

    votes: list[Vote] = []
    grid = soup.select_one("table.rgMasterTable")
    if grid:
        # Prefer `tbody tr` (the Telerik RadGrid renders an explicit <tbody>), but fall back to a
        # direct `tr` scan when it doesn't: lxml does NOT synthesize a <tbody>, so `tbody tr` alone
        # would silently drop the WHOLE roll-call on any no-<tbody> variant. The per-row structural
        # filter below (PersonDetail link / known literal) already rejects header/total rows, so the
        # broader `tr` scan never over-captures.
        for tr in (grid.select("tbody tr") or grid.select("tr")):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            person = _txt(tds[0])
            value = _txt(tds[-1])
            a = tds[0].find("a", href=_PERSON_HREF)
            pid = _PERSON_ID.search(a["href"]) if a and a.get("href") else None
            is_vote_row = bool(a) or (person and value in KNOWN_VOTE_LITERALS)
            if not is_vote_row or not value:
                continue
            if value not in KNOWN_VOTE_LITERALS:
                log.warning("unrecognized vote literal %r for %r — capturing verbatim", value, person)
            votes.append(Vote(
                person_id=pid.group(1) if pid else None,
                person_name=person or "",
                vote_value=value,            # raw literal; normalized (No->Nay) in the transform
            ))
    return ParsedHistory(action, result, action_text, votes)


def fetch_history_detail(url: str, get: Callable[[str], str]) -> ParsedHistory:
    """Fetch `url` with the caller's HTTP getter, then parse it.

    Used by the legislation slice (legistar_scrape.scrape_matter), which passes its own `_get`. The
    getter is INJECTED so that slice's fetch policy (session, rate-limit, retries) stays in the
    scraper and this module holds no HTTP policy — which also makes it trivially unit-testable with a
    fake getter. (The meeting slice fetches separately and calls `parse_history_detail` directly.)
    """
    return parse_history_detail(get(url))
