# `scrape/` — SF Legistar scrapers

Two independent slices that scrape `sfgov.legistar.com` HTML into raw **bronze** JSON
(one file per entity). Each module's docstring has the detail; this is the map + how to run.

## Modules


| File                   | What it does                                                                                                                                                                |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `fetch.py`             | Shared HTTP layer: rate-limited `requests` session, retry/backoff, the process-global rate gate (`_throttle`), and PDF text extraction. All network goes through here.      |
| `legistar_meetings.py` | **Meeting slice.** Calendar → MeetingDetail. Produces `dim_meeting` rows + the agenda **discovery feed** (each agenda item's `matter_url` + `history_id`). No votes.        |
| `legistar_scrape.py`   | **Legislation slice.** Date-window search + the agenda feed → each Matter's metadata, actions, attachments, and per-member roll-call votes. Sole producer of the fact data. |
| `history_detail.py`    | Pure parser for a HistoryDetail page → the per-member roll-call (`Vote`s). Called by the legislation slice.                                                                 |
| `tests/`               | Offline test suite (no network/browser). `fixtures/` holds committed live-HTML captures (ground truth — don't edit) plus one synthetic LegislationDetail page; `test_*.py` lock the parsers, the pure helpers, and the orchestration plumbing against them. |


Playwright (headless chromium) drives **only** the postback enumeration (calendar year, legislation
date search); everything else is plain `requests` + BeautifulSoup.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium        # once — needed for enumeration
```

## Run

**Order matters: meetings first** (the legislation slice reads their agenda feed via `--agenda-bronze`).
Output lands in `raw/<entity>/ingest_date=YYYY-MM-DD/` (gitignored; partition = scrape date). The JSON
shape is the bronze contract documented in `[../sample/README.md](../sample/README.md)`.

```bash
# Backfill a date range (e.g. YTD 2026). --year pages the full calendar; the search auto-bisects any
# week over Legistar's 100-row cap.
python -m scrape.legistar_meetings --year 2026 --from 2026-01-01 --to 2026-06-26 --raw-dir raw/meetings
python -m scrape.legistar_scrape  --from 2026-01-01 --to 2026-06-26 \
    --agenda-bronze raw/meetings/ingest_date=$(date +%F) \
    --raw-dir raw/matters/ingest_date=$(date +%F)

# Weekly run: drop --year (the current-month calendar is a plain GET, no browser).
python -m scrape.legistar_meetings --from 2026-06-22 --to 2026-06-28 --raw-dir raw/meetings
python -m scrape.legistar_scrape  --from 2026-06-22 --to 2026-06-28 \
    --agenda-bronze raw/meetings/ingest_date=$(date +%F) \
    --raw-dir raw/matters/ingest_date=$(date +%F)
```

Re-running the same command **resumes** — entities already on disk are skipped. Single-entity debug:
`--event <id> --guid <guid>` (meeting) or `--file <number>` (matter); add `--with-text` to pull PDF text.

## Bronze layout & coverage

One JSON per entity, named by its natural key, partitioned by *scrape* date — event dates live
inside each record (matter file numbers are year-prefixed: `020997` = matter 02-0997, filed 2002):

```
gs://cotc_raw/matters/ingest_date=YYYY-MM-DD/<file_number>.json
gs://cotc_raw/meetings/ingest_date=YYYY-MM-DD/<meeting_id>.json
```

| Partition | Contents |
|---|---|
| `ingest_date=2026-06-26` | Original YTD scrape (Jan–Jun 2026), no PDF text |
| `ingest_date=2026-07-11` | **Deep-history backfill: 2000-01-01 → 2026-07-11, with PDF text** — ~38.7k matters + ~4.7k meetings; where it overlaps 06-26, gold's latest-wins dedup prefers these |
| `ingest_date=2026-07-12` | First verified Cloud Run Job execution (trailing-week window) |
| `ingest_date=<Wednesdays>` | One partition per scheduled weekly run (Cloud Scheduler → Cloud Run Job, Wed 06:00 PT) |

Bronze is **append-only**: old partitions are point-in-time history, not duplicates — silver
ingests every file 1:1 (lineage columns `ingest_date` / `source_file` / `loaded_at`) and gold
keeps each entity's latest scrape, so superseded partitions cost nothing downstream. Don't
delete them.

## Test

```bash
pytest scrape/        # offline, no network/browser; also runs in CI
```

