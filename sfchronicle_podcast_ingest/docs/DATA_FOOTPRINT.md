# Data footprint — podcasts (M1)

| | |
|--|--|
| Project | `corn-off-the-cobb` |
| Bucket | `gs://podcasts-audio-files` |
| Prefix | `podcasts/` |

## Sources

| Source | Shows | Format | Update |
|--------|-------|--------|--------|
| Megaphone RSS | 8 SF Chronicle shows | MP3 + JSON metadata | New episodes on weekly ingest |
| Podbean RSS | `voice-of-san-francisco` | MP3 + JSON metadata | Same |

Feeds: `SHOW_FEEDS` in `ingest.py`.

## Bronze objects

| Path | What | Pattern |
|------|------|---------|
| `podcasts/audio/{show}/{id}.mp3` | Audio | Append-only |
| `podcasts/metadata/{show}/{id}.json` | Title, dates, URLs | One write per episode |
| `podcasts/_manifest.json` | Dedup index | Updated each ingest |

`episode_id` = first 16 hex chars of SHA-256(RSS guid).

## Scale / cadence

~1,860+ episodes across 9 shows. Audio dominates size (tens of GB).

| Job | Schedule |
|-----|----------|
| Cloud Run `podcast-weekly-pipeline` | Sunday 03:00 America/Los_Angeles |
| Steps | ingest → Whisper (budget-capped) → enrich → silver |
