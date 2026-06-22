"""SF Legistar meeting scraper (live site: sfgov.legistar.com) — scrape-by-meeting slice.

Companion to scrape/legistar_scrape.py (the legislation slice). Crawl path:

    Calendar.aspx                 -> harvest meeting rows (EventId, EventGUID, body, date/time,
                                     location+subtype, agenda/minutes URLs, Granicus video clip id)
    MeetingDetail.aspx?ID=&GUID=  -> meeting header + gridMain agenda items
    HistoryDetail.aspx?ID=&GUID=  -> per-item action text + per-person roll-call votes

Architecture (mirrors the legislation slice):
  * Plain `requests` + BeautifulSoup for every GET-able detail page. Parsing is DETERMINISTIC.
  * Playwright drives ONLY the Calendar postback (year/body selection) for deep enumeration.
    The default "This Month" calendar view is GET-able and already includes the current month's
    completed meetings — enough for the pilot without a browser.
  * Emits one JSON file per meeting to the bronze landing zone:
        raw/meetings/ingest_date=YYYY-MM-DD/<EventId>.json

Operational note (verified 2026-06-21): a meeting whose minutes status is "Draft" has NO
populated actions/votes yet (gridMain Action/Result blank, no HistoryDetail links). Such meetings
are still emitted to bronze (with status=Draft, no actions) so the incremental job can re-scrape
them from gold once minutes reach "Final Draft"/"Final"; pass --skip-draft to skip them instead.

CLI:
    python -m scrape.legistar_meetings --event 1422963 --guid 0C4442D2-...        # one meeting
    python -m scrape.legistar_meetings --current-month --raw-dir raw/meetings ... # GET, no browser
    python -m scrape.legistar_meetings --year 2026 --raw-dir raw/meetings ...     # Playwright enum
"""

from __future__ import annotations

import re
import json
import time
import logging
import argparse
import dataclasses
from io import BytesIO
from pathlib import Path
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("legistar-meetings")

BASE = "https://sfgov.legistar.com/"
# Polite crawling: identify ourselves and rate-limit (no robots.txt exists, so it's on us).
UA = "Mozilla/5.0 (research; MSDS683 student project; contact jacksoncdawson@gmail.com)"
RATE_LIMIT_S = 1.0
CP = "ctl00_ContentPlaceHolder1_"

# The HistoryDetail link on a gridMain agenda row is a radopen() ONCLICK, not an href.
_RADOPEN = re.compile(r"radopen\('(HistoryDetail\.aspx\?ID=(\d+)&GUID=[A-F0-9-]+)'", re.I)
# Granicus clip id lives in the window.open('...ID1=<clip>...') media handlers.
_CLIPID = re.compile(r"ID1=(\d+)")
# Meeting subtype is appended to the location string, e.g. "... Room 250 Recessed Meeting".
_SUBTYPE = re.compile(
    r"\s+(Regular|Special|Recessed|Closed Session|Joint|Adjourned|Recess)\s+Meeting\s*$", re.I)

# Expected HistoryDetail gridVote literals (Aye/Excused confirmed live; rest expected). Used ONLY
# to flag novel literals — NOT as a capture filter (vote rows are detected structurally so an
# unanticipated literal is captured + warned, never silently dropped).
KNOWN_VOTE_LITERALS = {"Aye", "No", "Nay", "Absent", "Excused", "Recused", "Present"}

SESSION = requests.Session()
SESSION.headers["User-Agent"] = UA


# --------------------------------------------------------------------------- data model
@dataclasses.dataclass
class Vote:
    person_id: str | None        # PersonId from PersonDetail.aspx?ID= (robust join key)
    person_name: str
    vote_value: str              # raw literal: Aye | No | Excused | Absent | Recused


