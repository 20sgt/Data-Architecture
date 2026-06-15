# Legislation Slice — Pipeline Design (ELT: raw landing zone → star schema)

**Owner:** Lynn · **Status:** draft for group review · **Date:** 2026-06-11
**Scope:** the legislation half of the architecture (matters, actions, votes, sponsors,
documents). The meeting half (dim_meeting, calendar scrape) is owned separately; the
cross-slice contract is defined in §5.

---

## 1. Why ELT and not a hand-built OLTP database

The classic "OLTP → OLAP" pipeline assumes we own a transactional database. We don't —
**Legistar's internal database is the OLTP system**, and we can only scrape its rendered HTML.
Our scraper is a batch extractor, not an application doing transactions. So instead of building
an artificial normalized OLTP database that no application ever writes to, we use **ELT with an
immutable raw landing zone**:

- **Replayability.** If the schema changes (it will — see §6), we re-run transforms from raw
  JSON instead of re-scraping. The 2020–2026 backfill is ~365 weekly search slices at a polite
  1 req/sec — many hours of scraping we never want to repeat.
- **Less code, fewer schemas.** One target schema (the star), not two plus a sync job.
- **It's the industry pattern** for scrape/API sources, and it maps 1:1 onto the
  bronze/silver/gold medallion layout used in the Lakehouse course next term.

## 2. Architecture

```
weekly Airflow DAG (orchestration — later increment)
        │
        ▼
legistar_scrape.py ──────────► raw/matters/ingest_date=YYYY-MM-DD/<file_number>.json
(Playwright search +           [BRONZE — append-only, immutable, date-partitioned]
 requests/bs4 detail scrape)
        │
        ▼
staging tables (stg_matters, stg_actions, stg_votes, stg_attachments, stg_sponsors)
        [SILVER — 1:1 with the JSON, typed, with lineage columns]
        │
        ▼  SQL transforms: dedupe (latest scrape wins), conform names → keys,
        │  surrogate-key lookups, derive lifecycle bucket
        ▼
star schema (group ERD + proposed additions, §6)
        [GOLD — what the dashboard queries]
        │
        ▼
Streamlit dashboard: votes by member · keyword search · weekly-change summary
```

**Warehouse:** transforms developed locally against **DuckDB** (free, instant feedback,
portable SQL), deployed to **Databricks Free Edition** (Delta tables, $0, no trial expiry) for
the cloud demo. ⚠️ Group decision pending — both slices must land in the same warehouse, and
Free Edition quotas should be verified before we hard-commit. Fallback: BigQuery sandbox.

## 3. Incremental load strategy (the non-obvious part)

A weekly scrape filtered by **File-Created date misses status changes on existing matters** —
a bill introduced in March passes in June, and a created-date window never sees it again.
The weekly job therefore scrapes the union of:

1. **New matters:** File-Created within the last window (Advanced search date slice).
2. **Open matters:** re-scrape every matter whose `lifecycle = 'in_works'` (not in a terminal
   status). SF runs ~30–100 files/week; the open set stays in the low hundreds — fine at 1 req/sec.

The raw layer stays **append-only**: each scrape writes a new dated partition, never mutates an
old one. The weekly-change use case is served by comparing the latest `is_current = true` rows in
`dim_matter` this week against the previous week's snapshot (see §6.1 for the SCD type 2 decision).

## 4. Natural keys and grain

| Entity | Natural key | Source |
|---|---|---|
| matter | `matter_id` | `ID=` param of LegislationDetail URL (e.g. 7994804); `matter_file` (260439) kept as the human-facing key |
| document | `document_id` | `ID=` param of View.ashx URL |
| person | full name → conformed `person_id` | seed map from Legistar `/persons` API dump (`spike/data/persons.json`); ⚠️ name-collision risk — flag if two supervisors ever share a name |
| vote | one row per **(matter, action, person)** | uniqueness on `(matter_id, action_date, body, person)` |

Why surrogate keys (`*_sk`) on top of natural keys: facts join on small integers (cheap), and
dims can be reloaded/corrected without rewriting fact rows; the natural key stays for idempotent
upserts and debugging.

## 5. Cross-slice contract (legislation ↔ meeting)

