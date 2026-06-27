"""SF Legistar meeting scraper (live site: sfgov.legistar.com) — scrape-by-meeting slice.

Companion to scrape/legistar_scrape.py (the legislation slice). Crawl path:

    Calendar.aspx                 -> harvest meeting rows (EventId, EventGUID, body, date/time,
                                     location+subtype, agenda/minutes URLs, Granicus video clip id)
    MeetingDetail.aspx?ID=&GUID=  -> meeting header + gridMain agenda items, INCLUDING each acted
                                     row's `history_id` (MatterHistory id, from the radopen link)

Single-producer facts: the meeting slice does NOT fetch HistoryDetail. `history_id` (read straight
from MeetingDetail) is all the gold transform needs to attach a meeting to its facts
(history_id -> meeting_sk). The legislation slice (legistar_scrape.py) is the sole producer of action
text + roll-call votes, and it scrapes every matter on a scraped agenda via the LegislationDetail
link this slice captures as AgendaItem.matter_url (the "discovery feed").

Architecture (mirrors the legislation slice):
  * Plain `requests` + BeautifulSoup for every GET-able detail page. Parsing is DETERMINISTIC.
  * Playwright drives ONLY the Calendar postback (year/body selection) for deep enumeration.
    The default "This Month" calendar view is GET-able and already includes the current month's
    completed meetings — enough for the pilot without a browser. `--from/--to` filters the
    enumerated rows to a date window (e.g. a 2-week test slice).
  * Emits one JSON file per meeting to the bronze landing zone:
        raw/meetings/ingest_date=YYYY-MM-DD/<EventId>.json

Operational note: a meeting whose minutes status is "Draft" has NO action
labels yet (gridMain Action/Result blank, no radopen links). Such meetings are still emitted to
bronze (with status=Draft, no actions) so the incremental job can re-scrape them once minutes reach
"Final Draft"/"Final"; pass --skip-draft to skip them instead.

CLI:
    python -m scrape.legistar_meetings --event 1422963 --guid 0C4442D2-...        # one meeting
    python -m scrape.legistar_meetings --current-month --raw-dir raw/meetings ... # GET, no browser
    python -m scrape.legistar_meetings --from 2026-06-11 --to 2026-06-25 ...      # 2-week window
    python -m scrape.legistar_meetings --year 2026 --raw-dir raw/meetings ...     # Playwright enum
"""

from __future__ import annotations

import re
import json
import logging
import argparse
import dataclasses
from pathlib import Path
from datetime import date, datetime

from bs4 import BeautifulSoup

try:                                   # `-m scrape.legistar_meetings`, direct run, and imports
    from scrape.fetch import BASE, UA, get, extract_pdf_text
except ModuleNotFoundError as e:       # fall back ONLY when the `scrape` package isn't on the path
    if (e.name or "").split(".")[0] != "scrape":   # a real failure INSIDE the module must surface
        raise
    from fetch import BASE, UA, get, extract_pdf_text   # `python scrape/legistar_meetings.py`

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("legistar-meetings")

CP = "ctl00_ContentPlaceHolder1_"

# The HistoryDetail link on a gridMain agenda row is a radopen() ONCLICK, not an href.
_RADOPEN = re.compile(r"radopen\('(HistoryDetail\.aspx\?ID=(\d+)&GUID=[A-F0-9-]+)'", re.I)
# Granicus clip id lives in the window.open('...ID1=<clip>...') media handlers.
_CLIPID = re.compile(r"ID1=(\d+)")
# Meeting subtype is appended to the location string, e.g. "... Room 250 Recessed Meeting".
_SUBTYPE = re.compile(
    r"\s+(Regular|Special|Recessed|Closed Session|Joint|Adjourned|Recess)\s+Meeting\s*$", re.I)
# Calendar/agenda RadGrid rows carry >= 11 cells; fewer means a header/spacer row to skip.
MIN_GRID_COLS = 11


# --------------------------------------------------------------------------- data model
@dataclasses.dataclass
class AgendaItem:
    item_seq: int                # 0-indexed position in gridMain
    matter_file: str | None      # gridMain c0 — THE join key to dim_matter (file string)
    matter_url: str | None       # LegislationDetail URL from the c0 link — the Option-2 discovery feed
    agenda_number: str | None    # c2
    matter_name: str | None      # c3 (short subject)
    matter_type: str | None      # c4 (Ordinance / Resolution / ...)
    matter_status: str | None    # c5
    title: str | None            # c6 (full title)
    action_raw: str | None       # c7 raw Legistar action label (None if no action this meeting)
    action_result: str | None    # c8 Pass | Fail | None
    history_id: str | None       # MatterHistory id from the radopen link (the meeting->fact join key)
    history_url: str | None
    # No action_text / votes: those are HistoryDetail-only, which the legislation slice owns
    # (single-producer facts). The fields above are all free from MeetingDetail.


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


