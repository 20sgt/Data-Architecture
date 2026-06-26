"""Consolidated SF Legistar scraper (live site: sfgov.legistar.com).

Extracts everything the project's 4 use cases need:

    enumerate_matters(start, end)  -> matters introduced in a date window (date-sliced search)
    find_matter_url(file_number)   -> resolve one file # to its LegislationDetail URL
    scrape_matter(detail_url)      -> full Matter: subject/abstract, type, status, lifecycle,
                                      controlling committee, sponsors, related files, attachments,
                                      per-member roll-call votes (each action tagged with its
                                      history_id), and (optional) full statutory text

Single-producer facts: this slice is the sole producer of fact_matter_action / fact_vote. Besides
its File-Created date window it also scrapes every matter on a scraped meeting agenda (the
`--agenda-bronze` discovery feed), so its coverage is a superset of the meeting slice's — a bill
created months ago but acted on this week is still reached via the agenda. Each action carries its
`history_id`, the exact key the transform uses to attach a fact to its meeting.

Architecture:
  * Playwright drives ONLY the ASP.NET/Telerik postback search (enumeration + file-# lookup).
  * Everything else is plain `requests` + BeautifulSoup against GET-able detail pages.
  * Votes and every structured field are parsed DETERMINISTICALLY — never via an LLM. The LLM is
    reserved for summarizing `Matter.full_text` downstream (not in this module).

CLI:
    python legistar_scrape.py --file 260388 [--with-text]
    python legistar_scrape.py --from 2026-05-01 --to 2026-05-14 [--with-text] [--out matters.json]
    python legistar_scrape.py --from 2026-06-11 --to 2026-06-25 \
        --agenda-bronze raw/meetings/ingest_date=2026-06-25 --raw-dir raw/matters/ingest_date=2026-06-25
"""

from __future__ import annotations

import re
import json
import logging
import argparse
import dataclasses
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

# playwright (collect) is imported LAZILY inside the only function that drives it, so this module
# imports with just requests + bs4 — `import scrape.legistar_scrape` works without a browser.
if TYPE_CHECKING:                      # `Page` is a type hint only; never needed at runtime.
    from playwright.sync_api import Page

try:                                   # `-m scrape.legistar_scrape`, the DAG import, and direct run
    from scrape.history_detail import Vote, parse_history_detail
    from scrape.fetch import BASE, UA, get, extract_pdf_text
except ModuleNotFoundError as e:       # fall back ONLY when the `scrape` package isn't on the path
    if (e.name or "").split(".")[0] != "scrape":   # a real failure INSIDE the module must surface
        raise
    from history_detail import Vote, parse_history_detail   # `python scrape/legistar_scrape.py`
    from fetch import BASE, UA, get, extract_pdf_text

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("legistar")

# CP carries a leading '#': it prefixes Playwright CSS selectors here, unlike the meeting slice's
# bare id prefix used with BeautifulSoup find(id=...).
CP = "#ctl00_ContentPlaceHolder1_"
RESULT_CAP = 100  # search cap; weekly slices stay safely under it (SF ~30-100 files/week)

# Status -> lifecycle stage. "Filed" = closed WITHOUT passage (not passed). Extend as the
# vocabulary grows; unknown statuses fall through to "other".
PASSED = {"passed", "approved", "adopted", "finally passed", "ordinance enacted", "mayor approved"}
IN_WORKS = {"first reading", "in committee", "pending committee action", "new business",
            "scheduled for committee hearing", "30 day rule", "for immediate adoption",
            "special order", "assigned", "continued", "pending board action"}


# --------------------------------------------------------------------------- data model
# `Vote` is imported from scrape.history_detail (the roll-call parser module).
@dataclasses.dataclass
class Action:
    date: str
    body: str
    action: str
    result: str
    history_id: str | None     # bare MatterHistory id — the exact key the transform joins to meeting_sk
    history_url: str | None
    votes: list[Vote]


@dataclasses.dataclass
class Attachment:
    name: str
    url: str


@dataclasses.dataclass
class Matter:
    file_number: str | None
    detail_url: str
    name: str | None           # short subject line
    title: str | None          # full abstract paragraph (summary/keyword corpus)
    type: str | None
    status: str | None
    introduced: str | None
    on_agenda: str | None
    final_action: str | None
    enactment_date: str | None
    enactment_number: str | None
    in_control: str | None     # committee/body currently holding it (use case 2)
    sponsors: list[str]
    related_files: list[str]
    attachments: list[Attachment]
    actions: list[Action]      # history, incl. per-member votes
    full_text: str | None      # extracted from the Leg Ver1 PDF when requested

    @property
    def lifecycle(self) -> str:
        return bucket(self.status or "")


