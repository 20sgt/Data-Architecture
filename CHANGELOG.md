# Changelog

Newest entries at the top.

## [2026-08-08 12:15] — Gold is consumable: docs, tests, grants + failure alerting (increment 8)

**What:** Made the gold layer usable by someone other than an admin, and made the
weekly Job's failures visible.
**Why:** A teammate with a workspace login could not read gold, had no column
descriptions if they could, and nothing re-verified the data after July.
**Files:** `dbt/models/gold/schema.yml` (new), `dbt/tests/*.sql` (3 new),
`dbt/dbt_project.yml`, `databricks.yml`

- **Alerting** (`0f54372`) — `email_notifications.on_failure`. The job failed 4
  Wednesdays running and nobody knew. `on_success` deliberately omitted: routine
  success mail trains you to ignore the alert that matters.
- **Docs** — 10 relations, 73 columns described; `persist_docs` writes them into
  Unity Catalog as COMMENTs. Verified 0 uncommented columns.
- **Tests** — 0 → **49, all passing in 20s.** `dbt build` had been identical to
  `dbt run`. Every assertion was probed against the live tables *before* being
  written, so it encodes reality rather than a guess.
- **Grants** — `select` on gold to `account users`, reapplied every run (a rebuilt
  table is a new object and loses its grants otherwise), plus one-time
  `USE CATALOG` / `USE SCHEMA`. Scope is gold only.

**Two documented data-quality facts, previously unwritten:** `fact_vote.meeting_sk`
is 3.7% NULL (21,828/587,165) and `fact_matter_action.meeting_sk` is **58% NULL**
(103,669/179,247). Both are legitimate LEFT-join gaps via `history_id`, but the
58% would badly mislead anyone building meeting-centric analysis, so it is now
called out in the column description rather than discovered the hard way.

**Gotchas worth keeping:**
- Unity Catalog resolves principals at the ACCOUNT level. Granting to the
  workspace-local group `users` fails with `PRINCIPAL_DOES_NOT_EXIST`; the
  account-level `account users` is the equivalent.
- A `dbt test` against a STOPPED serverless warehouse appeared to hang for 12
  minutes. Nothing was broken — the same tests took 20s once the warehouse was
  warm. Verify the warehouse state before debugging a Databricks "hang".
- dbt 1.11 deprecates top-level generic-test args; they now nest under
  `arguments:`. 16 occurrences fixed at authoring time.

**SECURITY: over-broad grants on `gold` found and revoked.** `account users` held
`ALL_PRIVILEGES`, `MANAGE` and `EXTERNAL_USE_SCHEMA` **directly on the `gold`
schema** (`inherited_from = NONE`) — DROP/MODIFY plus the ability to re-grant.
This pre-dated today's work: only `USE CATALOG`/`USE SCHEMA` were granted today,
and `gold_ref` carries the identical triple despite never being touched, so both
date from schema creation in July. **The gap was never that teammates lacked read
access — it was that everyone had write access.**

Revoked all three. Note the trap: `REVOKE ALL PRIVILEGES` also removes `USE_SCHEMA`,
which silently breaks reads (SELECT on a table is useless without USE SCHEMA on its
schema), so it had to be re-granted. Verified end state:

| level | privilege |
|-------|-----------|
| catalog `corn_off_the_cob` | `USE_CATALOG` |
| schema `gold` | `USE_SCHEMA` |
| all 10 gold tables | `SELECT` |

`gold_ref` still carries the over-broad grants — left alone deliberately; it is the
spent July validation fixture and gets dropped in the cleanup increment.

**Not yet verified:** nobody has actually queried gold as a non-admin. The real
test is a teammate running
`SELECT * FROM corn_off_the_cob.gold.member_vote_record LIMIT 10` from their own
login. Everything above is confirmed from the grant tables, not from a real read.

## [2026-08-08 11:48] — Weekly transform Job runs end to end in the cloud (increment 7)

**What:** First successful cloud run of `weekly_transform` — `bronze_ingest` +
`dbt_build`, 8m24s. Required four fixes: CLI auth moved to OAuth; the dbt task
now pins its catalog; the ingest cluster dropped to a single node; and the
ingest cluster now enables Unity Catalog. Also **paused** the dev schedule,
which had been live since July.
**Why:** Goal: stop running the transform by hand. The Job existed and had been
firing weekly since 2026-07-15 — and failing every time, unnoticed.
**Files:** `databricks.yml` (commits `dec39e0`, `3a91516`, `47eb3d1`, `f329527`)
**Notes:** Four defects, none of them code, all of them gaps between the config
we wrote and the resource that got created — `bundle validate` passed through
every one. Corrects two earlier claims: `bundle deploy` HAD succeeded (around
Jul 13), and the CHANGELOG's "never deployed" was wrong.