# --------------------------------------------------------------------------- parse helpers
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
        log.warning("calendar: no rgMasterTable found — empty page or layout change")
        return rows
    for tr in grid.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < MIN_GRID_COLS:
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
    if not grid:
        log.warning("meeting agenda: no gridMain found — layout change or empty agenda")
    else:
        for seq, tr in enumerate(grid.select("tbody tr")):
            tds = tr.find_all("td")
            if len(tds) < MIN_GRID_COLS:
                continue
            onclicks = " ".join(a.get("onclick", "") for a in tr.find_all("a"))
            m = _RADOPEN.search(onclicks)
            file_a = tds[0].find("a", href=re.compile(r"LegislationDetail\.aspx", re.I))
            items.append(AgendaItem(
                item_seq=seq,
                matter_file=_txt(tds[0]),
                matter_url=(BASE + file_a["href"].replace("&amp;", "&")) if file_a and file_a.get("href") else None,
                agenda_number=_txt(tds[2]),
                matter_name=_txt(tds[3]),
                matter_type=_txt(tds[4]),
                matter_status=_txt(tds[5]),
                title=_txt(tds[6]),
                action_raw=_txt(tds[7]),
                action_result=_txt(tds[8]),
                history_id=m.group(2) if m else None,
                history_url=(BASE + m.group(1).replace("&amp;", "&")) if m else None,
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
    """Scrape a single meeting: MeetingDetail header + agenda items (history_id included).

    No HistoryDetail fetch — see the single-producer note in the module docstring.
    """
    url = f"{BASE}MeetingDetail.aspx?ID={meeting_id}&GUID={event_guid}&Options="
    header, items = parse_meeting_detail(get(url))

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

    # history_id was captured from each row's radopen link in parse_meeting_detail (the only
    # meeting->fact key gold needs); action_text/votes come from the legislation slice and stay
    # empty here. No HistoryDetail fetch.
    log.info("meeting %s | %-38s | %s | minutes=%s | %d agenda items",
             meeting_id, (m.body_name or "")[:38], m.meeting_date,
             m.minutes_status, len(items))
    return m


# --------------------------------------------------------------------------- enumeration
def enumerate_current_month() -> list[CalendarRow]:
    """GET the default 'This Month' calendar (no browser) and parse its rows."""
    return parse_calendar(get(BASE + "Calendar.aspx"))


def enumerate_calendar(year: str | int | None = None, body: str | None = None) -> list[CalendarRow]:
    """Enumerate ALL calendar rows for a year / body via the Telerik postback (needs Playwright).

    Deep history (past months/years) is only reachable through the calendar's RadComboBox postback,
    so this drives a headless browser. Run `playwright install chromium` first. The grid renders only
    <=100 rows per page, so we page through every page and assert we collected the grid's reported
    total — a paging miss fails loudly instead of silently dropping meetings.
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
        rows = _collect_all_pages(page)
        browser.close()
    log.info("enumerated %d calendar rows (year=%s body=%s)", len(rows), year, body)
    return rows


# Calendar RadGrid pager line, e.g. "Page 1 of 2, items 1 to 100 of 189."
_PAGER_PAGES = re.compile(r"Page\s+\d+\s+of\s+(\d+)", re.I)
_PAGER_TOTAL = re.compile(r"items\s+[\d,]+\s+to\s+[\d,]+\s+of\s+([\d,]+)", re.I)


def _pager_pages(text: str) -> int:
    """Total page count from the pager line; 1 when there's no pager (single page)."""
    m = _PAGER_PAGES.search(text)
    return int(m.group(1)) if m else 1


def _pager_total(text: str) -> int | None:
    """Reported total row count from the pager line; None when there's no pager."""
    m = _PAGER_TOTAL.search(text)
    return int(m.group(1).replace(",", "")) if m else None


def _ajax_postback(page, trigger_js: str, arg) -> str:
    """Fire a Telerik trigger (combo select / pager nav) that posts back via ASP.NET, then wait for
    the grid to re-render. The trigger must defer __doPostBack via setTimeout — ASP.NET's postback
    reads arguments.callee, illegal in Playwright's strict-mode evaluate but fine in a deferred
    callback. The postback REMOVES then re-adds the grid table, so we wait it out deterministically:
    a brief settle (let the old table detach), network-idle, then wait for the table to reappear —
    fixed sleeps alone race the ~1s re-render and read a momentarily-absent grid."""
    result = page.evaluate(trigger_js, arg)
    page.wait_for_timeout(500)                          # let the postback fire + the old table detach
    try:
        page.wait_for_load_state("networkidle", timeout=60_000)
    except Exception:
        pass
    page.wait_for_selector("table.rgMasterTable", state="visible", timeout=30_000)  # new grid rendered
    page.wait_for_timeout(300)
    return result


def _collect_all_pages(page) -> list[CalendarRow]:
    """Parse every page of the calendar RadGrid and assert the de-duped count equals the grid's
    reported total, so paging that silently drops rows fails loudly. The grid caps at 100 rows/page
    with a numeric pager driven by Telerik.Web.UI.Grid.NavigateToPage(gridId, n)."""
    if not page.query_selector("table.rgMasterTable"):
        return []
    grid_id = page.eval_on_selector("table.rgMasterTable", "e => e.id")
    info = page.inner_text("body")
    pages, total = _pager_pages(info), _pager_total(info)
    rows: list[CalendarRow] = []
    seen: set[str] = set()
    for pnum in range(1, pages + 1):
        if pnum > 1:
            _ajax_postback(
                page,
                "([gid, p]) => { setTimeout(function () {"
                " Telerik.Web.UI.Grid.NavigateToPage(gid, p); }, 0); return 'ok'; }",
                [grid_id, str(pnum)])
        for r in parse_calendar(page.content()):
            if r.meeting_id not in seen:
                seen.add(r.meeting_id)
                rows.append(r)
    if total is not None and len(rows) != total:
        raise AssertionError(f"calendar paging incomplete: collected {len(rows)} of {total} reported "
                             f"rows across {pages} page(s) — Legistar pager/layout change?")
    return rows


def _select_radcombo(page, base_id: str, item_text: str) -> None:
    """Pick a Telerik RadComboBox item by visible text, reliably. Direct UI clicks race the dropdown's
    open-animation and silently no-op in headless, so we drive Telerik's client API ($find ->
    findItemByText -> select). The combo's postback uniqueID is its clientID with _ -> $."""
    result = _ajax_postback(
        page,
        """([cid, uid, text]) => {
             var c = window.$find && window.$find(cid);
             if (!c) return 'no-combo';
             var it = c.findItemByText(text);
             if (!it) return 'no-item';
             it.select();
             setTimeout(function () { __doPostBack(uid, ''); }, 0);
             return 'ok';
           }""",
        [base_id, base_id.replace("_", "$"), item_text])
    if result != "ok":
        raise LookupError(f"RadComboBox {base_id}: {result} for {item_text!r}")


# --------------------------------------------------------------------------- orchestration / CLI
def _meeting_in_window(date_raw: str | None, start: date | None, end: date | None) -> bool:
    """True if a calendar row's raw meeting_date (e.g. '6/16/2026') falls in [start, end].

    A window lets the no-browser `--current-month` enumeration be sliced to a test range (e.g. 2
    weeks). Blank/unparseable dates are excluded when a window is set."""
    if start is None and end is None:
        return True
    if not date_raw:
        return False
    try:
        d = datetime.strptime(date_raw.strip(), "%m/%d/%Y").date()
    except ValueError:
        return False
    return (start is None or d >= start) and (end is None or d <= end)


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
        if out_dir is not None and (out_dir / f"{r.meeting_id}.json").exists():
            log.info("[%d/%d] skip %s — already on disk (resume)", i, len(targets), r.meeting_id)
            continue
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
    ap.add_argument("--from", dest="start",
                    help="window start YYYY-MM-DD — filter enumerated meetings (no browser needed "
                         "when the window is within the current month)")
    ap.add_argument("--to", dest="end", help="window end YYYY-MM-DD")
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
    elif args.current_month or args.year or (args.start and args.end):
        rows = enumerate_calendar(year=args.year, body=args.body) if args.year \
            else enumerate_current_month()
        if args.start or args.end:
            s = date.fromisoformat(args.start) if args.start else None
            e = date.fromisoformat(args.end) if args.end else None
            kept = [r for r in rows if _meeting_in_window(r.meeting_date, s, e)]
            log.info("date window %s..%s -> %d of %d meetings", s, e, len(kept), len(rows))
            rows = kept
        meetings = collect(rows, out_dir=out_dir, completed_only=not args.all,
                           with_text=args.with_text, skip_draft=args.skip_draft)
    else:
        ap.error("provide --event/--guid, --current-month, --year, or --from/--to")

    if args.out:
        Path(args.out).write_text(
            json.dumps([dataclasses.asdict(m) for m in meetings], indent=2, ensure_ascii=False))
        log.info("wrote %d meetings -> %s", len(meetings), args.out)
    elif out_dir is not None:
        log.info("wrote %d meeting files -> %s", len(meetings), out_dir)


if __name__ == "__main__":
    main()
