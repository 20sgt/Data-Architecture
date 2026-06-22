# `integration` branch — what's here, where it came from, and its state

This branch merges the two independently-developed scrape slices into **one local pipeline** that
runs end-to-end (raw scrape → staging → unified gold star) on DuckDB, ready to PR into `main` or hand
back for review. It is **local only — never pushed**, and the source feature branches are untouched.

> Deep-dive design notes live in [`docs/integration.md`](docs/integration.md). This file is the map:
> what each file is, who it came from, what was changed here, and the current state.

---

## 1. Lineage

```
main ──▶ scrape-by-meeting (Jack)  ─┐
                                    ├─▶ integration   (this branch)
feature/legislation-schema-design ─┘   (merge + reconcile)
  (Lynn, origin/…)
```

Commit history:

| Commit | What |
|---|---|
| `1c9a1c8` | meeting slice "first draft" (Jack) — base of this branch |
| `f16fce6` | `git merge` of `origin/feature/legislation-schema-design` (Lynn) |
| `817549d` | **unified gold pipeline** — shared DDL, `transform_gold.py`, joint fact merge |
| `29fb66b` | fixes for adversarial review pass 1 (15 findings) |
| `7999f77` | fixes for review pass 2 (9 findings) |
| `28005d9` | fixes for review pass 3 (9 findings) — `history_id` cross-slice dedup |

The merge had only trivial text conflicts (`.gitignore`, `requirements.txt`); the real work was the
*logical* reconciliation in `817549d` and the three review passes after it.

---

## 2. Where each file came from

### Meeting slice (Jack — from `scrape-by-meeting`), unchanged here
| File | Role |
|---|---|
| [`scrape/legistar_meetings.py`](scrape/legistar_meetings.py) | Meeting scraper: `Calendar → MeetingDetail → HistoryDetail` → bronze JSON |
| [`scrape/action_types.py`](scrape/action_types.py) | **Shared** raw-label→code + `No→Nay` normalization (both slices use it) |
| [`scrape/fixtures/`](scrape/fixtures) | Saved live HTML for offline tests |
| [`warehouse/ddl/03_meeting_staging.sql`](warehouse/ddl/03_meeting_staging.sql) | Meeting silver staging |
| [`warehouse/load_meeting_staging.py`](warehouse/load_meeting_staging.py) | Meeting bronze→staging loader |
| [`warehouse/smoke_test_meetings.py`](warehouse/smoke_test_meetings.py) | Meeting parser + meeting-only gold test |
| [`docs/meeting_pipeline_design.md`](docs/meeting_pipeline_design.md) | Meeting slice design |
| [`erd/`](erd) , [`DISCUSSION.md`](DISCUSSION.md) | Gold ERD (source of truth) + decisions |

### Legislation slice (Lynn — from `feature/legislation-schema-design`), unchanged here
| File | Role |
|---|---|
| [`scrape/legistar_scrape.py`](scrape/legistar_scrape.py) | Legislation scraper: search → `LegislationDetail` → bronze JSON |
| [`warehouse/ddl/01_staging.sql`](warehouse/ddl/01_staging.sql) | Legislation silver staging |
| [`warehouse/load_staging.py`](warehouse/load_staging.py) | Legislation bronze→staging loader |
| [`docs/pipeline_design.md`](docs/pipeline_design.md), [`docs/architecture_diagrams.md`](docs/architecture_diagrams.md) | Legislation slice design |

### New on `integration`
| File | Role |
|---|---|
| [`warehouse/transform_gold.py`](warehouse/transform_gold.py) | **The unified gold builder** — replaces both per-slice transforms; joint fact merge |
| [`warehouse/run_local.py`](warehouse/run_local.py) | One command: raw → staging → gold |
| [`warehouse/export_parquet.py`](warehouse/export_parquet.py) | Export gold → Parquet for the Databricks handoff |
| [`warehouse/smoke_test_gold.py`](warehouse/smoke_test_gold.py) | Cross-slice fact-merge / dedup test |
| [`docs/integration.md`](docs/integration.md) | Integration design + run instructions |
| `BRANCH-README.md` | This file |

