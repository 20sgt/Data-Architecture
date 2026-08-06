"""Shared HTTP layer for both scraper slices.

A rate-limited requests session with bounded retry/backoff on transient failures. Both
legistar_meetings.py and legistar_scrape.py fetch through `get` / `get_bytes`, and pull PDF text
through `extract_pdf_text`. Parsing lives elsewhere (history_detail.py + the per-slice parsers);
this module holds the network policy and nothing else.
"""

from __future__ import annotations

import time
import logging
import threading
from io import BytesIO

import requests

log = logging.getLogger("legistar-fetch")

BASE = "https://sfgov.legistar.com/"
UA = "Mozilla/5.0 (research; Data-Architecture project)"
RETRY_STATUS = {429, 500, 502, 503, 504}   # transient — retry; permanent 4xx (e.g. 410) raise at once
MAX_RETRIES = 3
RATE_LIMIT_S = 0.5   # global MIN seconds between request STARTS -> aggregate ceiling of 1/RATE_LIMIT_S req/s

SESSION = requests.Session()
SESSION.headers["User-Agent"] = UA

# Aggregate rate gate. With concurrent scrapers (ThreadPoolExecutor) every fetch passes through
# _throttle, so total load on sfgov stays <= 1/RATE_LIMIT_S req/s REGARDLESS of worker count —
# workers saturate the ceiling and hide network latency, they never multiply it. To go faster,
# lower RATE_LIMIT_S (the politeness knob), not the worker count.
# ponytail: process-global gate; correct for one process. A multi-process run would need a shared
# limiter (e.g. redis token bucket) — not needed while the scraper is a single job.
_rate_lock = threading.Lock()
_next_request_at = 0.0   # time.monotonic() the next request may start


def _throttle() -> None:
    """Block until this thread's globally-spaced slot, keeping aggregate req/s under the ceiling."""
    global _next_request_at
    with _rate_lock:
        now = time.monotonic()
        slot = max(now, _next_request_at)
        _next_request_at = slot + RATE_LIMIT_S
        wait = slot - now
    if wait > 0:
        time.sleep(wait)   # sleep OUTSIDE the lock so other threads can reserve their slots meanwhile


def _request(url: str, timeout: int) -> requests.Response:
    """GET with rate-limit + bounded backoff on transient failures.

    Permanent errors (e.g. 410 from a missing GUID) raise immediately so they aren't retried."""
    last: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
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
                            e, url, attempt + 1, MAX_RETRIES)
                time.sleep(RATE_LIMIT_S * 2 * attempt)
    raise last  # type: ignore[misc]


def get(url: str) -> str:
    return _request(url, timeout=30).text


def get_bytes(url: str) -> bytes:
    return _request(url, timeout=60).content


def extract_pdf_text(url: str) -> str | None:
    """Download a View.ashx document and extract its text if it is a PDF."""
    from pypdf import PdfReader            # lazy: only PDF extraction needs it
    data = get_bytes(url)
    if data[:4] != b"%PDF":
        return None
    reader = PdfReader(BytesIO(data))
    return "\n".join(p.extract_text() or "" for p in reader.pages)
