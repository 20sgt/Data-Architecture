"""HistoryDetail page parser — the per-member roll-call parser for the legislation slice.

A Legistar `HistoryDetail.aspx` page is reached from the legislation crawl via a `radopen()` link
and provides the per-member roll-call. Vote rows are detected STRUCTURALLY (a PersonDetail link),
which captures the real PersonId and never silently drops an unrecognized literal. Pure (no network):
the caller fetches the page and passes the HTML in.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from typing import NamedTuple

from bs4 import BeautifulSoup

log = logging.getLogger("legistar-history")

# Roll-call literals confirmed/expected on the HistoryDetail gridVote (Aye/Excused confirmed live;
# the rest expected). Used ONLY to (a) flag a novel literal for review and (b) recognise a vote row
# that has no PersonDetail link — NEVER as a capture filter, so an unanticipated literal on a real
# (person-linked) row is captured and warned, never silently dropped.
KNOWN_VOTE_LITERALS = {"Aye", "No", "Nay", "Absent", "Excused", "Recused", "Present"}

_PERSON_HREF = re.compile(r"PersonDetail\.aspx", re.I)
_PERSON_ID = re.compile(r"[?&]ID=(\d+)", re.I)


@dataclass
class Vote:
    """One per-member roll-call vote — the bronze JSON shape the legislation slice emits.

    `vote_value` is the RAW site literal (e.g. "No")
    """
    person_id: str | None        # PersonId from PersonDetail.aspx?ID= (robust join key; None if absent)
    person_name: str
    vote_value: str              # raw literal: Aye | No | Excused | Absent | Recused | Present | ...


class ParsedHistory(NamedTuple):
    """A parsed HistoryDetail page. Callers read `.votes` (the per-member roll-call)."""
    votes: list[Vote]


def _txt(el) -> str | None:
    if el is None:
        return None
    t = el.get_text(" ", strip=True).replace("\xa0", " ").strip()
    return t or None


def parse_history_detail(html: str) -> ParsedHistory:
    """Parse the per-member roll-call from a HistoryDetail page. Pure (no network).

    A vote row is detected STRUCTURALLY: a PersonDetail link in the first cell (every real roll-call
    row has one) — or, as a fallback for a row with no link, a KNOWN literal in the value cell. The
    value is NEVER used as a capture filter, so an unrecognized literal on a real (person-linked) row
    is captured (and warned), never silently dropped. The vote value is the LAST cell, so an extra
    column (e.g. a district) never displaces it.
    """
    soup = BeautifulSoup(html, "lxml")
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
    return ParsedHistory(votes)