@dataclasses.dataclass
class AgendaItem:
    item_seq: int                # 0-indexed position in gridMain
    matter_file: str | None      # gridMain c0 — THE join key to dim_matter (file string)
    agenda_number: str | None    # c2
    matter_name: str | None      # c3 (short subject)
    matter_type: str | None      # c4 (Ordinance / Resolution / ...)
    matter_status: str | None    # c5
    title: str | None            # c6 (full title)
    action_raw: str | None       # c7 raw Legistar action label (None if no action this meeting)
    action_result: str | None    # c8 Pass | Fail | None
    history_id: str | None       # MatterHistory id from the radopen link
    history_url: str | None
    action_text: str | None      # HistoryDetail lblActionText (full motion text)
    votes: list[Vote]


@dataclasses.dataclass
class MeetingDocument:
    document_source: str         # meeting_agenda | meeting_minutes | transcript
    document_title: str | None
    document_url: str | None
    body_text: str | None        # extracted text when --with-text, else None


@dataclasses.dataclass
class Meeting:
    meeting_id: str              # Legistar EventId
    event_guid: str             # Legistar EventGUID (mandatory for every fetch)
    body_name: str | None       # MeetingDetail hypName / calendar col0
    meeting_date: str | None    # raw "6/16/2026"
    meeting_time: str | None    # raw "2:00 PM"
    location: str | None        # cleaned (subtype stripped)
    meeting_subtype: str | None # Regular / Recessed / Special / ...
    agenda_status: str | None   # Final / Draft
    minutes_status: str | None  # Final / Final Draft / Draft / None
    agenda_url: str | None      # View.ashx?M=A&ID=&GUID=
    minutes_url: str | None     # View.ashx?M=M&ID=&GUID=
    video_clip_id: str | None   # Granicus clip id (ID1=) — link to video/audio/transcript
    documents: list[MeetingDocument]
    agenda_items: list[AgendaItem]


# --------------------------------------------------------------------------- http helpers
RETRY_STATUS = {429, 500, 502, 503, 504}   # transient — retry; 4xx like 410 are permanent
MAX_RETRIES = 3


def _request(url: str, timeout: int) -> requests.Response:
    """GET with rate-limit + bounded backoff on transient failures. Permanent errors (e.g. 410
    from a missing GUID) raise immediately so they aren't retried."""
    last: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        time.sleep(RATE_LIMIT_S)
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code in RETRY_STATUS:
                raise requests.HTTPError(f"{r.status_code} transient", response=r)
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status is not None and status not in RETRY_STATUS:
                raise                          # permanent (404/410/...) — do not retry
            last = e
            if attempt < MAX_RETRIES:
                log.warning("transient fetch error (%s) on %s — retry %d/%d",
                            e, url, attempt, MAX_RETRIES - 1)
                time.sleep(RATE_LIMIT_S * 2 * attempt)
    raise last  # type: ignore[misc]


def _get(url: str) -> str:
    return _request(url, timeout=30).text


def _get_bytes(url: str) -> bytes:
    return _request(url, timeout=60).content


def _txt(el) -> str | None:
    if el is None:
        return None
    t = el.get_text(" ", strip=True).replace("\xa0", " ").strip()
    return t or None


def _lbl(soup: BeautifulSoup, name: str) -> str | None:
    """Text of the value label ctl00_ContentPlaceHolder1_<name> (suffix match tolerant)."""
    el = soup.find(id=f"{CP}{name}") or soup.find(id=re.compile(rf"{name}$"))
    return _txt(el)


def split_subtype(location_raw: str | None) -> tuple[str | None, str | None]:
    """Split '... Room 250 Recessed Meeting' -> ('... Room 250', 'Recessed')."""
    if not location_raw:
        return None, None
    loc = location_raw.replace("\xa0", " ").replace("\n", " ").strip()
    loc = re.sub(r"\s+", " ", loc)
    m = _SUBTYPE.search(loc)
    if m:
        return loc[: m.start()].strip(" ,") or None, m.group(1).title()
    return loc, None