- *Auth:* the July diagnosis (token scope) was wrong; the token was simply dead.
  `databricks current-user me` settles this in seconds and should be the first
  move on any Databricks auth error. Switched to OAuth (`databricks auth login`)
  — no expiry, no scope choice, token in the OS keyring. PATs are now labelled
  legacy in Databricks' own docs.
- *Quota:* each GCP node takes 30 GB pd-ssd + 150 GB pd-balanced = 180 GB, and
  pd-balanced counts against SSD_TOTAL_GB. us-west1 allows 500, so 1 driver +
  2 workers (540 GB) was never satisfiable — 5/5 failures. GCP's "try again
  later" wording concealed a hard ceiling. Single node = 180 GB, and capacity
  was never the constraint: the 39,723-file bootstrap ran on one node in July.
- *Unity Catalog:* UI-created clusters default to a UC-enabled security mode;
  bundle-declared ones do not. Same notebook, different metastore underneath.
- *Silence is the real bug:* four scheduled failures produced no signal because
  the Job has no `email_notifications`. Add before any prod target goes UNPAUSED.

**Data impact: none.** Post-run counts identical to the 2026-07-31 baseline
(dim_matter 38,724 / fact_vote 587,165 / fact_matter_action 179,247, UNMAPPED 0).
That is the correct result, and it exposed the next problem — see below.

**FOLLOW-UP (not fixed, upstream of this work): the scraper has produced no data
since 2026-07-22.** The transform half is healthy; the collection half is not.
- `2026-07-29` — Cloud Run execution never started: "Resource readiness deadline
  exceeded". Infra failure, no partition created.
- `2026-08-05` — execution reported **success** in ~74s (vs ~3 min on 2026-07-22)
  and wrote **0 JSON files**, leaving an empty `ingest_date=2026-08-05/` folder.
  Exit code 0 with no output is the dangerous case: nothing downstream can tell
  it apart from "no new legislation this week".
Needs the Aug 5 container logs to diagnose. `origin/fix/month-boundary-window`
is a tempting lead but has no commits ahead of `origin/main`, so it is probably
already merged — do not assume it explains this.

## [2026-07-30 20:51] — Historical bootstrap loaded + disposition map expanded (increment 6)

**What:** Ran the one-time full bootstrap. Auto Loader landed the whole bucket into
bronze (39,723 matters / 4,854 meetings, one row per file, no dupes), then
`dbt build` produced the full gold on 26 years of data: dim_matter 38,724,
fact_vote 587,165, fact_matter_action 179,247, **0 orphans**. The backfill surfaced
13 previously-unseen statuses (249 UNMAPPED matters); **expanded the disposition
taxonomy** with distinct terminal values `failed`/`vetoed`/`withdrawn` and classified
all 13 — **UNMAPPED is now 0**.
**Why:** Goal 3 (process the historical backfill). The UNMAPPED tripwire caught real
new statuses instead of silently mislabeling them.
**Files:** `dbt/models/gold/dim_matter.sql` (final_disposition + lifecycle maps).
**Notes:** Latest-wins dedup collapsed ~1,000 multi-scrape matters (39,723 bronze
rows → 38,724 distinct matters) — first real exercise of that logic. 3 low-volume
judgment-call statuses (`litigation-attorney`, `for immediate adoption`, `completed`)
are flagged in-code for later domain review. Full gold build ran in 45s on the
serverless SQL warehouse. Also fixed a None-guard bug in the bronze row-count print.

## [2026-07-12 21:44] — Serving view member_vote_record in dbt (increment 5)

**What:** Reconstructed the `member_vote_record` serving view as a dbt model
(materialized as a `view`): one row per vote, joining member (dim_person),
legislation + outcome (dim_matter), acting body (dim_committee), and meeting
(dim_meeting, LEFT).
**Why:** It's what the dashboard queries; the original notebook was referenced in
the README but never existed in git, so it was rebuilt from the README's
description.
**Files:** `dbt/models/gold/member_vote_record.sql`
**Notes:** No reference to diff against (never existed) — validated by sanity
check: `view_rows` = `fact_vote` rows (no fan-out) and zero NULL members. All
joins LEFT so a vote is never dropped; dims are unique on their keys so no
fan-out.

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
