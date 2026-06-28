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
# ponytail: --current-month is GET-only and covers this month; a window reaching into
# last month (first days of a month) misses prior-month meetings. Upgrade path if that
# bites: --year "$(date -u +%Y)" (Playwright enum, already in this image).
python -m scrape.legistar_meetings --current-month --from "$FROM" --to "$DATE" \
    --raw-dir "$RAW_ROOT/meetings" --date "$DATE"

echo ">> [2/2] matters   $FROM .. $DATE  (File-Created window + agenda feed)"
python -m scrape.legistar_scrape --from "$FROM" --to "$DATE" \
    --agenda-bronze "$RAW_ROOT/meetings/ingest_date=$DATE" \
    --raw-dir "$RAW_ROOT/matters" --date "$DATE"

echo ">> wrote ingest_date=$DATE under $RAW_ROOT"
# ponytail: GCS upload lands with the target infra (job SA + cotc_raw IAM):
#   gcloud storage rsync -r "$RAW_ROOT" gs://cotc_raw
