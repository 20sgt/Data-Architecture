# Data Footprint — Podcast Bronze Layer (M1)

Documented for the Corn Off the Cobb Data Architecture milestone: raw layer in the cloud, with source / format / size / update pattern.

## Cloud location

| Property | Value |
|----------|--------|
| GCP project | `corn-off-the-cobb` |
| Bucket | `gs://podcasts-audio-files` |
| Root prefix | `podcasts/` |

## Sources

| Source | Shows (slugs) | Access | Format in bronze |
|--------|---------------|--------|------------------|
| Megaphone RSS | `fifth-and-mission`, `extra-spicy`, `giants-splash-as-plus`, `datebook`, `the-doodler`, `warriors-off-court`, `fixing-our-city`, `chronicled-kamala-harris` | Public RSS → MP3 URL | MP3 + JSON metadata |
| Podbean RSS | `voice-of-san-francisco` | Public RSS → MP3 URL | MP3 + JSON metadata |

Feed URLs are defined in `ingest.py` (`SHOW_FEEDS`).

## Bronze objects

| Path | Format | Contents | Update pattern |
|------|--------|----------|----------------|
| `podcasts/audio/{show}/{episode_id}.mp3` | MP3 | Raw episode audio | Append-only on new RSS items |
| `podcasts/metadata/{show}/{episode_id}.json` | JSON | Title, description, pub_date, guid, GCS URI, ingest time | Written once per episode |
| `podcasts/_manifest.json` | JSON | Dedup index of ingested episode IDs | Updated each ingest |

Episode IDs are stable hashes of the RSS GUID (16-char hex), so re-runs do not duplicate audio.

## Downstream (not bronze, but footprint-adjacent)

| Path | Format | Role | Update pattern |
|------|--------|------|----------------|
| `podcasts/transcripts/` | JSON | Legacy transcripts (undisturbed) | Frozen for new work |
| `podcasts/transcripts_whisper/` | JSON | Whisper transcripts | Local Whisper; skip if exists |
| `podcasts/enrichment/` | JSON | Entity extraction | After new transcripts |
| `podcasts/silver/*.jsonl` | JSONL | Query tables | Rebuild after enrich |

## Approximate scale (as of mid–late 2026 pipeline runs)

| Metric | Approx. value |
|--------|----------------|
| Episodes / audio files | ~1,860+ |
| Shows | 9 |
| Whisper transcripts | Full corpus under `transcripts_whisper/` |
| Enrichment / silver | Built for usable episodes |

Re-measure anytime:

```bash
# Object counts under prefixes (requires gcloud / GCS client)
# audio mp3 count ≈ bronze episode count
```

Audio size is dominated by MP3s (typically tens of GB for the full backfill). Metadata and JSON layers are small relative to audio.

## Update cadence

| Job | Where | Schedule |
|-----|-------|----------|
| Ingest new episodes | Cloud Run Job `podcast-weekly-pipeline` | Sunday 03:00 America/Los_Angeles |
| Whisper backfill / catch-up | Local laptop | As needed after ingest |
| Enrich + silver | Local and/or Cloud Run (after transcripts exist) | After new Whisper files |

## Provenance fields (metadata JSON)

Each episode metadata record includes at least:

- `episode_id`, `show_slug`, `title`, `description`
- `pub_date`, `guid`, `source_url`
- `gcs_uri`, `ingested_at`

This is enough to cite a podcast episode from any silver query row.
