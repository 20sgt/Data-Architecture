# Changelog

Newest entries at the top.

## [2026-07-12 21:41] — Gold star schema in dbt, validated (increment 4)

**What:** Added an intermediate dedup layer (8 `int_*` views) + 9 gold models
(5 dims, 2 facts, 2 bridges), reproducing `gold_merge`. `dbt run` builds all 25
models; diffed all 9 gold tables against the notebook output (`gold_ref`, 869-
matter partition) — **all 9 identical**.
**Why:** dbt now owns silver→gold end to end. The xxhash64 key macro made a
byte-for-byte diff possible, proving the rewrite.
**Files:** `dbt/dbt_project.yml` (intermediate config), `dbt/models/intermediate/*.sql`
(8), `dbt/models/gold/*.sql` (9)
**Notes:** Latest-wins dedup lives in the `int_*` layer; child tables filtered via
LEFT SEMI JOIN. Stable fact keys + `qualify row_number()` reproduce the notebook's
dropDuplicates. Caught an ambiguous `matter_file` ref in `dim_matter` (must
qualify columns after a join). All phase-1 `table`; converting the hot tables to
incremental `merge` is increment 7. Dedup still only trivially exercised (single
scrape date) until the bootstrap.

## [2026-07-12 21:26] — Silver validated against reference (increment 3 complete)

**What:** Loaded the 2026-06-26 partition (869 matters / 112 meetings) into
`bronze`, ran `dbt run --select staging`, and diffed all 8 `silver.stg_*` against
the old notebook output (copied to `silver_ref`). **All 8 identical** (row counts
equal, zero rows on either side of `exceptAll`, lineage cols excluded).
**Why:** Proves the dbt flatten reproduces the PySpark flatten exactly — the
riskiest part of the migration. Green light to build gold on top.
**Files:** none (runtime validation via a scratch notebook; `silver_ref` and a
small `bronze` load created in Databricks, not in the repo)
**Notes:** Dedup was a no-op here (single scrape date) — the latest-wins logic
gets exercised for real once multiple scrape dates are loaded in the bootstrap.

## [2026-07-12 21:19] — Correct catalog name: workspace -> corn_off_the_cob

**What:** Point dbt profile, dbt source, and the bronze notebook at the real
catalog `corn_off_the_cob`.
**Why:** Reverses the catalog choice from the increment-1 planning (we picked
`workspace` because the old notebooks hardcoded it). The live workspace has no
`workspace` catalog — `SELECT current_catalog()` returned `corn_off_the_cob`
(which the README had right all along). `dbt debug` passed earlier only because
it tests connectivity, not catalog existence, so this would have failed at the
first `dbt run`.
**Files:** `dbt/profiles.yml.example`, `dbt/models/staging/_sources.yml`,
`databricks/bronze_autoloader_databricks.py` (local `dbt/profiles.yml` updated
too, gitignored)
**Notes:** Lesson: verify object names against the live warehouse, not stale
code comments.

## [2026-07-12 20:48] — Silver staging models in dbt (increment 3)

**What:** Added the 8 silver staging models as dbt SQL, plus the `bronze` source
declaration, reproducing the old notebook's flatten step. Also fixed the `sk`
macro (Jinja has no `*args`; extra args arrive as `varargs`).
**Why:** dbt now owns bronze→silver. These models unnest the nested bronze
records (matters→actions→votes, meetings→agenda_items, etc.) into flat tables.
**Files:** `dbt/models/staging/_sources.yml`, `dbt/models/staging/stg_*.sql` (8),
`dbt/macros/surrogate_key.sql` (fix)
**Notes:** Unnesting uses `LATERAL VIEW (pos)explode`. `action_seq` is taken from
`posexplode` position so it matches across `stg_actions`/`stg_votes` (the gold
join key). All 8 pass `dbt compile`; still UNVALIDATED against real data — next
step is a small one-partition bronze load to diff against the old notebook
tables before the full backfill bootstrap.

## [2026-07-12 19:52] — Bronze rework: Auto Loader lands nested Delta (increment 2)

**What:** Rewrote the Auto Loader notebook so it only *ingests* — landing whole
nested records into `bronze.matters` / `bronze.meetings` — and dropped the
flattening logic (moving to dbt). Renamed `silver_autoloader_databricks.py` →
`bronze_autoloader_databricks.py`.
**Why:** Under the new design dbt owns bronze→silver→gold; Auto Loader's only
remaining job is the file-by-file incremental read that dbt can't do. Splitting
ingestion from flattening gives a clean, testable bronze layer.
**Files:** `databricks/bronze_autoloader_databricks.py` (new), removed
`databricks/silver_autoloader_databricks.py`
**Notes:** Kept the explicit schemas (typed boundary) and lineage columns
unchanged; dates still land as raw strings (dbt parses them). `SRC` now points at
`gs://cotc_raw` and must run on a CLASSIC cluster (serverless blocks GCS egress —
a limit that applies only to this step). Not runnable until the bootstrap
(increment 6). README still references the old notebook name — fix in the docs
increment.

## [2026-07-12 19:46] — Scaffold dbt project (increment 1 of dbt migration)

**What:** Added a `dbt/` project (config, connection template, macros) that will
replace the hand-run PySpark gold notebooks with SQL models. Verified with
`dbt debug` against a serverless SQL warehouse ("All checks passed").
**Why:** Course requires dbt for silver→gold, and we want the silver+gold
transforms automated on a weekly schedule inside a Databricks Job. This is the
foundation; no tables are built yet.
**Files:** `dbt/dbt_project.yml`, `dbt/macros/{generate_schema_name,surrogate_key}.sql`,
`dbt/profiles.yml.example`, `dbt/.env.example`, `dbt/requirements-dbt.txt`, `.gitignore`
**Notes:** Kept the `xxhash64` surrogate-key formula from the notebooks so dbt
output will have byte-identical keys — lets us diff-validate the migration table
by table. Hit a macOS/Python TLS snag (`self-signed certificate in chain`): cause
was Python not finding its trust list, fixed by pointing `SSL_CERT_FILE` at
`certifi`. Not a proxy/network issue — `openssl` was clean the whole time.

<!-- Template for new entries:
## [YYYY-MM-DD HH:MM] — Brief title

**What:** One-line summary of what changed
**Why:** The reason — what problem it solves or feature it adds
**Files:** List of files touched
**Notes:** Caveats, tradeoffs, follow-ups, retrospective notes (optional)
-->
