"""Consolidated SF Legistar scraper (live site: sfgov.legistar.com).

Replaces the separate spikes (scrape_votes_proof / check_status / enumerate_year + the inline
PDF probe). Extracts everything the project's 4 use cases need:

    enumerate_matters(start, end)  -> matters introduced in a date window (date-sliced search)
    find_matter_url(file_number)   -> resolve one file # to its LegislationDetail URL
    scrape_matter(detail_url)      -> full Matter: subject/abstract, type, status, lifecycle,
                                      controlling committee, sponsors, related files, attachments,
                                      per-member roll-call votes, and (optional) full statutory text

Architecture (proven in spikes):
  * Playwright drives ONLY the ASP.NET/Telerik postback search (enumeration + file-# lookup).
  * Everything else is plain `requests` + BeautifulSoup against GET-able detail pages.
  * Votes and every structured field are parsed DETERMINISTICALLY — never via an LLM. The LLM is
    reserved for summarizing `Matter.full_text` downstream (not in this module).

CLI:
    python legistar_scrape.py --file 260388 [--full-text]
    python legistar_scrape.py --from 2026-05-01 --to 2026-05-14 [--full-text] [--out matters.json]
"""

from __future__ import annotations

import re
import json
import time
import logging
import argparse
import dataclasses
from io import BytesIO
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from playwright.sync_api import sync_playwright, Page

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("legistar")

BASE = "https://sfgov.legistar.com/"
# Polite crawling: identify ourselves and rate-limit (no robots.txt exists, so it's on us).
UA = "Mozilla/5.0 (research; MSDS683 student project; contact lynn.tong.14@gmail.com)"
RATE_LIMIT_S = 1.0
CP = "#ctl00_ContentPlaceHolder1_"
RESULT_CAP = 100  # search cap; weekly slices stay safely under it (SF ~30-100 files/week)

# Status -> lifecycle stage. "Filed" = closed WITHOUT passage (not passed). Extend as the
# vocabulary grows; unknown statuses fall through to "other".
PASSED = {"passed", "approved", "adopted", "finally passed", "ordinance enacted", "mayor approved"}
IN_WORKS = {"first reading", "in committee", "pending committee action", "new business",
            "scheduled for committee hearing", "30 day rule", "for immediate adoption",
            "special order", "assigned", "continued", "pending board action"}

SESSION = requests.Session()
SESSION.headers["User-Agent"] = UA


# --------------------------------------------------------------------------- data model
@dataclasses.dataclass
class Vote:
    person: str
    value: str  # Aye | No | Absent | Excused | Recused


@dataclasses.dataclass
class Action:
    date: str
    body: str
    action: str
    result: str
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


# --------------------------------------------------------------------------- http helpers
def _get(url: str) -> str:
    time.sleep(RATE_LIMIT_S)
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def _get_bytes(url: str) -> bytes:
    time.sleep(RATE_LIMIT_S)
    r = SESSION.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def _split_names(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = re.split(r";|,| and ", raw)
    return [p.strip() for p in parts if p.strip()]


# --------------------------------------------------------------------------- detail parsing
# "Action details" links embed their roll-call page id, e.g. radopen('HistoryDetail.aspx?ID=…').
_RADOPEN = re.compile(r"radopen\('(HistoryDetail\.aspx\?ID=\d+&GUID=[A-F0-9-]+)'", re.I)


def parse_votes(history_url: str) -> list[Vote]:
    """Per-member roll call from a HistoryDetail page (2-col HTML table). Deterministic."""
    soup = BeautifulSoup(_get(history_url), "lxml")
    votes: list[Vote] = []
    for tr in soup.select("table tr"):
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) == 2 and tds[0] and tds[1] in ("Aye", "No", "Absent", "Excused", "Recused"):
            votes.append(Vote(person=tds[0], value=tds[1]))
    return votes


def extract_pdf_text(view_url: str) -> str | None:
    """Download a View.ashx attachment and extract text if it's a PDF (full statutory text)."""
    data = _get_bytes(view_url)
    if data[:4] != b"%PDF":
        return None
    reader = PdfReader(BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def scrape_matter(detail_url: str, with_text: bool = False) -> Matter:
    """Parse a LegislationDetail page into a full Matter (metadata + votes [+ full text])."""
    soup = BeautifulSoup(_get(detail_url), "lxml")

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
    if grid:
        for tr in grid.select("tr"):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            cells = [td.get_text(strip=True) for td in tds]
            m = _RADOPEN.search(" ".join(a.get("onclick", "") for a in tr.find_all("a")))
            hist_url = BASE + m.group(1) if m else None
            votes = parse_votes(hist_url) if hist_url else []
            actions.append(Action(cells[0], cells[2], cells[3], cells[4], hist_url, votes))

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
    file_idx = next((i for i, h in enumerate(headers) if "File" in h), 0)
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
    return [BASE + h.replace("&amp;", "&") for h in dict.fromkeys(urls)]


# --------------------------------------------------------------------------- orchestration / CLI
def _weekly(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur, min(cur + timedelta(days=6), end)
        cur += timedelta(days=7)


def collect(file_number=None, start=None, end=None, with_text=False) -> list[Matter]:
    matters: list[Matter] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        if file_number:
            urls = [find_matter_url(file_number, page)]
        else:
            urls = []
            for ws, we in _weekly(start, end):
                slice_urls = enumerate_matters(ws, we, page)
                log.info("slice %s..%s -> %d matters", ws, we, len(slice_urls))
                urls += slice_urls
            urls = list(dict.fromkeys(urls))
        browser.close()
        # detail scraping is plain requests -> after the browser is closed
        for i, url in enumerate(urls, 1):
            m = scrape_matter(url, with_text=with_text)
            log.info("[%d/%d] %s | %-26s | %-8s | %d actions | votes:%d",
                     i, len(urls), m.file_number, (m.status or "")[:26], m.lifecycle,
                     len(m.actions), sum(len(a.votes) for a in m.actions))
            matters.append(m)
    return matters


def main() -> None:
    ap = argparse.ArgumentParser(description="Consolidated SF Legistar scraper")
    ap.add_argument("--file", help="single file number, e.g. 260388")
    ap.add_argument("--from", dest="start", help="introduced-date start YYYY-MM-DD")
    ap.add_argument("--to", dest="end", help="introduced-date end YYYY-MM-DD")
    ap.add_argument("--full-text", action="store_true", help="download + extract Leg Ver PDFs")
    ap.add_argument("--out", help="write results as JSON to this path")
    args = ap.parse_args()

    if args.file:
        matters = collect(file_number=args.file, with_text=args.full_text)
    elif args.start and args.end:
        s = datetime.strptime(args.start, "%Y-%m-%d").date()
        e = datetime.strptime(args.end, "%Y-%m-%d").date()
        matters = collect(start=s, end=e, with_text=args.full_text)
    else:
        ap.error("provide --file OR both --from and --to")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump([dataclasses.asdict(m) for m in matters], f, indent=2)
        log.info("wrote %d matters -> %s", len(matters), args.out)
    else:
        for m in matters:
            d = dataclasses.asdict(m)
            d["full_text"] = f"<{len(m.full_text)} chars>" if m.full_text else None
            d["lifecycle"] = m.lifecycle
            print(json.dumps(d, indent=2))


if __name__ == "__main__":
    main()
