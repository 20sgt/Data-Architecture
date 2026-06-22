# Meeting Slice — Pipeline Design (scrape-by-meeting)

**Owner:** Jack · **Status:** increment 1 (silver + uncontested gold) · **Date:** 2026-06-21
**Scope:** the meeting half of the architecture (`dim_meeting`, meeting documents, and — at the
cross-slice merge — `fact_matter_action` / `fact_vote`). The legislation half is owned separately
([`pipeline_design.md`](pipeline_design.md)); the shared contract is in §5.

Companion to the gold schema in [`../erd/schema.dbml`](../erd/schema.dbml) /
[`../erd/ERD.md`](../erd/ERD.md) and the open decisions in [`../DISCUSSION.md`](../DISCUSSION.md).

> **On the `integration` branch this is superseded for the gold layer.** The deferred joint fact
> merge and the "merge prerequisites" in §5 are now BUILT in the unified
> [`../warehouse/transform_gold.py`](../warehouse/transform_gold.py) against the shared
> [`../warehouse/ddl/02_star.sql`](../warehouse/ddl/02_star.sql). See
> [`integration.md`](integration.md). This doc still describes the meeting slice's bronze→silver and
> the contract rationale.

---

## 1. Why scrape by meeting at all

The legislation slice already reaches every roll-call from the *matter* side, so why crawl meetings?
Because the meeting is where an action physically happens, and the calendar is the **only** place
that cleanly exposes `EventId` + `EventGUID` (and the Granicus video clip id). The legislation path
can only say *"some body acted on some date"* — it cannot fill `fact_*.meeting_sk`. The meeting slice
is therefore the system-of-record for the meeting dimension and the natural source of `meeting_sk`.

## 2. Crawl path (verified live 2026-06-21)

```
Calendar.aspx                      -> one row per meeting: EventId, EventGUID, body name,
  (Telerik RadGrid; ~100-row cap;     date/time, location(+subtype), agenda/minutes View.ashx
   period/body = postback)            URLs, Granicus clip id (Video/Audio/Transcript columns)
        │
        ▼
MeetingDetail.aspx?ID=&GUID=       -> header (status/location/urls) + gridMain agenda items
        │                             (c0 File# = matter_file, c7 Action, c8 Result,
        │                              c9 = radopen('HistoryDetail...') ONCLICK)
        ▼
HistoryDetail.aspx?ID=&GUID=       -> lblActionText (full motion) + gridVote per-person roll-call
                                      (Person -> PersonDetail.aspx?ID=<PersonId>, Vote literal)
```

**Minutes-status gating (important).** `lblMinutesStatus` ∈ {`Final`, `Final Draft`, `Draft`}.
A meeting still in **`Draft`** has *no* populated actions/votes (gridMain Action/Result blank, no
HistoryDetail links). Draft meetings are still **emitted to bronze/gold** (the meeting row, with
`minutes_status=Draft` and no actions) so the incremental job can find and **re-scrape** them from
gold once minutes advance (§4). `--skip-draft` opts out of emitting them.

**Enumeration.** The default `Calendar.aspx` "This Month" view is GET-able and already includes the
current month's completed meetings — enough for the pilot with no browser (`--current-month`). Deeper
history (past months/years) needs the Telerik combo postback, so `--year` drives Playwright
(`playwright install chromium`); the combo-selection step is written but wants a live tuning pass.

## 3. Layering (ELT, medallion) and what increment 1 builds

```
legistar_meetings.py ──► raw/meetings/ingest_date=YYYY-MM-DD/<EventId>.json   [BRONZE, immutable]
        │
        ▼  load_meeting_staging.py (idempotent per ingest_date: delete-then-insert)
stg_meetings · stg_meeting_agenda_items · stg_meeting_votes · stg_meeting_documents   [SILVER]
        │
        ▼  transform_gold.py (unified full refresh; see docs/integration.md)
dim_meeting · dim_action_type · dim_document(meeting) · bridge_meeting_document       [GOLD ✓ built]
fact_matter_action · fact_vote                                                        [GOLD ✓ built (joint merge)]
```

This increment ("**silver + my uncontested gold**") builds everything except the two shared fact
tables, which are deliberately held for the joint merge (§5). The transform does a **full refresh**
of the meeting-owned subgraph (child-first delete, then insert) rather than in-place upsert — this is
both simpler and a hard requirement on DuckDB, which cannot UPDATE a `dim_meeting` row while
`bridge_meeting_document` references it. Re-scrapes are correct for free: the latest `ingest_date`
per `meeting_id` wins.

## 4. Incremental strategy

A weekly run scrapes the union of:
1. **New / current meetings** — the current calendar window.
2. **Draft re-scrapes** — any meeting already in gold whose `minutes_status` is still `Draft` (or
   `Final Draft`), so late-posted actions/votes get captured.

The raw layer stays append-only (one dated partition per run). `dim_meeting` is a **flat** dimension,
not SCD2 — a meeting is a fixed event; re-scrapes replace its row's mutable fields (status, urls, clip).

## 5. Cross-slice contract (meeting ↔ legislation)  ← the important part

