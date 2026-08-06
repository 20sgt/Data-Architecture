# SF Chronicle Podcast Pipeline

Runnable package for the Corn Off the Cobb **audio branch**: ingest → Whisper → enrich → silver.

Parent overview: [../README.md](../README.md)

## Pipeline steps

| Step | Command | Notes |
|------|---------|--------|
| Ingest | `./.venv/bin/python3 ingest.py` | RSS → GCS bronze |
| Transcribe | `./run_transcribe.sh` | Local Whisper only ($0 STT) |
| Enrich | `./run_enrich.sh` | Bills / people / topics |
| Silver | `./run_silver.sh` | SQLite + GCS JSONL |
| Full local | `./run_local_pipeline.sh` | All of the above |
| Query | `./.venv/bin/python3 query_silver.py --bill prop_c` | Local SQLite |

## Setup

```bash
python3 -m venv .venv
./.venv/bin/python3 -m pip install -r requirements.txt
cp .env.example .env
# Edit .env: set GCP_SERVICE_ACCOUNT_KEY to your service-account JSON path
chmod +x run_*.sh deploy_cloud.sh
```

Required `.env` values:

```text
GCP_PROJECT_ID=corn-off-the-cobb
GCP_BUCKET_NAME=podcasts-audio-files
GCP_SERVICE_ACCOUNT_KEY=/absolute/path/to/service-account.json
TRANSCRIPT_PREFIX=podcasts/transcripts_whisper
```

On Cloud Run, omit `GCP_SERVICE_ACCOUNT_KEY` and use the job’s attached service account.

## GCS layout

```text
podcasts/
  audio/{show_slug}/{episode_id}.mp3
  metadata/{show_slug}/{episode_id}.json
  transcripts/{show_slug}/{episode_id}.json          # legacy (never overwritten)
  transcripts_whisper/{show_slug}/{episode_id}.json  # Whisper-only
  enrichment/{show_slug}/{episode_id}.json
  silver/
    episodes.jsonl
    episode_bills.jsonl
    episode_topics.jsonl
    episode_people.jsonl
    episode_stances.jsonl
    episode_claims.jsonl
    _manifest.json
  _manifest.json
```

Local DB: `data/podcast_silver.sqlite` (gitignored).

## Querying

Normalized keys ignore ASR spelling variants:

```bash
./.venv/bin/python3 query_silver.py
./.venv/bin/python3 query_silver.py --bill prop_c
./.venv/bin/python3 query_silver.py --topic homelessness
./.venv/bin/python3 query_silver.py --person scott_wiener
./.venv/bin/python3 query_silver.py --topic housing --show fixing-our-city
```

```sql
SELECT e.title, b.bill_ref, b.quote
FROM episode_bills b
JOIN episodes e USING (episode_id)
WHERE b.bill_normalized = 'prop_c' AND e.usable = 1;
```

People lexicon: `data/representatives.json`.

Enrichment fields: `bills`, `people`, `topics`, `stances`, `claims`, `summary_fields`, `quality`.

## Cloud weekly job

Ingest + enrich + silver JSONL (no Whisper in cloud):

```bash
./deploy_cloud.sh
```

Schedule: Sunday 03:00 America/Los_Angeles (`podcast-weekly-trigger`).

After new episodes appear, run Whisper locally, then enrich/silver.

## Tests

```bash
./.venv/bin/python3 -m pytest -q
```

## Optional macOS schedule

```bash
mkdir -p logs
cp com.sfchronicle.podcast_pipeline.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.sfchronicle.podcast_pipeline.plist
```
