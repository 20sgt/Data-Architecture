# San Francisco Legislation Lakehouse

A big obstacle to getting involved in local politics is that the information is hard to reach.
To understand how policies are moving and what is being addressed or passed on, people need a
way to get at the topics they care about.

This is a data pipeline that scrapes legislative data from the City and County of San Francisco,
transforms it into an analytics-ready dimensional model, and serves it — as a charted voting
record and as a natural-language question interface over the gold layer.

Built as a data-architecture project using a **bronze → silver → gold** (medallion) lakehouse
pattern on Databricks.

- [GitHub repository](https://github.com/20sgt/Data-Architecture)
- [ERD](https://dbdocs.io/jacksoncdawson/Group-Project-ERD?view=relationships)

> Course documentation and slides are not public — you need to be logged in with the
> associated account.

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

Ordered by where data flows, not alphabetically.

| Path | What lives there |
|------|------------------|
| `scrape/` | The scrapers. `legistar_scrape.py` (legislation), `legistar_meetings.py` (meetings), `fetch.py` (rate-limited HTTP), `history_detail.py` (roll-call parser), `tests/` (offline golden tests, run in CI) |
| `Dockerfile`, `entrypoint.sh` | The scraper as a container. `entrypoint.sh` is the weekly order of operations: meetings, then matters |
| `terraform/` | GCP infrastructure — the bronze bucket, plus the weekly Cloud Run Job and its Scheduler trigger |
| `scripts/backfill.sh` | One-shot 2000→2026 deep-history scrape (resumable) |
| `databricks/` | The one notebook still in the pipeline: GCS JSON → bronze Delta via Auto Loader |
| `dbt/` | Everything after bronze. `models/staging/` flattens, `models/intermediate/` dedups latest-wins, `models/gold/` builds the star; `tests/` and `macros/` alongside |
| `databricks.yml` | Asset Bundle — the weekly transform Job, as code |
| `app/` | The serving layer. `ask.py` (question → SQL → answer), `dashboard.py` (the charted voting record), `streamlit_app.py` (both, as two tabs), `build_index.py` (podcast transcript FTS index) |
| `sfchronicle_podcast_ingest/` | Separate slice: podcast audio → Whisper transcripts → enrichment. Feeds `app/build_index.py` |
| `erd/schema.dbml` | Star-schema definition |
| `sample/` | Four bronze JSON files documenting the shape silver consumes |
| `docs/` | Design rationale and Mermaid diagrams |

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
  details, and its current outcome. This is what `app/` queries, and where you should start too.

`meeting_sk` on the fact tables is **nullable** — it's populated only when an action/vote resolves
to a known meeting via `history_id` (procedural actions like clerk referrals never occur in a
meeting). This is not a rare edge case: **58%** of `fact_matter_action` rows (103,669 of 179,247)
and **3.7%** of `fact_vote` rows (21,828 of 587,165) have no meeting. Join with `LEFT JOIN` —
filtering on `meeting_sk` silently drops most of the action history.

---

## Using the warehouse

The gold tables live in a Databricks SQL warehouse, reachable over JDBC/ODBC from **any** client —
you do not have to work inside the Databricks web UI. Notebooks, VS Code, DataGrip, DBeaver, a
local Python script and dbt all connect the same way.

### Connection details

| | |
|---|---|
| Host | `8259559357598229.9.gcp.databricks.com` |
| HTTP path | `/sql/1.0/warehouses/aa8398aa70d15ec5` |
| Catalog | `corn_off_the_cob` |
| Auth | your own credentials — see below |

For authentication, prefer **OAuth** over a personal access token:

```bash
databricks auth login --host https://8259559357598229.9.gcp.databricks.com --profile DEFAULT
```

This opens a browser, stores refreshing credentials in your OS keyring, and never expires on you.
Personal access tokens still work and are what the dbt profile uses, but Databricks now labels them
legacy — and a silently expired one cost this project a week in July.

### Reading from Python

```bash
pip install databricks-sql-connector
```

```python
from databricks import sql

with sql.connect(
    server_hostname="8259559357598229.9.gcp.databricks.com",
    http_path="/sql/1.0/warehouses/aa8398aa70d15ec5",
    access_token="dapi...",           # or use OAuth via auth_type="databricks-oauth"
) as conn, conn.cursor() as cur:
    cur.execute("SELECT * FROM corn_off_the_cob.gold.member_vote_record LIMIT 10")
    for row in cur.fetchall():
        print(row)
```

`member_vote_record` is the place to start — one row per vote with the member, legislation, outcome,
body and meeting already joined on, so you need no joins of your own.

### What you can and cannot touch

| schema | access |
|---|---|
| `gold` | **read only** — `SELECT` on all 10 relations |
| `silver`, `bronze` | no access (internal; shapes change without notice) |
| `dev_<yourname>` | **yours** — you own it, build whatever you like |

Every gold table and column carries a description in Unity Catalog, so **Catalog Explorer** is a
real reference: open `corn_off_the_cob` → `gold` and read the column comments rather than guessing
what `lifecycle` or `final_disposition` mean.

### Building your own models on top

Read from `gold`, write into your own schema. Ask an admin for one:

```sql
CREATE SCHEMA corn_off_the_cob.dev_yourname;
ALTER SCHEMA corn_off_the_cob.dev_yourname OWNER TO `you@example.com`;
```

If you are working in **this** dbt project, set `schema: dev_yourname` in your `profiles.yml`
(see `dbt/profiles.yml.example`). Every model then builds into your schema:

```
dbt run          ->  dev_yourname.dim_matter     (your sandbox)
```

Production is opt-in and deliberately awkward to trigger by accident:

```
dbt run --vars '{prod_schemas: true}'   ->  gold.dim_matter   (the live tables)
```

Prefer letting the scheduled Databricks Job build production. Sources are never redirected, so your
sandbox reads the **real** `bronze` data — you develop against production data without being able to
damage it.

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

### 2. Bronze — incremental ingestion

`databricks/bronze_autoloader_databricks.py` lands raw JSON into `bronze.matters` /
`bronze.meetings`, nesting intact. Auto Loader reads **only files it hasn't processed before**
(tracked in a checkpoint Volume), so each run ingests just the new partition.

Must run on a **classic** cluster with Unity Catalog enabled — serverless blocks external-GCS
egress, and without UC the notebook's three-part table names fail. Both are already set in
`databricks.yml`.

### 3. Silver + gold — dbt

Everything after bronze is dbt. There are no gold notebooks any more.

```bash
cd dbt
dbt build                                # builds into YOUR sandbox schema
dbt build --vars '{prod_schemas: true}'  # builds the real silver / gold
```

`dbt build` runs the models *and* the 49 data tests. See
[dbt/profiles.yml.example](dbt/profiles.yml.example) for connection setup and why the plain command
is the safe one.

### 4. Or just run the whole thing

Both steps above are wired together as a Databricks Job, defined in `databricks.yml`:

```bash
databricks bundle deploy -t dev
databricks bundle run weekly_transform -t dev
```

It runs `bronze_ingest` → `dbt_build`, on a Wednesday 08:00 PT schedule that is **paused** in the
dev target. Failures email the job owner.

### 5. The serving layer

```bash
cp .env.example .env      # fill in the warehouse + Anthropic credentials
set -a; source .env; set +a
streamlit run app/streamlit_app.py
```

Two tabs over the same gold tables:

- **Ask** — type a question in English, get an answer, the SQL that produced it, and the rows.
- **Voting record** — the fixed charts. Grouped bars per supervisor by vote value over a period
  you choose, and a click-through to every vote that member cast: the matter, its type, the
  committee, and how it ended up.

Nothing loads `.env` automatically — `app/ask.py` reads `os.environ`, so the `source` line is
required rather than decorative.

One-off from the shell, which prints the SQL it wrote and then the answer:

```bash
python app/ask.py "Which supervisor votes 'No' most often?"
```

The podcast transcript index is optional. Without it you get answers with no "related listening";
to build it (needs `gcloud` auth on the podcast bucket, ~200 MB local):

```bash
python app/build_index.py
```

Both modules self-check offline with no warehouse and no API key:

```bash
python app/ask.py --demo && python app/dashboard.py --demo && python app/build_index.py --demo
```

---

## Known limitations / roadmap

- **No alerting on the scrape.** The Databricks Job emails on failure; the Cloud Run Job does not.
  A run that fails to start logs at `ERROR` and tells nobody — which is exactly how the
  2026-07-29 miss (41 matters, 5 meetings) sat unnoticed for ten days. See `TODO.md`.
- **`action_type_code` is not a code.** Despite the name and the ERD's declared vocabulary, the
  column holds raw uppercased Legistar labels — one of them is 82 characters. The normalization
  layer (`dim_action_type`) is designed but unbuilt. Treat it as free text.
- **Meeting documents are staged and then dropped.** `stg_meeting_documents` exists and nothing
  reads it, so agendas, minutes and caption URLs never reach gold. `bridge_meeting_document` in
  the ERD is the missing piece (~14K rows).
- **UC external location not registered.** GCS reads currently go through the cluster's compute
  service account rather than a governed Unity Catalog external location. Registering one is a
  production-hardening step.
- **`dim_subject` has no source.** No subject/tag data is emitted by the source pages; the table is
  stubbed for future keyword/LLM tagging.
- **No district or party on the dashboard.** `dim_person` is identity-only — `person_id` and
  name, captured as a byproduct of roll-call votes. Neither attribute is published anywhere on
  the pages the scraper reads; filling them needs the Legistar web API people slice (`TODO.md`).
  The chart labels supervisors by name alone.

---

## Links

- **Repository:** https://github.com/20sgt/Data-Architecture
- **Data source (SF Legistar):** https://sfgov.legistar.com
- **Pipeline design:** [docs/pipeline_design.md](docs/pipeline_design.md)
- **Architecture diagrams:** [docs/architecture_diagrams.md](docs/architecture_diagrams.md)
- **ERD:** https://dbdocs.io/jacksoncdawson/Group-Project-ERD?view=relationships
- **Slides:** https://docs.google.com/presentation/d/1v0ImK7iBYsuHg1ciyDIfsG_P-4kD4YRl8uSPw1vbQmQ/edit?usp=sharing

