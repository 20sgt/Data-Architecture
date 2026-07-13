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

# Month-boundary guard: the browserless "This Month" GET above only lists the current
# month's rows, so a window reaching into the previous month also needs the Playwright
# year enumeration for FROM's year (chromium ships in this image). Dec->Jan works too:
# FROM's year is the prior year, and the current-month pass covers the January side.
# ponytail: a whole-year enumeration for <=7 days of rows, ~once a month; swap to
# webapi /events window enumeration if that minute ever matters.
if [ "${FROM%-*}" != "${DATE%-*}" ]; then
    echo ">> [1b] window spans months - year pass for ${FROM%%-*}"
    python -m scrape.legistar_meetings --year "${FROM%%-*}" --from "$FROM" --to "$DATE" \
        --raw-dir "$RAW_ROOT/meetings" --date "$DATE"
fi

echo ">> [2/2] matters   $FROM .. $DATE  (File-Created window + agenda feed)"
python -m scrape.legistar_scrape --from "$FROM" --to "$DATE" \
    --agenda-bronze "$RAW_ROOT/meetings/ingest_date=$DATE" \
    --raw-dir "$RAW_ROOT/matters" --date "$DATE"

echo ">> wrote ingest_date=$DATE under $RAW_ROOT"
