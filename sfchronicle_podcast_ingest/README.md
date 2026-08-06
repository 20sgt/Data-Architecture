# Podcast pipeline

Turns SF Chronicle / Voice of SF podcasts into searchable silver tables
(bills, people, topics) without Google Speech-to-Text.

```text
RSS → GCS audio/metadata (bronze)
    → Whisper transcripts_whisper/ (silver text)
    → enrichment/ + silver/*.jsonl + SQLite (silver entities)
```

## Setup

```bash
python3 -m venv .venv
./.venv/bin/python3 -m pip install -r requirements.txt
cp .env.example .env   # set GCP_SERVICE_ACCOUNT_KEY
chmod +x run_*.sh deploy_cloud.sh
```

## Run

| Step | Command |
|------|---------|
| All local | `./run_local_pipeline.sh` |
| Ingest | `./.venv/bin/python3 ingest.py` |
| Transcribe | `./run_transcribe.sh` |
| Enrich | `./run_enrich.sh` |
| Silver | `./run_silver.sh` |
| Query | `./.venv/bin/python3 query_silver.py --bill prop_c` |

On Cloud Run the attached service account replaces `GCP_SERVICE_ACCOUNT_KEY`.

## Cloud (Sunday 03:00 PT)

`ingest → Whisper tiny → enrich → silver`. Spend caps: **5 episodes**, **20 min**, **~$0.25**, job timeout **30m**.

```bash
./deploy_cloud.sh                 # schedule only
./deploy_cloud.sh --execute-now   # optional test
```

## Docs

- [Data footprint (M1)](docs/DATA_FOOTPRINT.md)
- [Deep-dive plan (M2)](docs/DEEP_DIVE_PLAN.md)

## Tests

```bash
./.venv/bin/python3 -m pytest -q
```
