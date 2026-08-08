#!/usr/bin/env bash

# Weekly SF Legistar scrape. Order matters: meetings first (it produces the agenda
# discovery feed the matter slice consumes), then matters (File-Created window + feed).
# Both write ingest_date=<DATE> partitions under RAW_ROOT. Tunable via env:
#   INGEST_DATE  partition date          (default: today, UTC)
#   WINDOW_FROM  File-Created window start (default: 7 days ago, UTC)
#   RAW_ROOT     output root              (default: /data/raw)

set -euo pipefail

DATE="${INGEST_DATE:-$(date -u +%F)}"
FROM="${WINDOW_FROM:-$(date -u -d '7 days ago' +%F)}"
RAW_ROOT="${RAW_ROOT:-/data/raw}"

echo ">> [1/2] meetings  $FROM .. $DATE"

python -m scrape.legistar_meetings --current-month --from "$FROM" --to "$DATE" \
    --raw-dir "$RAW_ROOT/meetings" --date "$DATE"

# Month-boundary guard: the browserless "This Month" GET above lists only the month
# the scrape RUNS IN, so any window not fully inside that month also needs the
# Playwright year enumeration for FROM's year (chromium ships in this image).
#
# Compare both window ends against the CURRENT month, not against each other. The
# "spans months" test they used to do silently lost meetings on any backfill of a
# past month: --current-month is anchored to today, so re-running the 2026-07-22
# window in August scraped August's (empty) calendar, found nothing, and skipped the
# year pass because both ends agreed it was July. 41 matters landed, 3 meetings did
# not. A year enumeration is year-wide, so one pass covers a window spanning two
# past months too.
#
# Tradeoff: a whole-year enumeration for <=7 days of rows, ~once a month on the
# normal weekly path; swap to webapi /events window enumeration if that minute ever
# matters.
NOW_MONTH="$(date -u +%Y-%m)"
if [ "${FROM%-*}" != "$NOW_MONTH" ] || [ "${DATE%-*}" != "$NOW_MONTH" ]; then
    echo ">> [1b] window outside current month ($NOW_MONTH) - year pass for ${FROM%%-*}"
    python -m scrape.legistar_meetings --year "${FROM%%-*}" --from "$FROM" --to "$DATE" \
        --raw-dir "$RAW_ROOT/meetings" --date "$DATE"
fi

echo ">> [2/2] matters   $FROM .. $DATE  (File-Created window + agenda feed)"
python -m scrape.legistar_scrape --from "$FROM" --to "$DATE" \
    --agenda-bronze "$RAW_ROOT/meetings/ingest_date=$DATE" \
    --raw-dir "$RAW_ROOT/matters" --date "$DATE"

echo ">> wrote ingest_date=$DATE under $RAW_ROOT"