Both slices surface the **same** `HistoryDetail` roll-call, so the canonical facts are assembled in a
**joint reconciliation step** (working mode "B", converging to the ERD's "meeting is primary" target):

- **Shared label→code map: [`scrape/action_types.py`](../scrape/action_types.py).** BOTH scrapers MUST
  normalize raw action labels through this one module — `action_type_code` is in the
  `fact_matter_action` dedup key, so divergent mappings silently duplicate rows. (Verified trap: the
  site emits both `PASSED, ON FIRST READING` and `PASSED ON FIRST READING` — same action.)
- **Fact natural keys** (both slices upsert on these):
  `fact_matter_action` → `(matter_id, meeting_id, action_type_code)`;
  `fact_vote` → `(matter_id, meeting_id, person_id)`.
- **The meeting slice supplies, per agenda item:** `matter_file` (→ `dim_matter.matter_id`),
  `meeting_id` (→ `meeting_sk` — only this slice has a clean EventId), `action_raw` (→ code),
  `action_result`, `action_text`, and votes (`person_id`, `vote_value`). `body_scope` = `board`
  when `body_name = 'Board of Supervisors'` else `committee`. `vote_value`: `No` → `Nay` at merge.
- **Neither slice's load depends on the other.** The legislation slice writes the same fact rows with
  `meeting_sk` NULL; the merge fills `meeting_sk` from the meeting staging and dedups on the keys.
- **Shared dims:** `dim_committee`/`dim_person` are owned by the legislation slice (seeded from
  `bodies.json`/`persons.json`). On a standalone meeting branch the transform **provisionally** seeds
  `dim_committee` from calendar body names (`committee_id` NULL), reconciled on name at merge. The
  meeting slice uniquely captures `PersonId` (from `PersonDetail`) — a more robust person key than the
  name-matching the legislation slice currently uses; recommend the merge key on `person_id`.

### Merge prerequisites — the legislation slice must adopt these before the joint merge
> **✅ Resolved on the `integration` branch.** The unified `warehouse/transform_gold.py` supersedes the
> legislation slice's `transform_star.py` entirely and handles all three below: it normalizes both
> slices through `scrape/action_types.py`, dedups cross-slice on the shared **`history_id`** (so the
> ERD-natural-key concern is moot), and builds against the single milestone-3 `02_star.sql`. The list
> is kept as a record of the original divergences. See [`integration.md`](integration.md).

An adversarial review (2026-06-21) confirmed the sibling branch `feature/legislation-schema-design`
**violated** the contract above; on its own these would make the merge silently duplicate / corrupt facts:
1. **Normalize via the shared module.** `transform_star.py` writes the *raw* label into
   `action_type_code` (e.g. `PASSED, ON FIRST READING`) and never calls `action_types.py`. It must call
   `normalize_action(raw).code` (and `normalize_vote(raw)` for `No`→`Nay`) — otherwise the meeting row
   (`PASSED_BOARD_1ST_READING`) and the legislation row (`PASSED, ON FIRST READING`) never dedup.
2. **Use the ERD natural key.** Its dedup key is `(matter_sk, action_type_code, action_date)` — it omits
   `meeting_id` and substitutes `action_date`, so it disagrees on grain with the ERD key
   `(matter_id, meeting_id, action_type_code)`. The merge step should own dedup on the ERD key.
3. **Adopt the milestone-3 shared-dim shapes.** `02_star.sql` still has milestone-2 `dim_document`
   (`document_id INTEGER NOT NULL`, no `document_source`) and `dim_committee`
   (`committee_id INTEGER NOT NULL`, `UNIQUE(committee_id)`). Because both slices share one DuckDB file
   via `CREATE TABLE IF NOT EXISTS`, whichever DDL runs first wins and breaks the other (the meeting
   transform's `document_source` insert / `committee_id NULL` provisional seed fail against the
   milestone-2 shapes). Settle one shared DDL at the milestone-3 shapes in `erd/schema.dbml`.

## 6. Known gaps / next increments

- **Playwright enumeration** (`--year`) needs a live tuning pass against the RadComboBox postback;
  the pilot runs on `--current-month` today.
- **Vote literals**: roll-call rows are detected structurally (PersonDetail link), so the literal is
  captured verbatim and unrecognized values are logged — never silently dropped; `normalize_vote`
  maps `No`→`Nay` at the merge. `Aye`/`Excused` confirmed live; confirm `No`/`Absent`/`Recused`/`Present`
  casing against a contested/absentee meeting before locking the `vote_value` CHECK.
- **Transcript `body_text`**: recorded as a Granicus captions VTT URL; extraction deferred (`--with-text`
  currently covers agenda/minutes PDFs only).
- **dim_matter orphans**: meeting facts join on `matter_file`; until the legislation slice builds
  `dim_matter`, recent files may orphan — see DISCUSSION Q5 (html-stub strategy).
- **Airflow DAG** (`scrape → load → transform`, weekly, with Draft re-scrape) — a later increment,
  mirroring `dags/legislation_weekly.py`.

## 7. How to run

```bash
pip install -r requirements.txt        # + `playwright install chromium` for --year

# scrape one known meeting (no browser)
python -m scrape.legistar_meetings --event 1422963 --guid 0C4442D2-D43D-4908-B173-C02789C9BAA1

# scrape the current month's completed meetings -> bronze
python -m scrape.legistar_meetings --current-month --raw-dir raw/meetings --date 2026-06-21

# bronze -> silver -> gold (unified builder; see docs/integration.md)
python warehouse/load_meeting_staging.py --src raw/meetings/ingest_date=2026-06-21 --date 2026-06-21
python warehouse/transform_gold.py

# offline end-to-end check (no network)
python warehouse/smoke_test_meetings.py
```
