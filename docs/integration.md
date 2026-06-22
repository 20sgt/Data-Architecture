# Integration — Unified Local Pipeline

**Branch:** `integration` (merges `scrape-by-meeting` + `feature/legislation-schema-design`)
**Status:** full local pipeline, both slices → one gold star, runs end-to-end on DuckDB.

This branch reconciles the two slices into a single warehouse and builds the joint facts the
cross-slice contract describes. It supersedes the per-slice gold transforms.

## Layers

```
scrape/legistar_meetings.py ─▶ raw/meetings/...   ─┐
scrape/legistar_scrape.py    ─▶ raw/matters/...   ─┤  BRONZE (per slice, immutable)
                                                   │
load_meeting_staging.py ─▶ stg_meeting_*          ─┤  SILVER (per slice, 1:1 with JSON)
load_staging.py         ─▶ stg_matters/actions/…  ─┘
                                                   │
transform_gold.py ─▶ ONE shared star (02_star.sql) ─  GOLD (both slices, milestone-3 ERD)
```

- **Silver stays per-slice** (`01_staging.sql` legislation, `03_meeting_staging.sql` meeting).
- **Gold is one shared schema**: [`warehouse/ddl/02_star.sql`](../warehouse/ddl/02_star.sql), matching
  [`erd/schema.dbml`](../erd/schema.dbml). The old `04_meeting_star.sql` is folded in.
- **One builder**: [`warehouse/transform_gold.py`](../warehouse/transform_gold.py) replaces the
  per-slice `transform_star.py` / `transform_meeting_star.py`.

## What the unified build does

1. **Self-contained seeding** (no `bodies.json` / `persons.json` dependency — those were never
   committed): `dim_committee` from every scraped body name; `dim_person` one row per name, using the
   meeting slice's **real `PersonId`** where available (more robust than name-only).
2. **`dim_matter` flat** (per the ERD — status is derived from `fact_matter_action`, not stored).
   Matter files a meeting references but legislation hasn't scraped get an **`html_stub`** row so
   facts never orphan.
3. **Full rebuild, child-first delete then insert** — idempotent, and it satisfies DuckDB's FK
   enforcement (which forbids deleting an FK-referenced parent). Re-running is the recovery path.
4. **Joint fact merge** (resolves the contract that was deferred on the meeting branch):
   - One shared label→code / vote map ([`scrape/action_types.py`](../scrape/action_types.py)) is used
     for BOTH slices, so `action_type_code` and `vote_value` are consistent in the dedup keys.
   - **Meeting is system-of-record**: meeting-sourced facts are written first (clean `EventId` →
     `meeting_sk`). Legislation rows are added only where the meeting scrape didn't already cover the
     `(matter, meeting, action/person)` tuple, with `meeting_sk` resolved best-effort via
     `(committee, action_date)` → `dim_meeting` (NULL when no meeting matches).
   - Each fact row carries `source` (`meeting` | `legislation`) for lineage.

This is what the original review flagged as the silent-duplicate risk; the unified transform is where
it's actually prevented (proven by [`warehouse/smoke_test_gold.py`](../warehouse/smoke_test_gold.py)).

## Run it

```bash
pip install -r requirements.txt            # + `playwright install chromium` for deep enumeration

# offline checks (no network)
python warehouse/smoke_test_meetings.py    # meeting parsers + meeting-only gold
python warehouse/smoke_test_gold.py        # cross-slice fact merge / dedup

# real local run (after scraping both slices to raw/)
python warehouse/run_local.py \
    --meeting-raw raw/meetings/ingest_date=2026-06-21 \
    --matters-raw raw/matters/ingest_date=2026-06-21 \
    --date 2026-06-21
```

## Resolved here (were the merge prerequisites)
- Legislation `action_type_code` / `vote_value` now normalized through the shared module (via the
  unified transform) — dedup keys align across slices.
- Fact grain uses the ERD natural key with `meeting_id` resolved by the meeting slice.
- `dim_document` / `dim_committee` shape collision gone — one shared `02_star.sql` at milestone-3.

## Still open (further work)
- **Orchestration**: `dags/legislation_weekly.py` now calls `transform_gold`, but the meeting scrape
  isn't an upstream DAG task yet — add it so the weekly run feeds both stagings.
- **Live legislation run** needs `playwright install chromium`; the meeting slice runs via
  `--current-month` without a browser. Deep history (`--year`) needs the windowing pass.
- **Real `BodyId`s**: `dim_committee.committee_id` is NULL (seeded by name). Layer in a `bodies.json`
  seed if/when available; everything joins on `committee_name` meanwhile.
- **Vote casing** (`No`/`Absent`/`Recused`/`Present`) and **`dim_subject`** (no source) remain open.
