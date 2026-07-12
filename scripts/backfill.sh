#!/usr/bin/env bash

# Deep-history backfill: 2026 YTD first (re-land with text), then 2025 -> 2000 newest-first.
# Rerunning resumes — both scrapers skip entities already on disk, so a failed year is
# fixed by running this again. Per-year logs + a one-line-per-year summary in raw/backfill_logs/.
# ponytail: sequential single process on purpose — the fetch.py throttle is per-process,
# so one process = the intended 2 req/s politeness ceiling on sfgov.

set -uo pipefail
cd "$(dirname "$0")/.."   # repo root — raw/, .venv/, and scrape/ live there

PY=.venv/bin/python
INGEST=2026-07-11            # one partition for the whole backfill (the real scrape date)
RAW=raw
FEED=$RAW/meetings/ingest_date=$INGEST
LOGS=$RAW/backfill_logs
mkdir -p "$LOGS"

note() { echo "$1  $(date -u '+%m-%d %H:%M')" >> "$LOGS/summary.txt"; }

note "backfill start"

# 2026 gap-closer: the June YTD scrape lives only in GCS and was text-less; re-land it
# with text locally so the whole history is one consistent, text-bearing corpus.
{ $PY -m scrape.legistar_meetings --year 2026 --from 2026-01-01 --to 2026-07-11 --with-text \
      --raw-dir "$RAW/meetings" --date "$INGEST" \
  && $PY -m scrape.legistar_scrape --from 2026-01-01 --to 2026-07-11 --with-text \
      --agenda-bronze "$FEED" --raw-dir "$RAW/matters" --date "$INGEST"
} >> "$LOGS/2026.log" 2>&1 && note "2026 OK" || note "2026 FAIL"

for Y in $(seq 2025 -1 2000); do
  { $PY -m scrape.legistar_meetings --year "$Y" --all --from "$Y-01-01" --to "$Y-12-31" --with-text \
        --raw-dir "$RAW/meetings" --date "$INGEST" \
    && $PY -m scrape.legistar_scrape --from "$Y-01-01" --to "$Y-12-31" --with-text \
        --agenda-bronze "$FEED" --raw-dir "$RAW/matters" --date "$INGEST"
  } >> "$LOGS/$Y.log" 2>&1 && note "$Y OK" || note "$Y FAIL"
done

note "backfill done"
