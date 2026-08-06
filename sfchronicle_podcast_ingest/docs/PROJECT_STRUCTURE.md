# Project structure (podcast package)

All audio-branch documentation lives **inside** `sfchronicle_podcast_ingest/` so root `README.md` / `docs/` on `main` stay untouched.

```text
sfchronicle_podcast_ingest/
├── README.md
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DATA_FOOTPRINT.md
│   ├── DEEP_DIVE_PLAN.md
│   └── PROJECT_STRUCTURE.md
├── ingest.py
├── transcribe.py
├── enrich.py
├── silver.py
├── query_silver.py
├── data/representatives.json
├── tests/
├── Dockerfile
├── deploy_cloud.sh
└── run_*.sh
```

Repo root on `audio-processing` may still contain `.gitignore` (combined with `main`) and the shared project `README.md` from `main` so merges do not overwrite the team overview.

Do not commit:

- `.env` or service-account JSON keys
- `.venv/`, `logs/`, local `*.sqlite`
- Large audio / transcript dumps (they live in GCS)