def extract_pdf_text(url: str) -> str | None:
    """Download a View.ashx document and extract text if it's a PDF."""
    from pypdf import PdfReader
    data = _get_bytes(url)
    if data[:4] != b"%PDF":
        return None
    reader = PdfReader(BytesIO(data))
    return "\n".join(p.extract_text() or "" for p in reader.pages)


# --------------------------------------------------------------------------- pure parsers
@dataclasses.dataclass
class CalendarRow:
    meeting_id: str
    event_guid: str
    body_name: str | None
    meeting_date: str | None
    meeting_time: str | None
    location: str | None
    meeting_subtype: str | None
    agenda_url: str | None
    minutes_url: str | None
    video_clip_id: str | None
    has_minutes: bool


def parse_calendar(html: str) -> list[CalendarRow]:
    """Parse the Calendar.aspx RadGrid into meeting rows. Pure (no network)."""
    soup = BeautifulSoup(html, "lxml")
    grid = soup.select_one("table.rgMasterTable")
    rows: list[CalendarRow] = []
    if not grid:
        return rows
    for tr in grid.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 11:
            continue
        md = tds[5].find("a", href=re.compile(r"MeetingDetail\.aspx", re.I))
        if not md:
            continue
        href = md["href"]
        eid = re.search(r"[?&]ID=(\d+)", href)
        guid = re.search(r"[?&]GUID=([A-F0-9-]+)", href, re.I)
        if not (eid and guid):
            continue

        def _href(td):
            a = td.find("a", href=re.compile(r"View\.ashx", re.I))
            return BASE + a["href"].replace("&amp;", "&") if a and a.get("href") else None

        def _clip(td):
            a = td.find("a")
            m = _CLIPID.search((a.get("onclick") or a.get("href") or "")) if a else None
            return m.group(1) if m else None

        loc_clean, subtype = split_subtype(_txt(tds[4]))
        mins_txt = _txt(tds[7]) or ""
        rows.append(CalendarRow(
            meeting_id=eid.group(1),
            event_guid=guid.group(1).upper(),
            body_name=_txt(tds[0]),
            meeting_date=_txt(tds[1]),
            meeting_time=_txt(tds[3]),
            location=loc_clean,
            meeting_subtype=subtype,
            agenda_url=_href(tds[6]),
            minutes_url=_href(tds[7]),
            video_clip_id=_clip(tds[8]),
            has_minutes="available" not in mins_txt.lower() and bool(mins_txt),
        ))
    return rows