### Lynn's files **edited** on `integration`
| File | Change |
|---|---|
| [`warehouse/ddl/02_star.sql`](warehouse/ddl/02_star.sql) | **Rewritten** to the single milestone-3 shared gold schema (flat `dim_matter`; `dim_document`/`dim_committee` shapes unified; `dim_action_type`; `body_scope`/`action_result`) |
| [`dags/legislation_weekly.py`](dags/legislation_weekly.py) | Transform task now calls `transform_gold`; "in_works" re-scrape set reads from `stg_matters` (flat `dim_matter` dropped `lifecycle`/`is_current`); task renamed `build_gold` |
| [`databricks/01_load_legislation.py`](databricks/01_load_legislation.py) | Flat-schema sanity query (status from facts), consistent `CATALOG`, full gold table list, `'Primary'` casing |

### Deleted (superseded)
| File | Why |
|---|---|
| `warehouse/transform_star.py` (Lynn) | Superseded by `transform_gold.py` |
| `warehouse/smoke_test.py` (Lynn) | Depended on uncommitted `spike/` data; replaced by `smoke_test_gold.py` |
| `warehouse/ddl/04_meeting_star.sql` (Jack) | Folded into the shared `02_star.sql` |

*(`.gitignore`, `README.md`, `requirements.txt` are merged supersets of both slices' versions.)*

---

## 3. The reconciliation logic (what `transform_gold.py` does and why)

The two slices were independently correct but collided on the shared gold layer. Resolved as:

1. **One shared gold DDL** (`02_star.sql`) at the milestone-3 ERD. Per the ERD, `dim_matter` is
   **flat** (status derived from `fact_matter_action`), superseding Lynn's SCD2 version.
2. **One builder** (`transform_gold.py`) reads *both* stagings and full-rebuilds gold child-first
   (idempotent, FK-safe on DuckDB).
3. **Self-contained dim seeding** — no `bodies.json`/`persons.json` (those were never committed and
   don't exist locally). `dim_committee` from scraped body names; `dim_person` one row per name using
   the meeting slice's **real `PersonId`** where available. Names matched through one Python
   canonicalizer (no SQL/Python parity gap).
4. **Joint fact merge — meeting is system-of-record.** Cross-slice dedup is keyed on the
   **`history_id`** (MatterHistory id) that *both* slices carry (meeting via the agenda-row `radopen`
   link; legislation via the `ID=` in `history_url`). The same physical action/roll-call shares that
   id regardless of how each slice labels/dates/attributes it, so this dedups duplicates **and**
   preserves genuinely distinct events (different history entries, different committees same day,
   voters one slice missed). `matter_file`-only meeting matters get an `html_stub` row so facts never
   orphan. Every fact carries a `source` (`meeting` | `legislation`) column for lineage.

The shared `scrape/action_types.py` is the single label→code / vote-normalization authority for both
slices.

---

## 4. State

**Verified (all green):**
- `python warehouse/smoke_test_meetings.py` — meeting parsers + meeting-only gold.
- `python warehouse/smoke_test_gold.py` — cross-slice fact merge / dedup (18 assertions).
- Real run on live meeting data: `scrape → run_local → export_parquet` (14 gold tables to Parquet).
- **Three adversarial review passes** run and all confirmed findings fixed (15 + 9 + 9), each with
  regression assertions added.

**Reviewed dev env:** a local `.venv` (gitignored) with `requirements.txt`; gold lands in
`warehouse/db/legislation.duckdb` (gitignored), Parquet in `warehouse/exports/` (gitignored).

**Still open (further work, not blockers):**
- Add the meeting scrape as an upstream DAG task (the DAG feeds only legislation staging today).
- Live legislation run needs `playwright install chromium`; deep meeting history (`--year`) needs the
  pagination/windowing pass.
- Real `BodyId`s (`committee_id` is NULL, seeded by name); vote-casing for `No`/`Absent`/`Recused`;
  `dim_subject` has no source. (All tracked in [`DISCUSSION.md`](DISCUSSION.md) / `docs/`.)

**For the merge to `main` / handing to Lynn:** her feature branch is untouched. The edits to her
files (`02_star.sql`, the DAG, the Databricks notebook) and the two deletions are isolated as commits
on this branch — reviewable as a diff (`git diff origin/feature/legislation-schema-design..integration`).

---

## 5. Run it

```bash
pip install -r requirements.txt              # + `playwright install chromium` for deep enumeration

# offline checks (no network)
python warehouse/smoke_test_meetings.py
python warehouse/smoke_test_gold.py

# real local run (after scraping; meeting slice needs no browser)
python -m scrape.legistar_meetings --current-month --raw-dir raw/meetings --date $(date +%F)
python warehouse/run_local.py --meeting-raw raw/meetings/ingest_date=$(date +%F) --date $(date +%F)
python warehouse/export_parquet.py           # gold → warehouse/exports/*.parquet for Databricks
```
