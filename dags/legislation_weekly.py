"""legislation_weekly — weekly SF legislation ETL pipeline.

Task chain (linear — each step must succeed before the next runs):

  scrape_matters → load_staging → transform_star

scrape_matters:
  Enumerates matters created in the 7-day window ending on the DAG's
  logical date, then re-scrapes every matter currently marked in_works
  (status changes won't appear in a created-date window). Writes one
  JSON file per matter to raw/matters/ingest_date=YYYY-MM-DD/.

load_staging:
  Reads that raw partition into the five staging tables. Idempotent:
  deletes all rows for this ingest_date before inserting, so a retry
  after a partial failure starts clean rather than resuming mid-run.

transform_star:
  Upserts dims (SCD type 2 for dim_matter) and inserts facts.
  meeting_sk is left NULL — the cross-slice meeting join runs separately
  once the teammate's calendar data is loaded.

Schedule: Monday 06:00 UTC. SF Board meets Tuesdays, so Monday catches
the prior week's introduced matters before the next meeting cycle.

Backfill: catchup=False. The 2020-2026 historical backfill is a
separate manual run (see docs/pipeline_design.md §9).

Local setup (once):
  pip install apache-airflow duckdb requests beautifulsoup4 lxml pypdf playwright
  playwright install chromium
  export AIRFLOW_HOME=$(pwd)/.airflow
  airflow db migrate
  airflow standalone          # starts scheduler + webserver on localhost:8080
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from airflow import DAG
from airflow.providers.standard.operators.python import PythonOperator

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
RAW_ROOT  = REPO_ROOT / "raw" / "matters"
DB_PATH   = REPO_ROOT / "warehouse" / "db" / "legislation.duckdb"

# Make scrape/ and warehouse/ importable inside task callables.
# Done inside callables (not at module level) so Airflow can parse the DAG
# file without requiring all dependencies to be installed on the scheduler.
def _add_repo_to_path() -> None:
    p = str(REPO_ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)


# ── task callables ────────────────────────────────────────────────────────────

def scrape_matters(**context) -> None:
    """Enumerate new matters + re-scrape open matters → raw landing zone."""
    _add_repo_to_path()
    import duckdb
    from playwright.sync_api import sync_playwright
    from scrape.legistar_scrape import enumerate_matters, scrape_matter, UA, _weekly

    ds           = context["ds"]                      # "YYYY-MM-DD" logical date
    ingest_date  = date.fromisoformat(ds)
    week_start   = ingest_date - timedelta(days=6)

    # 1. URLs for matters currently in_works — re-scrape so status changes surface.
    con = duckdb.connect(str(DB_PATH))
    open_urls = {
        row[0] for row in con.execute("""
            SELECT legistar_url FROM dim_matter
            WHERE lifecycle = 'in_works'
              AND is_current = true
              AND legistar_url IS NOT NULL
        """).fetchall()
    }
    con.close()

    # 2. Enumerate newly created matters via Playwright (postback search requires browser).
    with sync_playwright() as pw:
        browser  = pw.chromium.launch()
        page     = browser.new_page(user_agent=UA)
        new_urls: set[str] = set()
        for ws, we in _weekly(week_start, ingest_date):
            new_urls.update(enumerate_matters(ws, we, page))
        browser.close()

    all_urls = list(new_urls | open_urls)
    log.info("scraping %d URLs (%d new, %d open re-scrape)",
             len(all_urls), len(new_urls), len(open_urls - new_urls))

    # 3. Scrape detail pages with plain requests (no browser needed after enumeration).
    matters = [scrape_matter(url) for url in all_urls]

    # 4. Write one JSON file per matter to the immutable raw landing zone.
    out_dir = RAW_ROOT / f"ingest_date={ds}"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for m in matters:
        if not m.file_number:
            continue
        path = out_dir / f"{m.file_number}.json"
        path.write_text(
            json.dumps(
                {**dataclasses.asdict(m), "lifecycle": m.lifecycle},
                indent=2, ensure_ascii=False,
            )
        )
        written += 1

    log.info("wrote %d matter files → %s", written, out_dir)


def load_staging(**context) -> None:
    """Load the raw partition for this date into staging tables (idempotent)."""
    _add_repo_to_path()
    import duckdb
    from warehouse.load_staging import ensure_schema, load_partition

    ds          = context["ds"]
    ingest_date = date.fromisoformat(ds)
    src_dir     = RAW_ROOT / f"ingest_date={ds}"

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    ensure_schema(con)
    load_partition(con, src_dir, ingest_date)
    con.close()


def transform_star(**context) -> None:
    """Transform staging into the star schema for this date's partition.

    Clears the pipeline_runs record before running so that Airflow retries
    (which only reach here after load_staging has fully succeeded) start
    clean rather than being blocked by a prior partial run.
    """
    _add_repo_to_path()
    import duckdb
    from warehouse.transform_star import (
        ensure_sequences, seed_committees, seed_persons,
        transform_matters, transform_actions, transform_votes,
        transform_documents, transform_sponsors,
    )

    ds          = context["ds"]
    ingest_date = date.fromisoformat(ds)

    con = duckdb.connect(str(DB_PATH))
    ensure_sequences(con)

    # Clear any prior run record so a retry after load_staging succeeds
    # doesn't get blocked by the pipeline_runs guard in transform_star.main().
    con.execute("DELETE FROM pipeline_runs WHERE ingest_date = ?", [ingest_date])

    seed_committees(con)
    seed_persons(con)
    transform_matters(con, ingest_date)
    transform_actions(con, ingest_date)
    transform_votes(con, ingest_date)
    transform_documents(con, ingest_date)
    transform_sponsors(con, ingest_date)

    con.execute("INSERT INTO pipeline_runs VALUES (?, ?)", [ingest_date, datetime.now()])
    con.close()


# ── DAG definition ────────────────────────────────────────────────────────────

with DAG(
    dag_id="legislation_weekly",
    description="Weekly SF legislation scrape → staging → star schema",
    schedule="0 6 * * 1",          # Monday 06:00 UTC
    start_date=datetime(2026, 6, 1),
    catchup=False,                  # don't auto-backfill historical weeks
    default_args={
        "owner": "legislation",
        "retries": 1,
        "retry_delay": timedelta(minutes=5),
    },
    tags=["legislation"],
) as dag:

    t_scrape = PythonOperator(
        task_id="scrape_matters",
        python_callable=scrape_matters,
        doc_md="""
        **scrape_matters** — enumerate new matters + re-scrape open ones.
        Writes raw JSON to `raw/matters/ingest_date={{ ds }}/`.
        Requires: Playwright chromium, outbound network to sfgov.legistar.com.
        """,
    )

    t_load = PythonOperator(
        task_id="load_staging",
        python_callable=load_staging,
        doc_md="""
        **load_staging** — load raw partition into staging tables.
        Delete-then-insert idempotency: safe to retry from scratch.
        """,
    )

    t_transform = PythonOperator(
        task_id="transform_star",
        python_callable=transform_star,
        doc_md="""
        **transform_star** — upsert dims (SCD type 2) and insert facts.
        meeting_sk left NULL pending the teammate's calendar data.
        """,
    )

    # Linear chain: downstream tasks are blocked until upstream succeeds.
    # If load_staging fails, transform_star enters upstream_failed and does
    # not run on partial staging data.
    t_scrape >> t_load >> t_transform
