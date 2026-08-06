# Corn Off the Cobb — Podcast Data Pipeline

Civic podcast ingestion and enrichment for San Francisco / Bay Area politics.

This branch turns SF Chronicle and Voice of San Francisco podcasts into a **queryable silver layer**: bills, representatives, topics, stances, and citeable quotes — without Google Speech-to-Text or paid LLM APIs.

## Why this exists

Local government information is hard to find. Podcasts discuss propositions, supervisors, housing, and homelessness in plain language, but audio is not searchable. This pipeline:

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
| Silver (entities) | Bills, people, topics, stances, claims | `…/podcasts/enrichment` + `…/silver/*.jsonl` |
| Local query | Same tables as SQLite | `sfchronicle_podcast_ingest/data/podcast_silver.sqlite` |

## Repository layout

```text
.
├── README.md                          ← you are here
├── docs/
│   ├── ARCHITECTURE.md                Pipeline design & cost model
│   ├── DATA_FOOTPRINT.md              Bronze sources (M1)
│   ├── DEEP_DIVE_PLAN.md              Evaluation & next steps (M2)
│   └── PROJECT_STRUCTURE.md           What belongs on this branch
└── sfchronicle_podcast_ingest/        Runnable pipeline package
    ├── ingest.py / transcribe.py / enrich.py / silver.py
    ├── query_silver.py
    ├── data/representatives.json
    ├── tests/
    └── README.md                      Setup & commands
```

## Quick start

```bash
cd sfchronicle_podcast_ingest
python3 -m venv .venv
./.venv/bin/python3 -m pip install -r requirements.txt
cp .env.example .env   # set GCP_SERVICE_ACCOUNT_KEY to your key path

chmod +x run_*.sh
./run_local_pipeline.sh
```

Query examples (after silver build):

```bash
./.venv/bin/python3 query_silver.py --bill prop_c
./.venv/bin/python3 query_silver.py --topic homelessness
./.venv/bin/python3 query_silver.py --person scott_wiener
```

## Cost model

| Component | Charge? |
|-----------|---------|
| Local Whisper transcription | **No** (CPU / free model download) |
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

## Documentation

- [Architecture & cost model](docs/ARCHITECTURE.md)
- [Data footprint (M1)](docs/DATA_FOOTPRINT.md)
- [Deep-dive plan & KPIs (M2)](docs/DEEP_DIVE_PLAN.md)
- [Project structure](docs/PROJECT_STRUCTURE.md)
- [Pipeline package README](sfchronicle_podcast_ingest/README.md)

## Tests

```bash
cd sfchronicle_podcast_ingest
./.venv/bin/python3 -m pytest -q
```

## Branch note

`audio-processing` contains **only** the podcast pipeline and docs. Merge or pull from `main` when you need the rest of the Corn Off the Cobb monorepo (legislation scrape, dbt, etc.).
