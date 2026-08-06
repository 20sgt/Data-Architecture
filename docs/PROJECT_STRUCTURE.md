# Project structure (audio branch)

This branch is intentionally small: podcast pipeline + documentation only.

```text
.
├── README.md
├── .gitignore
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DATA_FOOTPRINT.md
│   └── DEEP_DIVE_PLAN.md
└── sfchronicle_podcast_ingest/
    ├── ingest.py
    ├── transcribe.py
    ├── enrich.py
    ├── silver.py
    ├── query_silver.py
    ├── data/representatives.json
    ├── tests/
    ├── Dockerfile / deploy_cloud.sh / run_*.sh
    └── README.md
```

Do not commit:

- `.env` or service-account JSON keys
- `.venv/`, `logs/`, local `*.sqlite`
- Large audio / transcript dumps (they live in GCS)
