-- 02_star.sql — Gold layer: star schema (group ERD + proposed additions)
--
-- OWNERSHIP:
--   Lynn's slice:   dim_committee, dim_person, dim_matter, dim_document, dim_subject,
--                   fact_vote, fact_matter_action, bridge_matter_sponsor,
--                   bridge_matter_document, bridge_matter_subject
--   Teammate's:     dim_meeting, fact_committee_membership (stubs at bottom of file)
--
-- Surrogate keys (*_sk) are assigned by the transform layer using ROW_NUMBER() —
-- not auto-increment columns — so they remain stable across DuckDB and Databricks.
--
-- Run 01_staging.sql first, then this file.

-- ── dim_committee ─────────────────────────────────────────────────────────────
-- Seeded from spike/data/bodies.json (authoritative Legistar BodyId → BodyName).
-- Stable reference data; does not need SCD versioning.
-- Owned by the legislation slice; the meeting slice resolves committee_sk from this dim.
CREATE TABLE IF NOT EXISTS dim_committee (
    committee_sk    INTEGER PRIMARY KEY,
    committee_id    INTEGER NOT NULL,  -- BodyId from bodies.json
    committee_name  TEXT    NOT NULL,
    committee_type  TEXT,              -- BodyTypeName (e.g. "Standing Committee")
    is_active       BOOLEAN,
    UNIQUE (committee_id)
);

-- ── dim_person ────────────────────────────────────────────────────────────────
-- Supervisors and any other named persons (committee members, staff).
-- Seeded from spike/data/persons.json; vote names conformed against full_name.
-- SCD type 2: a new row is inserted when a supervisor changes district or term.
-- Queries for current members: WHERE is_current = true.
CREATE TABLE IF NOT EXISTS dim_person (
    person_sk             INTEGER PRIMARY KEY,
    person_id             INTEGER NOT NULL,  -- PersonId from persons.json
    full_name             TEXT    NOT NULL,
    district              INTEGER,
    party                 TEXT,
    gender                TEXT,
    birth_date            DATE,
    supervisor_term_start DATE,
    supervisor_term_end   DATE,
    effective_from        DATE    NOT NULL,
    effective_to          DATE,             -- NULL = currently open
    is_current            BOOLEAN NOT NULL
);

-- ── dim_matter ────────────────────────────────────────────────────────────────
-- One row per version of a matter (SCD type 2 on status fields).
-- When status changes (e.g. in_works → passed), the old row is closed:
--   effective_to = change date, is_current = false.
-- A new row is inserted with the updated status and is_current = true.
-- Queries for current state: WHERE is_current = true.
-- Queries for history: join on matter_id (natural key) across all versions.
--
-- NOTE: columns marked "PROPOSED ADDITION" are not in the group ERD as of 2026-06-11.
--       Please review before the group finalises the schema.
CREATE TABLE IF NOT EXISTS dim_matter (
    matter_sk                INTEGER PRIMARY KEY,
    matter_id                INTEGER NOT NULL,  -- natural key: Legistar ID from URL
    matter_file              TEXT,              -- human-facing file number ("260439")
    matter_title             TEXT,              -- full abstract paragraph
    matter_name              TEXT,              -- short subject line
    matter_type              TEXT,
    introduction_date        DATE,
    controlling_committee_sk INTEGER REFERENCES dim_committee(committee_sk),
    requester                TEXT,
    legistar_url             TEXT,
    -- PROPOSED ADDITIONS (status lifecycle) ──────────────────────────────────
    status                   TEXT,             -- raw Legistar status string
    lifecycle                TEXT,             -- passed | in_works | other
    final_action_date        DATE,
    enactment_date           DATE,
    enactment_number         TEXT,
    -- SCD type 2 versioning (same pattern as dim_person) ────────────────────
    effective_from           DATE    NOT NULL,
    effective_to             DATE,             -- NULL = currently open
    is_current               BOOLEAN NOT NULL
);

-- ── dim_document ──────────────────────────────────────────────────────────────
-- Attachments from the Legistar document store (Leg Ver PDFs, committee packets, etc.).
-- document_id from the ID= param of the View.ashx URL.
CREATE TABLE IF NOT EXISTS dim_document (
    document_sk    INTEGER PRIMARY KEY,
    document_id    INTEGER NOT NULL,
    document_title TEXT,
    document_url   TEXT,
    document_type  TEXT,
    body_text      TEXT,       -- populated only when --full-text was used
    scraped_at     TIMESTAMP,
    UNIQUE (document_id)
);

-- ── dim_subject ───────────────────────────────────────────────────────────────
-- Subject/topic tags for matters.
-- DATA SOURCE: open — no Legistar field maps directly to subjects.
-- Proposed: derive from matter_name/matter_title keywords or LLM tagging (later increment).
CREATE TABLE IF NOT EXISTS dim_subject (
    subject_sk   INTEGER PRIMARY KEY,
    subject_id   INTEGER NOT NULL,
    subject_name TEXT    NOT NULL,
    UNIQUE (subject_id)
);