- **`fact_vote.meeting_sk` / `fact_matter_action.meeting_sk` are NULLABLE.** The scraper emits
  `(body name, action date)`; `meeting_id` comes from the calendar scrape. See the join strategy
  below.
- **Join key: integer `committee_sk`, not a committee name string.** `spike/data/bodies.json`
  (Legistar API) gives authoritative `BodyId → BodyName` pairs. `dim_committee` is seeded from
  this file, so `committee_id` is a stable integer. The staging→star transform maps
  `stg_actions.body` (text) to `dim_committee.committee_sk` **once**, failing loudly on unknown
  names. The contract with the meeting slice then becomes:
  `(committee_sk INT, meeting_date DATE) → meeting_sk INT` — an integer join, not fragile
  text-matching.
- **Neither slice's load may depend on the other's having run.** `meeting_sk` stays NULL until the
  meeting data is present; a subsequent transform pass fills it in.
- **Shared dims — proposed ownership:** `dim_committee` → legislation slice (seeded from
  `bodies.json`); `dim_person` → legislation slice (seeded from `spike/data/persons.json` +
  vote name conformance). Teammate's meeting scraper looks up `committee_sk` by `committee_id`
  from the pre-seeded dim.

## 6. Proposed changes to the group ERD (please review)

1. **`dim_matter`: add status fields using SCD type 2** — `status text`, `lifecycle text`
   (derived bucket: passed / in_works / other), `final_action_date date`, `enactment_date date`,
   `enactment_number text`, plus the versioning columns already present on `dim_person`:
   `effective_from date NN`, `effective_to date`, `is_current boolean NN`.
   When a matter's status changes (e.g. `in_works` → `passed`), the old row is closed out
   (`effective_to`, `is_current = false`) and a new row is inserted (new `matter_sk`). This
   preserves every status transition — you can answer "when did this bill pass?" directly from the
   dim, and the weekly-diff use case is a join of two `WHERE is_current = true` snapshots.
   Queries for current state filter `WHERE is_current = true`. Consistent with how `dim_person`
   is already modeled in the group ERD.
2. **`fact_matter_action`: add `action_result text`** (Pass/Fail). Mapping: `action_type_code` ←
   action (e.g. RECOMMENDED), `action_text` ← free text, `action_result` ← result column.
3. **`meeting_sk` nullable** on both fact tables (see §5).
4. **`dim_subject` has no data source yet.** Nothing on the Legistar pages emits subject tags.
   Proposal: keep the tables in the DDL, populate later via keyword extraction from
   `matter_name`/`matter_title` (or LLM tagging). Marked open.
5. **Minor:** `requester` not scraped yet (one label-id addition); `sponsor_type` — Legistar
   doesn't distinguish primary vs co-sponsor, so convention: first-listed = `primary`, rest = `co`.

## 7. Data quality (Great Expectations bonus hook)

Staging is the natural validation point — fail loudly *before* facts are built:
`vote_value ∈ {Aye, No, Absent, Excused, Recused}` · `matter_id` unique per partition ·
`introduced` parses as a date · row count > 0 per weekly run.

## 8. Cost model (draft)

| Component | Sizing assumption | Cost |
|---|---|---|
| Raw storage | 6 years ≈ tens of thousands of JSON files, sub-GB total | ~$0 (local / free tier object storage) |
| Warehouse | DuckDB local; Databricks Free Edition (or BigQuery sandbox ≤10 GB) | $0 |
| Orchestration | Airflow in local Docker | $0 |
| LLM summaries | Ollama local, 1 call/matter, **cached by content hash** — re-summarize only on text change | $0, the real bottleneck is wall-clock |

Honest scaling note for the rubric: real volume is sub-GB, so we *defend* scale-readiness
(date-partitioned raw, incremental loads, columnar warehouse) rather than demonstrate TBs.

## 9. Increments (small, reviewable)

1. **This doc + DDL** (staging + star) + DuckDB smoke test against the 5 spike samples ← *now*
2. Loader: raw JSON → staging (idempotent per partition)
3. Transforms: staging → dims/facts (latest-wins dedupe, SK lookups)
4. Airflow DAG: scrape → load → transform → validate, weekly schedule
5. 2020–2026 backfill run (polite, resumable)
6. Databricks deploy · Ollama summaries · Streamlit
