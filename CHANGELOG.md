# Changelog

Newest entries at the top.

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
