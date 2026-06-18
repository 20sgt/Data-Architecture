-- 01_staging.sql — Silver layer: 1-to-1 with legistar_scrape.py output
--
-- One table per nested collection in the scraper's Matter dataclass.
-- Raw date strings are kept as TEXT and parsed in the star transform, not here —
-- keeps staging simple to reload and avoids silent NULL on unexpected formats.
-- `ingest_date` + `scraped_at` are lineage columns: the transform uses them to
-- pick the latest scrape per matter when multiple runs overlap.
--
-- Compatible with DuckDB (local dev) and Databricks SQL (Delta tables).
-- Run this file before 02_star.sql.

-- ── stg_matters ──────────────────────────────────────────────────────────────
-- One row per matter per scrape run.
-- matter_id is extracted from the ID= param of detail_url by the loader
-- (e.g. "https://sfgov.legistar.com/LegislationDetail.aspx?ID=7994804&..." → 7994804).
CREATE TABLE IF NOT EXISTS stg_matters (
    matter_id           INTEGER NOT NULL,  -- Legistar internal ID (from URL)
    matter_file         TEXT    NOT NULL,  -- human-facing file number ("260439")
    detail_url          TEXT,
    name                TEXT,              -- short subject line
    title               TEXT,             -- full abstract paragraph (keyword corpus)
    matter_type         TEXT,              -- Ordinance | Resolution | etc.
    status              TEXT,              -- raw Legistar status string
    lifecycle           TEXT,             -- scraper-derived bucket: passed|in_works|other
    introduced_raw      TEXT,             -- "5/5/2026" — parsed to date in transform
    on_agenda_raw       TEXT,
    final_action_raw    TEXT,
    enactment_date_raw  TEXT,
    enactment_number    TEXT,
    in_control          TEXT,             -- committee name; resolved to committee_sk in transform
    full_text           TEXT,             -- PDF text if --full-text flag was used, else NULL
    ingest_date         DATE      NOT NULL,
    scraped_at          TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- ── stg_actions ──────────────────────────────────────────────────────────────
-- One row per action (history entry) per matter per scrape run.
-- action_seq preserves the order of actions as they appear on the page.
CREATE TABLE IF NOT EXISTS stg_actions (
    matter_id       INTEGER NOT NULL,
    action_seq      INTEGER NOT NULL,  -- 0-indexed position in the actions array
    action_date_raw TEXT,             -- "5/20/2026"
    body            TEXT,             -- committee/body name — resolved to committee_sk in transform
    action          TEXT,             -- e.g. "RECOMMENDED", "PASSED"
    result          TEXT,             -- "Pass" | "Fail"
    history_url     TEXT,             -- HistoryDetail.aspx URL (source of votes)
    ingest_date     DATE      NOT NULL,
    scraped_at      TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- ── stg_votes ────────────────────────────────────────────────────────────────
-- One row per per-member vote per action per matter per scrape run.
-- person_name resolved to person_sk via dim_person lookup in the transform.
CREATE TABLE IF NOT EXISTS stg_votes (
    matter_id   INTEGER NOT NULL,
    action_seq  INTEGER NOT NULL,
    person_name TEXT    NOT NULL,
    vote_value  TEXT    NOT NULL,  -- Aye | No | Absent | Excused | Recused
    ingest_date DATE      NOT NULL,
    scraped_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- ── stg_attachments ──────────────────────────────────────────────────────────
-- One row per attachment per matter per scrape run.
-- document_id extracted from the ID= param of the View.ashx URL.
CREATE TABLE IF NOT EXISTS stg_attachments (
    matter_id       INTEGER NOT NULL,
    attachment_seq  INTEGER NOT NULL,  -- 0-indexed position in the attachments array
    attachment_name TEXT,
    attachment_url  TEXT,
    document_id     INTEGER,           -- extracted from ID= in View.ashx URL; NULL if absent
    ingest_date     DATE      NOT NULL,
    scraped_at      TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- ── stg_sponsors ─────────────────────────────────────────────────────────────
-- One row per sponsor name per matter per scrape run.
-- sponsor_pos drives sponsor_type assignment in the transform:
-- pos 0 → 'primary', pos > 0 → 'co' (Legistar doesn't distinguish them natively).
CREATE TABLE IF NOT EXISTS stg_sponsors (
    matter_id    INTEGER NOT NULL,
    sponsor_pos  INTEGER NOT NULL,  -- 0-indexed
    sponsor_name TEXT    NOT NULL,
    ingest_date  DATE      NOT NULL,
    scraped_at   TIMESTAMP NOT NULL DEFAULT current_timestamp
);