def bucket(status: str) -> str:
    s = status.strip().lower()
    if s in PASSED:
        return "passed"
    if s in IN_WORKS:
        return "in_works"
    return "other"


# --------------------------------------------------------------------------- parse helpers
def _split_names(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = re.split(r";|,| and ", raw)
    return [p.strip() for p in parts if p.strip()]


# --------------------------------------------------------------------------- detail parsing
# "Action details" links embed their roll-call page id, e.g. radopen('HistoryDetail.aspx?ID=…').
# Group 1 = the HistoryDetail URL; group 2 = the bare MatterHistory id (the cross-slice join key).
_RADOPEN = re.compile(r"radopen\('(HistoryDetail\.aspx\?ID=(\d+)&GUID=[A-F0-9-]+)'", re.I)

# Per-member roll-call parsing lives in scrape.history_detail.parse_history_detail (a pure parser;
# this slice fetches the page and passes the HTML in).


def scrape_matter(detail_url: str, with_text: bool = False) -> Matter:
    """Parse a LegislationDetail page into a full Matter (metadata + votes [+ full text])."""
    soup = BeautifulSoup(get(detail_url), "lxml")

    def val(value_id: str) -> str | None:
        el = soup.find(id=f"ctl00_ContentPlaceHolder1_{value_id}")
        txt = el.get_text(" ", strip=True) if el else None
        return txt or None

    # Attachments: anchors to the document store. The first "Leg Ver*" is the full bill text.
    attachments = [
        Attachment(a.get_text(strip=True), BASE + a["href"].replace("&amp;", "&"))
        for a in soup.find_all("a", href=re.compile(r"View\.ashx\?M=F"))
    ]

    # History grid -> actions; each action's HistoryDetail URL comes from its radopen() onclick.
    actions: list[Action] = []
    grid = soup.find(id="ctl00_ContentPlaceHolder1_gridLegislation_ctl00")
    if not grid:
        log.warning("%s: no history grid found — control-id drift? emitted with 0 actions", detail_url)
    else:
        for tr in grid.select("tr"):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            cells = [td.get_text(strip=True) for td in tds]
            m = _RADOPEN.search(" ".join(a.get("onclick", "") for a in tr.find_all("a")))
            hist_url = BASE + m.group(1) if m else None
            hist_id = m.group(2) if m else None
            votes = parse_history_detail(get(hist_url)).votes if hist_url else []
            actions.append(Action(cells[0], cells[2], cells[3], cells[4], hist_id, hist_url, votes))

    full_text = None
    if with_text:
        leg = next((a for a in attachments if a.name.lower().startswith("leg ver")), None)
        if leg:
            full_text = extract_pdf_text(leg.url)

    return Matter(
        file_number=val("lblFile2"),
        detail_url=detail_url,
        name=val("lblName2"),
        title=val("lblTitle2"),
        type=val("lblType2"),
        status=val("lblStatus2"),
        introduced=val("lblIntroduced2"),
        on_agenda=val("lblOnAgenda2"),
        final_action=val("lblPassed2"),
        enactment_date=val("lblEnactmentDate2"),
        enactment_number=val("lblEnactmentNumber2"),
        in_control=val("hypInControlOf2"),
        sponsors=_split_names(val("lblSponsors2")),
        related_files=_split_names(val("lblRelatedFiles2")),
        attachments=attachments,
        actions=actions,
        full_text=full_text,
    )


# --------------------------------------------------------------------------- search (Playwright)
def find_matter_url(file_number: str, page: Page) -> str:
    """Resolve a file number to its LegislationDetail URL via the (postback) simple search.

    The ID search is fuzzy (it also matches matters that *reference* the number), so we select the
    grid row whose File# column equals `file_number` exactly rather than taking the first result.
    """
    page.goto(BASE + "Legislation.aspx", wait_until="networkidle", timeout=60_000)
    page.check(CP + "chkID")
    page.fill(CP + "txtSearch", file_number)
    page.press(CP + "txtSearch", "Enter")
    page.wait_for_load_state("networkidle", timeout=60_000)
    grid = CP + "gridMain_ctl00"
    headers = [h.inner_text().strip() for h in page.query_selector_all(f"{grid} th")]
    file_idx = next((i for i, h in enumerate(headers) if "File" in h), None)
    if file_idx is None:
        raise LookupError(f"File# column not in search grid headers {headers} (layout change?)")
    for tr in page.query_selector_all(f"{grid} tr"):
        cells = [td.inner_text().replace("\xa0", " ").strip() for td in tr.query_selector_all("td")]
        if len(cells) == len(headers) and cells[file_idx] == file_number:
            href = next((a.get_attribute("href") for a in tr.query_selector_all("a")
                         if "LegislationDetail.aspx" in (a.get_attribute("href") or "")), None)
            if href:
                return BASE + href.replace("&amp;", "&")
    raise LookupError(f"No exact match for file {file_number}")


def enumerate_matters(start: date, end: date, page: Page) -> list[str]:
    """Return LegislationDetail URLs for matters whose File-Created date is in [start, end].

    Uses Advanced search (the simple search caps at 100 with no pager). Keep windows ~weekly so
    each query stays under the cap. Telerik gotcha: RadDatePicker commits on blur -> type + Tab.
    """
    page.goto(BASE + "Legislation.aspx", wait_until="networkidle", timeout=60_000)
    page.click(CP + "btnSwitch")  # -> Advanced search
    page.wait_for_load_state("networkidle", timeout=60_000)
    page.check("input[name='ctl00$ContentPlaceHolder1$radFileCreated'][value='between']")
    dp = page.locator("input.riTextBox")  # 0/1 = File-Created from/to
    for idx, d in ((0, start), (1, end)):
        dp.nth(idx).click()
        dp.nth(idx).press_sequentially(d.strftime("%-m/%-d/%Y"), delay=15)
        dp.nth(idx).press("Tab")
    page.wait_for_timeout(300)
    page.click("#visibleSearchButton")
    page.wait_for_load_state("networkidle", timeout=60_000)

    reported = re.search(r"(\d[\d,]*)\s+records?", page.inner_text("body"))
    if reported and int(reported.group(1).replace(",", "")) >= RESULT_CAP:
        log.warning("slice %s..%s hit the %d cap -> narrow the window", start, end, RESULT_CAP)
    urls = page.eval_on_selector_all(
        f"{CP}gridMain_ctl00 a",
        "els => els.map(a => a.getAttribute('href'))"
        ".filter(h => h && h.includes('LegislationDetail.aspx'))")
    if len(urls) >= RESULT_CAP:
        log.warning("slice %s..%s returned >= %d rows — likely truncated; narrow the window",
                    start, end, RESULT_CAP)
    return [BASE + h.replace("&amp;", "&") for h in dict.fromkeys(urls)]


# --------------------------------------------------------------------------- orchestration / CLI
def _weekly(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur, min(cur + timedelta(days=6), end)
        cur += timedelta(days=7)


def read_agenda_matter_urls(bronze_dir: Path) -> list[str]:
    """Distinct LegislationDetail URLs for every matter on a scraped meeting agenda (the discovery
    feed), order-preserving and de-duplicated. Read straight from AgendaItem.matter_url — no per-file
    browser search, and it resolves matters from any year.
    """
    urls: list[str] = []
    seen: set[str] = set()
    items_seen = 0
    for path in sorted(bronze_dir.glob("*.json")):
        if path.stem == "_index":
            continue
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log.warning("skip unreadable meeting bronze %s: %s", path, e)
            continue
        for it in raw.get("agenda_items", []):
            items_seen += 1
            url = (it.get("matter_url") or "").strip()
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    if items_seen and not urls:
        log.warning("%s has agenda items but no matter_url — re-run the meeting scraper "
                    "(matter_url was added with the Option-2 discovery feed)", bronze_dir)
    return urls


def _write_matter(m: Matter, out_dir: Path) -> None:
    """Write one matter's raw bronze JSON (pure dataclass — no derived fields; silver computes those)."""
    if m.file_number:
        (out_dir / f"{m.file_number}.json").write_text(
            json.dumps(dataclasses.asdict(m), indent=2, ensure_ascii=False))


def collect(file_number=None, start=None, end=None, with_text=False,
            agenda_urls=None, out_dir=None) -> list[Matter]:
    # Agenda matters are LegislationDetail URLs straight from the meeting bronze -> scraped with plain
    # requests, NO browser. Only the file-# lookup and the date-window enumeration drive Playwright.
    urls: list[str] = [] if file_number else list(agenda_urls or [])
    if file_number or (start and end):
        from playwright.sync_api import sync_playwright   # lazy: only search/enumeration needs a browser
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=UA)
            if file_number:
                urls = [find_matter_url(file_number, page)]
            else:
                for ws, we in _weekly(start, end):
                    slice_urls = enumerate_matters(ws, we, page)
                    log.info("slice %s..%s -> %d matters", ws, we, len(slice_urls))
                    urls += slice_urls
            browser.close()
    # De-dup by matter id: the same matter can arrive via BOTH the agenda feed and the date-window
    # enumeration with different query params — same ID= means the same matter, so scrape it once.
    seen: set[str] = set()
    deduped: list[str] = []
    for u in urls:
        mid = re.search(r"[?&]ID=(\d+)", u)
        key = mid.group(1) if mid else u
        if key not in seen:
            seen.add(key)
            deduped.append(u)
    urls = deduped

    matters: list[Matter] = []
    for i, url in enumerate(urls, 1):
        try:                                                         # one bad matter never aborts the batch
            m = scrape_matter(url, with_text=with_text)
        except Exception as e:                                       # noqa: BLE001
            log.warning("[%d/%d] FAILED %s: %s — skipping", i, len(urls), url, e)
            continue
        log.info("[%d/%d] %s | %-26s | %-8s | %d actions | votes:%d",
                 i, len(urls), m.file_number, (m.status or "")[:26], m.lifecycle,
                 len(m.actions), sum(len(a.votes) for a in m.actions))
        matters.append(m)
        if out_dir is not None:           # incremental write: a late failure keeps already-scraped matters
            _write_matter(m, out_dir)
    return matters


def main() -> None:
    ap = argparse.ArgumentParser(description="Consolidated SF Legistar scraper")
    ap.add_argument("--file", help="single file number, e.g. 260388")
    ap.add_argument("--from", dest="start", help="introduced-date start YYYY-MM-DD")
    ap.add_argument("--to", dest="end", help="introduced-date end YYYY-MM-DD")
    ap.add_argument("--with-text", action="store_true", help="download + extract Leg Ver PDFs")
    ap.add_argument("--out", help="write results as JSON array to this path")
    ap.add_argument("--raw-dir", dest="raw_dir",
                    help="write one JSON file per matter to this directory (raw landing zone)")
    ap.add_argument("--agenda-bronze", dest="agenda_bronze",
                    help="meeting bronze dir (raw/meetings/ingest_date=...) whose agenda matter_files "
                         "are ALSO scraped — the Option-2 discovery feed (run the meeting scraper first)")
    args = ap.parse_args()

    out_dir = None
    if args.raw_dir:
        out_dir = Path(args.raw_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

    agenda_urls = None
    if args.agenda_bronze:
        agenda_urls = read_agenda_matter_urls(Path(args.agenda_bronze))
        log.info("agenda feed: %d distinct matter URL(s) from %s",
                 len(agenda_urls), args.agenda_bronze)

    if args.file:
        matters = collect(file_number=args.file, with_text=args.with_text, out_dir=out_dir)
    elif (args.start and args.end) or args.agenda_bronze:
        s = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else None
        e = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None
        matters = collect(start=s, end=e, with_text=args.with_text,
                          agenda_urls=agenda_urls, out_dir=out_dir)
    else:
        ap.error("provide --file, OR --from/--to, OR --agenda-bronze")

    if args.raw_dir:                      # bronze already written incrementally inside collect()
        log.info("wrote %d matter files -> %s",
                 sum(1 for m in matters if m.file_number), args.raw_dir)
    elif args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump([dataclasses.asdict(m) for m in matters], f, indent=2)
        log.info("wrote %d matters -> %s", len(matters), args.out)
    else:
        for m in matters:                 # stdout: human debug view (truncated text + derived lifecycle)
            d = dataclasses.asdict(m)
            d["full_text"] = f"<{len(m.full_text)} chars>" if m.full_text else None
            d["lifecycle"] = m.lifecycle
            print(json.dumps(d, indent=2))


if __name__ == "__main__":
    main()
