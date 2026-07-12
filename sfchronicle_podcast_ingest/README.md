# SF Chronicle Podcast Ingest and Transcription Pipeline

This project downloads podcast episodes (SF Chronicle Megaphone feeds + Voice of San Francisco) into Google Cloud Storage, then creates transcripts for any audio files that do not already have transcript JSON files.

## Storage Layout

```text
podcasts/
  audio/{show_slug}/{episode_id}.mp3
  metadata/{show_slug}/{episode_id}.json
  transcripts/{show_slug}/{episode_id}.json
  _manifest.json
```

The ingest step uses `_manifest.json` and stable episode IDs to avoid duplicate audio uploads. The transcription step checks whether `podcasts/transcripts/{show_slug}/{episode_id}.json` exists before calling Speech-to-Text.

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
TRANSCRIPTION_LANGUAGE_CODE=en-US
```

On Cloud Run, omit `GCP_SERVICE_ACCOUNT_KEY` and use the attached service account instead.

## Run Manually (local)

Always use the project venv Python (not Homebrew `python3`):

```bash
./.venv/bin/python3 ingest.py
./.venv/bin/python3 transcribe.py --limit 1
./run_transcribe.sh --limit 1
./run_weekly_pipeline.sh
```

Transcription of a full podcast can take several minutes per episode.

## Run Weekly in the Cloud (recommended)

This deploys a **Cloud Run Job** plus a **Cloud Scheduler** trigger for Sundays at 3:00 AM Pacific.

```bash
chmod +x deploy_cloud.sh run_cloud_pipeline.sh
./deploy_cloud.sh
```

To deploy and run one job immediately:

```bash
./deploy_cloud.sh --execute-now
```

What the deploy script does:

1. Enables Cloud Run, Cloud Scheduler, Cloud Build, Speech-to-Text, and Storage APIs
2. Grants the service account Storage Object Admin + Speech Client
3. Builds a Docker image and pushes it to Container Registry
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

## Schedule Weekly on macOS (optional local backup)

The included `com.sfchronicle.podcast_pipeline.plist` runs every Sunday at 3:00 AM local time.

```bash
chmod +x run_weekly_pipeline.sh
mkdir -p logs
cp com.sfchronicle.podcast_pipeline.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.sfchronicle.podcast_pipeline.plist
```