-- ════════════════════════════════════════════════════════════════════════════
-- TEAMMATE'S TABLES (stubs — owned by the meeting slice)
-- Defined here so fact table FK references resolve in a single-database setup.
-- Do not populate from the legislation pipeline.
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS dim_meeting (
    meeting_sk    INTEGER PRIMARY KEY,
    meeting_id    INTEGER NOT NULL,
    committee_sk  INTEGER REFERENCES dim_committee(committee_sk),
    meeting_date  DATE,
    meeting_time  TIME,
    location      TEXT,
    agenda_url    TEXT,
    minutes_url   TEXT,
    UNIQUE (meeting_id)
);

-- ════════════════════════════════════════════════════════════════════════════
-- FACT TABLES
-- ════════════════════════════════════════════════════════════════════════════

-- ── fact_matter_action ────────────────────────────────────────────────────────
-- One row per legislative action (committee hearing, board vote, referral, etc.)
-- meeting_sk is NULLABLE: resolved via (committee_sk, action_date) lookup against
-- dim_meeting when the meeting slice has run; NULL otherwise.
-- PROPOSED ADDITION: action_result (not in group ERD).
CREATE TABLE IF NOT EXISTS fact_matter_action (
    matter_action_sk INTEGER PRIMARY KEY,
    matter_sk        INTEGER NOT NULL REFERENCES dim_matter(matter_sk),
    meeting_sk       INTEGER REFERENCES dim_meeting(meeting_sk),  -- nullable, cross-slice
    action_type_code TEXT    NOT NULL,   -- e.g. "RECOMMENDED", "PASSED ON FIRST READING"
    action_date      DATE,
    action_text      TEXT,
    action_result    TEXT               -- PROPOSED ADDITION: "Pass" | "Fail"
);

-- ── fact_vote ─────────────────────────────────────────────────────────────────
-- One row per per-member vote per action.
-- Grain: (matter_sk, action in fact_matter_action, person_sk).
-- meeting_sk nullable for the same reason as fact_matter_action.
CREATE TABLE IF NOT EXISTS fact_vote (
    vote_sk     INTEGER PRIMARY KEY,
    matter_sk   INTEGER NOT NULL REFERENCES dim_matter(matter_sk),
    meeting_sk  INTEGER REFERENCES dim_meeting(meeting_sk),  -- nullable, cross-slice
    person_sk   INTEGER NOT NULL REFERENCES dim_person(person_sk),
    vote_date   DATE,
    vote_value  TEXT NOT NULL,  -- Aye | No | Absent | Excused | Recused
    motion_text TEXT
);

-- ── fact_committee_membership ─────────────────────────────────────────────────
-- TEAMMATE'S TABLE (stub). SCD type 2 on membership.
CREATE TABLE IF NOT EXISTS fact_committee_membership (
    membership_sk  INTEGER PRIMARY KEY,
    person_sk      INTEGER REFERENCES dim_person(person_sk),
    committee_sk   INTEGER REFERENCES dim_committee(committee_sk),
    position       TEXT    NOT NULL,
    effective_from DATE    NOT NULL,
    effective_to   DATE,
    is_current     BOOLEAN
);

-- ════════════════════════════════════════════════════════════════════════════
-- BRIDGE TABLES
-- ════════════════════════════════════════════════════════════════════════════

-- ── bridge_matter_sponsor ────────────────────────────────────────────────────
-- Links matters to their sponsoring supervisors.
-- sponsor_type convention (Legistar doesn't distinguish natively):
--   sponsor_pos 0 → 'primary', pos > 0 → 'co'
CREATE TABLE IF NOT EXISTS bridge_matter_sponsor (
    matter_sk    INTEGER NOT NULL REFERENCES dim_matter(matter_sk),
    person_sk    INTEGER NOT NULL REFERENCES dim_person(person_sk),
    sponsor_type TEXT    NOT NULL,  -- 'primary' | 'co'
    PRIMARY KEY (matter_sk, person_sk)
);

-- ── bridge_matter_document ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bridge_matter_document (
    matter_sk   INTEGER NOT NULL REFERENCES dim_matter(matter_sk),
    document_sk INTEGER NOT NULL REFERENCES dim_document(document_sk),
    PRIMARY KEY (matter_sk, document_sk)
);

-- ── bridge_matter_subject ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bridge_matter_subject (
    matter_sk  INTEGER NOT NULL REFERENCES dim_matter(matter_sk),
    subject_sk INTEGER NOT NULL REFERENCES dim_subject(subject_sk),
    PRIMARY KEY (matter_sk, subject_sk)
);
