# SF Chronicle Podcast Ingest and Transcription Pipeline

This project downloads podcast episodes (SF Chronicle Megaphone feeds + Voice of San Francisco) into Google Cloud Storage, then creates transcripts for any audio files that do not already have transcript JSON files.

## Storage Layout

```text
podcasts/
  audio/{show_slug}/{episode_id}.mp3
  metadata/{show_slug}/{episode_id}.json
  transcripts/{show_slug}/{episode_id}.json          # legacy (undisturbed)
  transcripts_whisper/{show_slug}/{episode_id}.json  # Whisper-only ($0 STT)
  enrichment/{show_slug}/{episode_id}.json           # bills / people / topics
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

Local query DB (free):

```text
data/podcast_silver.sqlite
```

The ingest step uses `_manifest.json` and stable episode IDs to avoid duplicate audio uploads. New transcription writes only to `podcasts/transcripts_whisper/` using **local Whisper** (free inference). It does **not** use Google Cloud Speech-to-Text and does **not** read or overwrite `podcasts/transcripts/`.

## Setup

```bash
cd "/Users/sgt/MSDS/Summer/Data Architecture/Final Project/sfchronicle_podcast_ingest"
python3 -m venv .venv
./.venv/bin/python3 -m pip install -r requirements.txt
```

Make sure `.env` contains:

```text
GCP_PROJECT_ID=corn-off-the-cobb
GCP_BUCKET_NAME=podcasts-audio-files
GCP_SERVICE_ACCOUNT_KEY=/path/to/service-account.json
TRANSCRIPTION_LANGUAGE_CODE=en
WHISPER_MODEL=base
TRANSCRIPT_PREFIX=podcasts/transcripts_whisper
```

On Cloud Run, omit `GCP_SERVICE_ACCOUNT_KEY` and use the attached service account instead.

## Pipeline overview (all $0 paid APIs)

| Step | Script | Cost | Where |
|------|--------|------|-------|
| Ingest RSS → GCS | `ingest.py` | GCS storage only | Local or Cloud |
| Transcribe | `transcribe.py` | Local CPU Whisper | **Local only** |
| Enrich bills/people/topics | `enrich.py` | Rule-based, free | Local or Cloud |
| Silver query tables | `silver.py` | SQLite + GCS JSONL | Local or Cloud |

## Run locally (recommended full loop)

```bash
chmod +x run_*.sh
./run_local_pipeline.sh
# or step by step:
./.venv/bin/python3 ingest.py
./run_transcribe.sh
./run_enrich.sh
./run_silver.sh
```

Query the silver layer (no cloud needed after build):

```bash
./.venv/bin/python3 query_silver.py
./.venv/bin/python3 query_silver.py --bill prop_c
./.venv/bin/python3 query_silver.py --topic homelessness
./.venv/bin/python3 query_silver.py --person scott_wiener
./.venv/bin/python3 query_silver.py --topic housing --show fixing-our-city
```

Example SQL against `data/podcast_silver.sqlite`:

```sql
SELECT e.title, b.bill_ref, b.quote
FROM episode_bills b
JOIN episodes e USING (episode_id)
WHERE b.bill_normalized = 'prop_c' AND e.usable = 1;
```

Normalized keys ignore spelling variants (`Prop C` / `Proposition C` → `prop_c`).

## Enrich transcripts for querying

After transcription, run enrichment to extract bills, people, topics, stance, and claims:

```bash
./run_enrich.sh --limit 10
./run_enrich.sh --show fifth-and-mission --limit 20
./run_enrich.sh
```

People lexicon: `data/representatives.json` (edit to add supervisors / reps). Output lands in:

```text
podcasts/enrichment/{show_slug}/{episode_id}.json
```

Each enrichment file includes:
- `bills` (Prop/AB/SB/ordinance/file refs + quote windows + `normalized`)
- `people` (known officials/hosts + contextual names + `normalized`)
- `topics` (homelessness, housing, transit, covid, etc.)
- `stances` (supports / opposes / concerned / neutral)
- `claims` (sentence-level statements tied to topics)
- `summary_fields` for quick filtering
- `quality` (`usable=false` for bad ASR/music-only transcripts)

This step is local/rule-based and does not call paid APIs.

## Silver layer

```bash
./run_silver.sh                 # SQLite + GCS JSONL
./run_silver.sh --local-only    # laptop SQLite only
./run_silver.sh --gcs-only      # bucket tables only (Cloud Run)
```

## Run weekly in the cloud (after Whisper transcripts exist)

Cloud Scheduler runs Sundays at 3:00 AM Pacific:

1. Ingest new podcasts
2. Enrich any episodes that already have Whisper transcripts
3. Rebuild silver JSONL in GCS

Whisper stays local so you avoid Speech-to-Text charges. Typical weekly flow:

1. Cloud job ingests + enriches + rebuilds silver for whatever transcripts exist
2. On your laptop (when convenient): `./run_transcribe.sh` then `./run_enrich.sh` then `./run_silver.sh`

```bash
chmod +x deploy_cloud.sh run_cloud_pipeline.sh
./deploy_cloud.sh
```

What the deploy script does:

1. Enables Cloud Run, Cloud Scheduler, Cloud Build, and Storage APIs
2. Grants the service account Storage Object Admin
3. Builds/pushes the Docker image (ingest + enrich + silver)
4. Creates/updates Cloud Run Job `podcast-weekly-pipeline`
5. Creates/updates Cloud Scheduler job `podcast-weekly-trigger` (`0 3 * * 0`)

Useful commands:

```bash
GCLOUD="/Users/sgt/MSDS/Spring/Spring Module 2/MLOps/google-cloud-sdk/bin/gcloud"

# Manual run
$GCLOUD run jobs execute podcast-weekly-pipeline --region us-west1

# Check executions
$GCLOUD run jobs executions list --job podcast-weekly-pipeline --region us-west1

# Check scheduler
$GCLOUD scheduler jobs describe podcast-weekly-trigger --location us-west1
```

## Tests

```bash
./.venv/bin/python3 -m pytest -q
```

## Schedule weekly on macOS (optional local backup)

The included `com.sfchronicle.podcast_pipeline.plist` runs every Sunday at 3:00 AM local time.

```bash
chmod +x run_weekly_pipeline.sh
mkdir -p logs
cp com.sfchronicle.podcast_pipeline.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.sfchronicle.podcast_pipeline.plist
```
