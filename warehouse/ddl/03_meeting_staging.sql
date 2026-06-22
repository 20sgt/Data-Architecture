-- 03_meeting_staging.sql — Silver layer for the scrape-by-meeting slice.
--
-- One table per nested collection in scrape/legistar_meetings.py's Meeting dataclass
-- (meeting -> agenda_items -> votes; meeting -> documents). 1-to-1 with the bronze JSON.
-- Raw date/time strings stay TEXT and are parsed in the star transform (keeps reloads
-- trivial and avoids silent NULLs on unexpected formats). `ingest_date` + `scraped_at`
-- are lineage columns: the transform picks the latest scrape per meeting.
--
-- Companion to 01_staging.sql (legislation slice). Compatible with DuckDB + Databricks SQL.
-- Run before 04_meeting_star.sql.

-- ── stg_meetings ──────────────────────────────────────────────────────────────
-- One row per meeting per scrape run. meeting_id = Legistar EventId; event_guid is
-- mandatory for every refetch (ID alone -> HTTP 410).
CREATE TABLE IF NOT EXISTS stg_meetings (
    meeting_id       BIGINT  NOT NULL,   -- Legistar EventId
    event_guid       TEXT    NOT NULL,   -- Legistar EventGUID
    body_name        TEXT,               -- resolved to committee_sk in the transform
    meeting_date_raw TEXT,               -- "6/16/2026"
    meeting_time_raw TEXT,               -- "2:00 PM"
    location         TEXT,               -- subtype stripped off into meeting_subtype
    meeting_subtype  TEXT,               -- Regular | Recessed | Special | ...
    agenda_status    TEXT,               -- Final | Draft
    minutes_status   TEXT,               -- Final | Final Draft | Draft | NULL
    agenda_url       TEXT,
    minutes_url      TEXT,
    video_clip_id    TEXT,               -- Granicus clip id (ID1=)
    ingest_date      DATE      NOT NULL,
    scraped_at       TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- ── stg_meeting_agenda_items ──────────────────────────────────────────────────
-- One row per gridMain agenda item per meeting per scrape run.
-- item_seq preserves agenda order. matter_file is the ONLY join key to dim_matter
-- (the file string, NOT the LegislationDetail URL id). action_raw is the verbatim
-- Legistar label; normalize to dim_action_type via scrape/action_types.py at merge.
CREATE TABLE IF NOT EXISTS stg_meeting_agenda_items (
    meeting_id    BIGINT  NOT NULL,
    item_seq      INTEGER NOT NULL,
    matter_file   TEXT,                  -- gridMain c0 — join key to dim_matter
    agenda_number TEXT,
    matter_name   TEXT,
    matter_type   TEXT,
    matter_status TEXT,
    title         TEXT,
    action_raw    TEXT,                  -- verbatim action label (NULL = no action this meeting)
    action_result TEXT,                  -- Pass | Fail | NULL
    history_id    BIGINT,                -- MatterHistory id (distinct from EventId)
    history_url   TEXT,
    action_text   TEXT,                  -- HistoryDetail lblActionText (full motion text)
    ingest_date   DATE      NOT NULL,
    scraped_at    TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- ── stg_meeting_votes ─────────────────────────────────────────────────────────
-- One row per per-person roll-call vote per agenda item per meeting per scrape run.
-- person_id (from PersonDetail.aspx?ID=) is the robust person join key the meeting
-- slice uniquely captures. vote_value_raw is the verbatim literal ("No" -> "Nay" in gold).
CREATE TABLE IF NOT EXISTS stg_meeting_votes (
    meeting_id     BIGINT  NOT NULL,
    item_seq       INTEGER NOT NULL,
    person_id      BIGINT,               -- Legistar PersonId (NULL if link absent)
    person_name    TEXT    NOT NULL,
    vote_value_raw TEXT    NOT NULL,     -- Aye | No | Excused | Absent | Recused
    ingest_date    DATE      NOT NULL,
    scraped_at     TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- ── stg_meeting_documents ─────────────────────────────────────────────────────
-- One row per meeting document (agenda / minutes / transcript) per scrape run.
CREATE TABLE IF NOT EXISTS stg_meeting_documents (
    meeting_id      BIGINT  NOT NULL,
    doc_seq         INTEGER NOT NULL,
    document_source TEXT    NOT NULL,    -- meeting_agenda | meeting_minutes | transcript
    document_title  TEXT,
    document_url    TEXT,
    body_text       TEXT,               -- populated only when --with-text was used
    ingest_date     DATE      NOT NULL,
    scraped_at      TIMESTAMP NOT NULL DEFAULT current_timestamp
);
