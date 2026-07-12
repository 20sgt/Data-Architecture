# Data-Architecture

A big obstacle for people to get involved with their local politics is the accessibility of information from local government meetings. To understand how policies are moving and what issues are being addressed or passed on, people need a way to access the information on the topics they are passionate about.

## Resources

Documentation/Slides links are not accessible publicly. Must be logged in with associated account.

- [GitHub Repository](https://github.com/20sgt/Data-Architecture)


## Architecture

- [ERD](https://dbdocs.io/jacksoncdawson/Group-Project-ERD?view=relationships)

# San Francisco Legislation Lakehouse

A data pipeline that scrapes legislative data from the City and County of San Francisco,
transforms it into an analytics-ready dimensional model, and serves it to a dashboard where
users can explore how their representatives vote.

Built as a data-architecture project using a **bronze → silver → gold** (medallion) lakehouse
pattern on Databricks.

---

## What it is

The San Francisco Board of Supervisors publishes every piece of legislation, every committee and
board meeting, and every recorded vote online — but only as rendered web pages, which are hard to
query or analyze. This project turns that public record into clean, queryable tables and a
dashboard.

The end product lets a user pick a representative, choose a time period, and see:

- which legislation they voted on,
- how they voted (Aye / No / Absent / Excused / Recused),
- details about that legislation, and
- what ultimately happened to it (passed, filed, killed, still in progress).

Under the hood it's an **ELT pipeline** with an immutable raw landing zone, organized into three
layers:

| Layer | What it holds | How it's built |
|-------|---------------|----------------|
| **Bronze** | Raw scraped JSON, one file per record, append-only | Web scraper → GCS bucket |
| **Silver** | Typed, flattened staging tables (1:1 with the JSON) | Databricks Auto Loader (incremental) |
| **Gold** | Star schema (dimensions, facts, bridges) + serving view | Latest-wins dedup + Delta `MERGE` |

```
sfgov.legistar.com
      │  scrape (Playwright + requests/bs4)
      ▼
GCS bucket: gs://cotc_raw/{matters,meetings}/ingest_date=YYYY-MM-DD/   [BRONZE]
      │  Auto Loader (cloudFiles) — ingests only new files
      ▼
silver staging tables (8)                                             [SILVER]
      │  latest-wins dedup → build star → MERGE upserts
      ▼
gold star schema + member_vote_record view                           [GOLD]
      │
      ▼
dashboard
```

---

## Data source

All data comes from the SF Board of Supervisors' legislative system, powered by **Legistar**:

- **Source:** https://sfgov.legistar.com

The data is collected by **scraping the rendered web pages**, not via an API. (Legistar exposes a
Web API, but it returned no data prior to a recent cutoff for this jurisdiction, so scraping the
HTML is the only complete source.) Because scraping is the system of record, the pipeline keeps an
immutable raw landing zone so transforms can be re-run without re-scraping.

Two entity "slices" are scraped independently and joined downstream:

- **Legislation** — matters (bills, resolutions, ordinances, hearings, motions), their actions,
  recorded votes, sponsors, and attached documents.
- **Meetings** — committee and board meeting calendars, agendas, and agenda items.

The two slices are linked by `history_id` (a stable identifier shared between a matter's action and
the corresponding meeting agenda item), which resolves each vote/action to the meeting it occurred
in.

---

## Repository structure

```
.
├── scrape/                     # Python scrapers (Playwright + requests/bs4)
│   ├── legistar_scrape.py      #   legislation slice
│   ├── legistar_meetings.py    #   meeting slice
│   ├── fetch.py                #   rate-limited HTTP + retry
│   └── history_detail.py       #   roll-call vote parser
├── transform/                  # Databricks notebooks (the lakehouse pipeline)
│   ├── silver_autoloader_databricks.py    # bronze JSON → silver staging (Auto Loader)
│   ├── gold_merge_databricks.py           # silver → gold star (dedup + MERGE)
│   └── gold_serving_view_databricks.py    # gold → member_vote_record serving view
├── docs/
│   ├── pipeline_design.md      # design rationale (ELT, incremental load, modeling decisions)
│   └── architecture_diagrams.md# data-flow + ER diagrams (Mermaid)
├── erd/
│   └── schema.dbml             # star-schema definition
├── frontend/
│   └── Design System.html      # dashboard design system / mockup
├── sample/                     # small sample of scraped JSON for local testing
│   ├── matters/ingest_date=.../
│   └── meetings/ingest_date=.../
├── requirements.txt
└── README.md
```

See the design docs for the full reasoning behind the architecture:

- [Pipeline design](docs/pipeline_design.md)
- [Architecture diagrams](docs/architecture_diagrams.md)
- [Star-schema definition](erd/schema.dbml)

---

## Data model (gold)

A dimensional **star schema**. Surrogate keys (`*_sk`) are deterministic hashes of natural keys, so
they're identical on every run and safe for incremental MERGE.

**Dimensions**
- `dim_matter` — one row per matter, as an **accumulating snapshot**: status, derived `lifecycle`
  and `final_disposition`, and milestone dates (introduced, first committee, final action,
  enactment). Updated in place as a bill progresses.
- `dim_person` — representatives (keyed on the stable Legistar `person_id`).
- `dim_committee` — every acting body (committees, the full Board, President, Mayor, Clerk).
- `dim_document` — matter attachments.
- `dim_meeting` — committee/board meetings.
- `dim_subject` — *stubbed; no data source yet.*

**Facts**
- `fact_matter_action` — one row per (matter, action).
- `fact_vote` — one row per (matter, action, person); the per-member roll call.

**Bridges**
- `bridge_matter_sponsor` — matter ↔ sponsor (primary vs. co).
- `bridge_matter_document` — matter ↔ document.

**Serving view**
- `member_vote_record` — denormalized, one row per vote, joining the member, the legislation's
  details, and its current outcome. This is what the dashboard queries.

`meeting_sk` on the fact tables is **nullable** — it's populated only when an action/vote resolves
to a known meeting via `history_id` (procedural actions like clerk referrals never occur in a
meeting).

---

## How to run

### Prerequisites

- A **GCP project** with a Cloud Storage bucket for the raw landing zone (this project uses
  `gs://cotc_raw`).
- A **Databricks workspace** with **Unity Catalog** enabled and read access to the bucket.
  > Reading external GCS requires a non-serverless (**classic**) Databricks workspace, since
  > serverless compute blocks external bucket egress. The classic cluster's service account must
  > have **Storage Object Viewer** on the bucket.
- Python 3.10+ for the scraper (`pip install -r requirements.txt`).

### 1. Scrape (bronze)

Runs the scrapers, which write one JSON file per record into the bucket, partitioned by scrape
date:

```
gs://cotc_raw/matters/ingest_date=YYYY-MM-DD/<file_number>.json
gs://cotc_raw/meetings/ingest_date=YYYY-MM-DD/<meeting_id>.json
```

The weekly scrape ingests matters **created** in the trailing 7-day window **plus** re-scrapes
everything on that week's agendas, so bills that move in a meeting are refreshed. (A matter whose
status changes *off-agenda* is refreshed only when it next appears on one — the open-set re-scrape
in `TODO.md` closes that gap.)

Per-file layout and what each existing partition covers: [scrape/README.md](scrape/README.md).

### 2. Silver — incremental ingestion

In Databricks, run **`transform/silver_autoloader_databricks.py`**. Set in the config cell:

```python
CATALOG = "corn_off_the_cob"   # your Unity Catalog catalog
SRC     = "gs://cotc_raw"       # the raw bucket
CKPT    = "/Volumes/<catalog>/silver/raw/_checkpoints"   # Auto Loader checkpoint (managed Volume)
```

Auto Loader reads **only files it hasn't processed before** (tracked in the checkpoint), so each run
ingests just the new partition. First run ingests the full backfill; subsequent runs are
incremental.

### 3. Gold — star schema

Run **`transform/gold_merge_databricks.py`** (set the same `CATALOG`). This:

1. collapses silver to each matter's/meeting's **latest scrape** (latest-wins dedup),
2. builds the dimensions, facts, and bridges,
3. **`MERGE`s** them into the gold tables — updating changed matters in place, inserting new ones,
   and skipping unchanged rows (idempotent).

### 4. Serving view

Run **`transform/gold_serving_view_databricks.py`** to create the `member_vote_record` view the
dashboard consumes.

> **Run order matters:** silver → gold → view. The gold notebook is the single source of truth for
> the gold layer; do not also run earlier overwrite-style gold notebooks.

---

## Known limitations / roadmap

- **8 unmapped statuses.** A full year of data surfaced 8 matters whose `status` isn't yet in the
  disposition map (they currently land as `final_disposition = 'UNMAPPED'`). This is a designed
  tripwire — they need to be added to the `TERMINAL`/`IN_PROGRESS` maps in the gold notebook.
  (Expect more from the 2000→2026 backfill.)
- **Databricks orchestration not yet automated.** The scrape side is scheduled (Cloud Scheduler →
  Cloud Run Job, weekly); the notebooks still run manually — a scheduled weekly Databricks
  Workflow (silver → gold → view) is the next step.
- **Data-quality checks pending.** Integrity assertions (unmapped statuses, orphan keys, name
  collisions) exist inline but should be lifted into a Great Expectations suite that fails the run
  loudly.
- **UC external location not registered.** GCS reads currently go through the cluster's compute
  service account rather than a governed Unity Catalog external location. Registering one is a
  production-hardening step.
- **`dim_subject` has no source.** No subject/tag data is emitted by the source pages; the table is
  stubbed for future keyword/LLM tagging.
- **Dashboard.** A design-system mockup exists (`frontend/`); the dashboard itself is not yet built
  on top of `member_vote_record`.

---

## Links

- **Repository:** https://github.com/20sgt/Data-Architecture
- **Data source (SF Legistar):** https://sfgov.legistar.com
- **Pipeline design:** [docs/pipeline_design.md](docs/pipeline_design.md)
- **Architecture diagrams:** [docs/architecture_diagrams.md](docs/architecture_diagrams.md)
- **ERD:** https://dbdocs.io/jacksoncdawson/Group-Project-ERD?view=relationships
- **Slides:** https://docs.google.com/presentation/d/1v0ImK7iBYsuHg1ciyDIfsG_P-4kD4YRl8uSPw1vbQmQ/edit?usp=sharing

