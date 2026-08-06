# SF Chronicle Podcast Pipeline

Civic podcast ingestion and enrichment for San Francisco / Bay Area politics (Corn Off the Cobb **audio** workstream).

Turns SF Chronicle and Voice of San Francisco podcasts into a **queryable silver layer**: bills, representatives, topics, stances, and citeable quotes — without Google Speech-to-Text or paid LLM APIs.

## Why this exists

Podcasts discuss propositions, supervisors, housing, and homelessness in plain language, but audio is not searchable. This package:

1. **Ingests** episode audio + metadata into Google Cloud Storage (bronze)
2. **Transcribes** with local Whisper (free inference)
3. **Extracts** civic entities with rule-based enrichment
4. **Publishes** flat tables for SQL / dashboards / future RAG or MCP tools

## Architecture (medallion)

```text
RSS feeds
   │
   ▼
┌──────────────────────────────────────────────────────────┐
│  BRONZE  GCS: podcasts/audio + podcasts/metadata         │
└──────────────────────────────────────────────────────────┘
   │  local Whisper (faster-whisper)
   ▼
┌──────────────────────────────────────────────────────────┐
│  SILVER transcripts  podcasts/transcripts_whisper/       │
└──────────────────────────────────────────────────────────┘
   │  enrich.py (regex + lexicons)
   ▼
┌──────────────────────────────────────────────────────────┐
│  SILVER entities  podcasts/enrichment/                   │
│  + SQLite / JSONL  podcasts/silver/  (query tables)      │
└──────────────────────────────────────────────────────────┘
```

| Layer | Contents | Location |
|-------|----------|----------|
| Bronze | MP3 audio, episode metadata JSON | `gs://…/podcasts/audio`, `…/metadata` |
| Silver (text) | Whisper transcripts | `…/podcasts/transcripts_whisper` |
| Silver (entities) | Bills, people, topics, stances, claims | `…/enrichment` + `…/silver/*.jsonl` |
| Local query | Same tables as SQLite | `data/podcast_silver.sqlite` |

## Package layout

```text
sfchronicle_podcast_ingest/
├── README.md                 ← you are here
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DATA_FOOTPRINT.md     # M1 bronze footprint
│   ├── DEEP_DIVE_PLAN.md     # M2 plan + KPIs
│   └── PROJECT_STRUCTURE.md
├── ingest.py / transcribe.py / enrich.py / silver.py
├── query_silver.py
├── data/representatives.json
├── tests/
└── run_*.sh / deploy_cloud.sh / Dockerfile
```

## Quick start

```bash
cd sfchronicle_podcast_ingest
python3 -m venv .venv
./.venv/bin/python3 -m pip install -r requirements.txt
cp .env.example .env
# Edit .env: set GCP_SERVICE_ACCOUNT_KEY to your service-account JSON path

chmod +x run_*.sh deploy_cloud.sh
./run_local_pipeline.sh
```

### Pipeline commands

| Step | Command | Notes |
|------|---------|--------|
| Ingest | `./.venv/bin/python3 ingest.py` | RSS → GCS bronze |
| Transcribe | `./run_transcribe.sh` | Local Whisper only ($0 STT) |
| Enrich | `./run_enrich.sh` | Bills / people / topics |
| Silver | `./run_silver.sh` | SQLite + GCS JSONL |
| Full local | `./run_local_pipeline.sh` | All of the above |
| Query | `./.venv/bin/python3 query_silver.py --bill prop_c` | Local SQLite |

Required `.env` values:

```text
GCP_PROJECT_ID=corn-off-the-cobb
GCP_BUCKET_NAME=podcasts-audio-files
GCP_SERVICE_ACCOUNT_KEY=/absolute/path/to/service-account.json
TRANSCRIPT_PREFIX=podcasts/transcripts_whisper
```

On Cloud Run, omit `GCP_SERVICE_ACCOUNT_KEY` and use the job’s attached service account.

## Cost model

| Component | Charge? |
|-----------|---------|
| Local Whisper transcription | **No** |
| Rule-based enrichment | **No** |
| SQLite / GCS JSONL silver | **No** paid analytics APIs |
| Google Speech-to-Text | **Not used** |
| GCS storage + weekly Cloud Run ingest | Standard GCP storage / small job cost only |

## Shows ingested

| Slug | Source |
|------|--------|
| `fifth-and-mission` | Megaphone |
| `fixing-our-city` | Megaphone |
| `extra-spicy` | Megaphone |
| `datebook` | Megaphone |
| `the-doodler` | Megaphone |
| `giants-splash-as-plus` | Megaphone |
| `warriors-off-court` | Megaphone |
| `chronicled-kamala-harris` | Megaphone |
| `voice-of-san-francisco` | Podbean |

Civic querying is strongest on **Fifth & Mission**, **Fixing Our City**, and **Voice of San Francisco**.

## GCS layout

```text
podcasts/
  audio/{show_slug}/{episode_id}.mp3
  metadata/{show_slug}/{episode_id}.json
  transcripts/{show_slug}/{episode_id}.json          # legacy (never overwritten)
  transcripts_whisper/{show_slug}/{episode_id}.json  # Whisper-only
  enrichment/{show_slug}/{episode_id}.json
  silver/*.jsonl
  _manifest.json
```

Local DB: `data/podcast_silver.sqlite` (gitignored).

## Querying

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

## Cloud weekly job

Scheduled **only** Sunday 03:00 America/Los_Angeles (Cloud Scheduler).  
Pipeline: ingest → budget-capped Whisper (`tiny`/CPU) → enrich → silver JSONL.

**No Google Speech-to-Text.** Hard stops keep spend near zero:

| Guard | Default |
|-------|---------|
| `WHISPER_MAX_EPISODES` | 5 new episodes / run |
| `WHISPER_MAX_RUNTIME_MINUTES` | 20 minutes |
| `WHISPER_BUDGET_USD` | ~\$0.25 estimated Cloud Run CPU |
| Job `--task-timeout` | 30 minutes (platform kill switch) |

```bash
./deploy_cloud.sh                 # build + update schedule (does not run now)
./deploy_cloud.sh --execute-now   # optional manual test only
```

Resources: **1 vCPU / 2 GiB**. Idempotent — already-transcribed audio is skipped.


## Documentation

- [Architecture & cost model](docs/ARCHITECTURE.md)
- [Data footprint (M1)](docs/DATA_FOOTPRINT.md)
- [Deep-dive plan & KPIs (M2)](docs/DEEP_DIVE_PLAN.md)
- [Project structure](docs/PROJECT_STRUCTURE.md)

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