def parse_history(html: str) -> tuple[str | None, str | None, str | None, list[Vote]]:
    """Parse a HistoryDetail page -> (action_raw, action_result, action_text, votes)."""
    soup = BeautifulSoup(html, "lxml")
    action = _lbl(soup, "lblAction")
    result = _lbl(soup, "lblResult")
    action_text = _lbl(soup, "lblActionText")
    votes: list[Vote] = []
    grid = soup.select_one("table.rgMasterTable")
    if grid:
        for tr in grid.select("tbody tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            person = _txt(tds[0])
            value = _txt(tds[-1])
            a = tds[0].find("a", href=re.compile(r"PersonDetail\.aspx", re.I))
            pid = re.search(r"[?&]ID=(\d+)", a["href"]) if a and a.get("href") else None
            # A vote row is detected STRUCTURALLY: a person link in tds[0] (every real roll-call
            # row has one) or a known literal as a fallback — never by value-membership alone, so
            # an unanticipated literal is captured (and flagged), not silently dropped.
            is_vote_row = bool(a) or (person and value in KNOWN_VOTE_LITERALS)
            if not is_vote_row or not value:
                continue
            if value not in KNOWN_VOTE_LITERALS:
                log.warning("unrecognized vote literal %r for %r — capturing verbatim", value, person)
            votes.append(Vote(
                person_id=pid.group(1) if pid else None,
                person_name=person or "",
                vote_value=value,             # raw literal; normalized (No->Nay) at the merge
            ))
    return action, result, action_text, votes


def parse_meeting_detail(html: str) -> tuple[dict, list[AgendaItem]]:
    """Parse MeetingDetail header + gridMain agenda items (votes not yet fetched). Pure."""
    soup = BeautifulSoup(html, "lxml")
    loc_clean, subtype = split_subtype(_lbl(soup, "lblLocation"))

    def _hyp_href(name: str) -> str | None:
        a = soup.find(id=f"{CP}{name}") or soup.find(id=re.compile(rf"{name}$"))
        return BASE + a["href"].replace("&amp;", "&") if a and a.get("href") else None

    # The Granicus clip id (ID1=) is also on the MeetingDetail page (per-item video handlers all
    # point at the one meeting recording), so the single-meeting path recovers it without the calendar.
    clip = _CLIPID.search(html)
    header = {
        "body_name": _lbl(soup, "hypName"),
        "meeting_date": _lbl(soup, "lblDate"),
        "meeting_time": _lbl(soup, "lblTime"),
        "location": loc_clean,
        "meeting_subtype": subtype,
        "agenda_status": _lbl(soup, "lblAgendaStatus"),
        "minutes_status": _lbl(soup, "lblMinutesStatus"),
        "agenda_url": _hyp_href("hypAgenda"),
        "minutes_url": _hyp_href("hypMinutes"),
        "video_clip_id": clip.group(1) if clip else None,
    }

    items: list[AgendaItem] = []
    grid = soup.select_one("table.rgMasterTable")
    if grid:
        for seq, tr in enumerate(grid.select("tbody tr")):
            tds = tr.find_all("td")
            if len(tds) < 11:
                continue
            onclicks = " ".join(a.get("onclick", "") for a in tr.find_all("a"))
            m = _RADOPEN.search(onclicks)
            items.append(AgendaItem(
                item_seq=seq,
                matter_file=_txt(tds[0]),
                agenda_number=_txt(tds[2]),
                matter_name=_txt(tds[3]),
                matter_type=_txt(tds[4]),
                matter_status=_txt(tds[5]),
                title=_txt(tds[6]),
                action_raw=_txt(tds[7]),
                action_result=_txt(tds[8]),
                history_id=m.group(2) if m else None,
                history_url=(BASE + m.group(1).replace("&amp;", "&")) if m else None,
                action_text=None,
                votes=[],
            ))
    return header, items


# --------------------------------------------------------------------------- scrape one meeting
def _build_documents(m: Meeting, with_text: bool) -> list[MeetingDocument]:
    docs: list[MeetingDocument] = []
    if m.agenda_url:
        docs.append(MeetingDocument("meeting_agenda", "Agenda", m.agenda_url, None))
    if m.minutes_url:
        docs.append(MeetingDocument("meeting_minutes", "Minutes", m.minutes_url, None))
    if m.video_clip_id:
        vtt = f"https://sanfrancisco.granicus.com/videos/{m.video_clip_id}/captions.vtt"
        docs.append(MeetingDocument("transcript", "Transcript (VTT)", vtt, None))
    if with_text:
        for d in docs:
            if d.document_source in ("meeting_agenda", "meeting_minutes"):
                try:
                    d.body_text = extract_pdf_text(d.document_url)
                except Exception as e:                       # noqa: BLE001
                    log.warning("doc text extract failed (%s): %s", d.document_url, e)
    return docs


def scrape_meeting(meeting_id: str, event_guid: str,
                   cal: CalendarRow | None = None,
                   with_text: bool = False,
                   skip_draft: bool = False) -> Meeting | None:
    """Scrape a single meeting (MeetingDetail + per-item HistoryDetail votes)."""
    url = f"{BASE}MeetingDetail.aspx?ID={meeting_id}&GUID={event_guid}&Options="
    header, items = parse_meeting_detail(_get(url))

    if skip_draft and (header.get("minutes_status") or "").strip().lower() == "draft":
        log.info("skip %s (%s) — minutes Draft, actions not posted yet",
                 meeting_id, header.get("body_name"))
        return None

    m = Meeting(
        meeting_id=meeting_id,
        event_guid=event_guid.upper(),
        body_name=header["body_name"] or (cal.body_name if cal else None),
        meeting_date=header["meeting_date"] or (cal.meeting_date if cal else None),
        meeting_time=header["meeting_time"] or (cal.meeting_time if cal else None),
        location=header["location"] or (cal.location if cal else None),
        meeting_subtype=header["meeting_subtype"] or (cal.meeting_subtype if cal else None),
        agenda_status=header["agenda_status"],
        minutes_status=header["minutes_status"],
        agenda_url=header["agenda_url"] or (cal.agenda_url if cal else None),
        minutes_url=header["minutes_url"] or (cal.minutes_url if cal else None),
        video_clip_id=header.get("video_clip_id") or (cal.video_clip_id if cal else None),
        documents=[],
        agenda_items=items,
    )
    m.documents = _build_documents(m, with_text)

    # Only follow HistoryDetail for items actually acted on this meeting (blank action ->
    # blank HistoryDetail, verified). Saves a fetch per no-action agenda item.
    voted = 0
    for it in items:
        if it.history_url and it.action_raw:
            action, result, action_text, votes = parse_history(_get(it.history_url))
            it.action_raw = it.action_raw or action
            it.action_result = it.action_result or result
            it.action_text = action_text
            it.votes = votes
            voted += len(votes)

    log.info("meeting %s | %-38s | %s | minutes=%s | %d items | %d votes",
             meeting_id, (m.body_name or "")[:38], m.meeting_date,
             m.minutes_status, len(items), voted)
    return m


# --------------------------------------------------------------------------- enumeration
def enumerate_current_month() -> list[CalendarRow]:
    """GET the default 'This Month' calendar (no browser) and parse its rows."""
    return parse_calendar(_get(BASE + "Calendar.aspx"))


def enumerate_calendar(year: str | int | None = None, body: str | None = None) -> list[CalendarRow]:
    """Enumerate calendar rows for a year / body via the Telerik postback (needs Playwright).

    Deep history (past months/years) is only reachable through the calendar's RadComboBox
    postback, so this drives a headless browser. Run `playwright install chromium` first.
    """
    from playwright.sync_api import sync_playwright
    rows: list[CalendarRow] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        page.goto(BASE + "Calendar.aspx", wait_until="networkidle", timeout=60_000)
        if year is not None:
            _select_radcombo(page, f"{CP}lstYears", str(year))
        if body:
            _select_radcombo(page, f"{CP}lstBodies", body)
        page.wait_for_load_state("networkidle", timeout=60_000)
        rows = parse_calendar(page.content())
        browser.close()
    log.info("enumerated %d calendar rows (year=%s body=%s)", len(rows), year, body)
    if len(rows) >= 95:
        log.warning("calendar returned %d rows — likely hit the ~100-row RadGrid page cap; "
                    "window per body (--body) or per month to avoid SILENT truncation", len(rows))
    return rows


def _select_radcombo(page, base_id: str, item_text: str) -> None:
    """Pick an item in a Telerik RadComboBox by visible text, then wait for the postback."""
    page.click(f"#{base_id}_Arrow")
    page.wait_for_selector(f"#{base_id}_DropDown", state="visible", timeout=15_000)
    page.click(f"#{base_id}_DropDown >> text=/^\\s*{re.escape(item_text)}\\s*$/")
    page.wait_for_load_state("networkidle", timeout=60_000)


# --------------------------------------------------------------------------- orchestration / CLI
def _write_one(m: Meeting, out_dir: Path) -> None:
    (out_dir / f"{m.meeting_id}.json").write_text(
        json.dumps(dataclasses.asdict(m), indent=2, ensure_ascii=False))


def collect(rows: list[CalendarRow], out_dir: Path | None = None, completed_only: bool = True,
            with_text: bool = False, skip_draft: bool = False) -> list[Meeting]:
    """Scrape each calendar row. Per-meeting error isolation (one bad fetch never aborts the
    batch) and incremental bronze writes (a late failure never discards already-fetched meetings).
    Draft meetings are emitted by default so the incremental job can re-scrape them from gold."""
    meetings: list[Meeting] = []
    targets = [r for r in rows if r.has_minutes] if completed_only else rows
    for i, r in enumerate(targets, 1):
        log.info("[%d/%d] fetching %s %s", i, len(targets), r.body_name, r.meeting_date)
        try:
            m = scrape_meeting(r.meeting_id, r.event_guid, cal=r,
                               with_text=with_text, skip_draft=skip_draft)
        except Exception as e:                                       # noqa: BLE001
            log.warning("[%d/%d] FAILED %s %s: %s — skipping", i, len(targets),
                        r.body_name, r.meeting_id, e)
            continue
        if not m:
            continue
        meetings.append(m)
        if out_dir is not None:
            _write_one(m, out_dir)
    return meetings


def main() -> None:
    ap = argparse.ArgumentParser(description="SF Legistar meeting scraper (scrape-by-meeting)")
    ap.add_argument("--event", help="single EventId")
    ap.add_argument("--guid", help="EventGUID (required with --event)")
    ap.add_argument("--current-month", action="store_true",
                    help="GET the default calendar (no browser) and scrape completed meetings")
    ap.add_argument("--year", help="enumerate a year via Playwright (needs chromium)")
    ap.add_argument("--body", help="restrict enumeration to a body name")
    ap.add_argument("--all", action="store_true",
                    help="with --current-month/--year: scrape every row, not only completed")
    ap.add_argument("--with-text", action="store_true", help="extract agenda/minutes PDF text")
    ap.add_argument("--skip-draft", action="store_true",
                    help="skip meetings whose minutes are still Draft (default: emit them so the "
                         "incremental job can re-scrape from gold)")
    ap.add_argument("--raw-dir", default="raw/meetings", help="bronze landing zone root")
    ap.add_argument("--date", dest="ingest_date", default=date.today().isoformat(),
                    help="ingest partition date YYYY-MM-DD (default: today)")
    ap.add_argument("--out", help="instead of --raw-dir, write a single JSON array here")
    args = ap.parse_args()

    out_dir: Path | None = None
    if not args.out:
        out_dir = Path(args.raw_dir) / f"ingest_date={args.ingest_date}"
        out_dir.mkdir(parents=True, exist_ok=True)

    if args.event:
        if not args.guid:
            ap.error("--event requires --guid")
        try:
            m = scrape_meeting(args.event, args.guid,
                               with_text=args.with_text, skip_draft=args.skip_draft)
        except Exception as e:                                       # noqa: BLE001
            log.error("scrape failed for %s: %s", args.event, e)
            m = None
        meetings = [m] if m else []
        if out_dir is not None:
            for mm in meetings:
                _write_one(mm, out_dir)
    elif args.current_month or args.year:
        rows = enumerate_calendar(year=args.year, body=args.body) if args.year \
            else enumerate_current_month()
        meetings = collect(rows, out_dir=out_dir, completed_only=not args.all,
                           with_text=args.with_text, skip_draft=args.skip_draft)
    else:
        ap.error("provide --event/--guid, --current-month, or --year")

    if args.out:
        Path(args.out).write_text(
            json.dumps([dataclasses.asdict(m) for m in meetings], indent=2, ensure_ascii=False))
        log.info("wrote %d meetings -> %s", len(meetings), args.out)
    elif out_dir is not None:
        log.info("wrote %d meeting files -> %s", len(meetings), out_dir)


if __name__ == "__main__":
    main()
