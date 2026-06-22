-- 02_star.sql — SHARED gold star schema (milestone-3 ERD, single source of truth).
--
-- This is the ONE gold DDL both slices target (reconciled on the `integration` branch). It matches
-- erd/schema.dbml exactly; the per-slice silver staging stays separate (01_staging.sql for the
-- legislation slice, 03_meeting_staging.sql for the meeting slice). The unified builder
-- warehouse/transform_gold.py populates everything here from both stagings.
--
-- Reconciliation notes vs the old per-slice DDLs:
--   * dim_matter is FLAT (per the ERD: "No current_status column — derive from latest
--     fact_matter_action"). The legislation slice's earlier SCD2/status columns are dropped.
--   * dim_document gains document_source + nullable document_id (meeting docs have no attachment id).
--   * dim_committee.committee_id is nullable + UNIQUE(committee_name) — seeded self-contained from
--     scraped body names (real BodyIds can be layered in later).
--   * dim_action_type lookup added; fact_vote gains body_scope; fact_matter_action gains action_result.
--
-- Surrogate keys (*_sk) are assigned by the transform via DuckDB sequences. FK REFERENCES are kept
-- for documentation/correctness; the transform rebuilds gold child-first so DuckDB's FK enforcement
-- is satisfied (Databricks/Delta treats FKs as informational). Compatible with DuckDB + Databricks SQL.
-- Hard CHECKs on free-text vote/action values are kept as comments (not enforced) so unconfirmed
-- live literals never abort a load. Run after 01_staging.sql / 03_meeting_staging.sql.

-- ════════════════════════════════════════════════════════════════════════════ Dimensions
-- ── dim_person ── SCD2: one row per person-version (key person_id across versions).
CREATE TABLE IF NOT EXISTS dim_person (
    person_sk             INTEGER PRIMARY KEY,
    person_id             BIGINT,             -- Legistar PersonId (real from meeting scrape; synthetic for legislation-only names)
    full_name             TEXT    NOT NULL,
    district              INTEGER,
    party                 TEXT,
    gender                TEXT,
    birth_date            DATE,
    supervisor_term_start DATE,
    supervisor_term_end   DATE,
    effective_from        DATE    NOT NULL,
    effective_to          DATE,               -- NULL = current
    is_current            BOOLEAN NOT NULL
);

-- ── dim_committee ── flat; seeded self-contained from scraped body names (committee_id nullable).
CREATE TABLE IF NOT EXISTS dim_committee (
    committee_sk   INTEGER PRIMARY KEY,
    committee_id   BIGINT,                    -- Legistar BodyId; NULL until a bodies.json seed is layered in
    committee_name TEXT    NOT NULL,
    committee_type TEXT,                      -- Full Board | Standing Committee | Special
    is_active      BOOLEAN,
    UNIQUE (committee_name)
);

-- ── dim_matter ── FLAT (one row per matter); status derived from facts, not stored here.
CREATE TABLE IF NOT EXISTS dim_matter (
    matter_sk                INTEGER PRIMARY KEY,
    matter_id                BIGINT,           -- Legistar MatterId (URL id); NULL for html_stub matters
    matter_file              TEXT,             -- file number, e.g. "260439" — the join key from a meeting agenda row
    matter_title             TEXT,
    matter_name              TEXT,
    matter_type              TEXT,
    introduction_date        DATE,
    controlling_committee_sk INTEGER REFERENCES dim_committee(committee_sk),
    requester                TEXT,
    legistar_url             TEXT,
    matter_source            TEXT,             -- legislation | html_stub (stub = referenced by a meeting but not yet scraped)
    UNIQUE (matter_file)
);

-- ── dim_subject ── (no per-matter source found yet; table kept, currently unpopulated).
CREATE TABLE IF NOT EXISTS dim_subject (
    subject_sk   INTEGER PRIMARY KEY,
    subject_id   BIGINT  NOT NULL,
    subject_name TEXT    NOT NULL,
    UNIQUE (subject_id)
);

-- ── dim_document ── matter attachments (legislation) AND meeting docs (meeting); source disambiguates.
CREATE TABLE IF NOT EXISTS dim_document (
    document_sk     INTEGER PRIMARY KEY,
    document_id     BIGINT,                   -- MatterAttachmentId; NULL for meeting docs
    document_source TEXT    NOT NULL,         -- matter_attachment | meeting_agenda | meeting_minutes | transcript
    document_title  TEXT,
    document_url    TEXT,
    document_type   TEXT,
    body_text       TEXT,                     -- extracted text; NULL unless --with-text
    scraped_at      TIMESTAMP
);

-- ── dim_meeting ── flat; owned by the meeting slice. event_guid mandatory for refetch.
CREATE TABLE IF NOT EXISTS dim_meeting (
    meeting_sk      INTEGER PRIMARY KEY,
    meeting_id      BIGINT  NOT NULL,         -- Legistar EventId
    event_guid      TEXT    NOT NULL,
    committee_sk    INTEGER REFERENCES dim_committee(committee_sk),
    meeting_date    DATE,
    meeting_time    TIME,
    location        TEXT,
    meeting_subtype TEXT,
    agenda_status   TEXT,
    minutes_status  TEXT,
    agenda_url      TEXT,
    minutes_url     TEXT,
    video_clip_id   TEXT,
    UNIQUE (meeting_id)
);

-- ── dim_action_type ── lookup; seeded from scrape/action_types.py (the shared label->code map).
CREATE TABLE IF NOT EXISTS dim_action_type (
    action_type_code TEXT PRIMARY KEY,
    action_category  TEXT,                    -- introduction | referral | committee | board | amendment | disposition | other
    description      TEXT
);

-- ════════════════════════════════════════════════════════════════════════════ Facts
-- ── fact_matter_action ── grain: (matter, meeting, action). Natural key: (matter_id, meeting_id, action_type_code).
CREATE TABLE IF NOT EXISTS fact_matter_action (
    matter_action_sk INTEGER PRIMARY KEY,
    matter_sk        INTEGER REFERENCES dim_matter(matter_sk),
    meeting_sk       INTEGER REFERENCES dim_meeting(meeting_sk),   -- nullable: legislation rows unmatched to a meeting
    action_type_code TEXT    NOT NULL REFERENCES dim_action_type(action_type_code),
    action_result    TEXT,                    -- CHECK: Pass | Fail | NULL (not enforced)
    action_date      DATE,
    action_text      TEXT,                    -- raw label / full motion text (traceability to bronze)
    source           TEXT                     -- meeting | legislation (which crawl wrote the surviving row)
);

-- ── fact_vote ── grain: (matter, meeting, person). Natural key: (matter_id, meeting_id, person_id).
CREATE TABLE IF NOT EXISTS fact_vote (
    vote_sk     INTEGER PRIMARY KEY,
    matter_sk   INTEGER REFERENCES dim_matter(matter_sk),
    meeting_sk  INTEGER REFERENCES dim_meeting(meeting_sk),        -- nullable
    person_sk   INTEGER REFERENCES dim_person(person_sk),
    body_scope  TEXT    NOT NULL,             -- board | committee
    vote_date   DATE,
    vote_value  TEXT    NOT NULL,             -- canonical: Aye | Nay | Excused | Absent (+ Recused/Present passthrough)
    motion_text TEXT,
    source      TEXT
);

-- ── fact_committee_membership ── SCD2-shaped tenure (no live source yet; table kept).
CREATE TABLE IF NOT EXISTS fact_committee_membership (
    membership_sk  INTEGER PRIMARY KEY,
    person_sk      INTEGER REFERENCES dim_person(person_sk),
    committee_sk   INTEGER REFERENCES dim_committee(committee_sk),
    position       TEXT    NOT NULL,          -- Chair | Vice Chair | Member
    effective_from DATE    NOT NULL,
    effective_to   DATE,
    is_current     BOOLEAN
);

-- ════════════════════════════════════════════════════════════════════════════ Bridges (M:M)
CREATE TABLE IF NOT EXISTS bridge_matter_subject (
    matter_sk  INTEGER NOT NULL REFERENCES dim_matter(matter_sk),
    subject_sk INTEGER NOT NULL REFERENCES dim_subject(subject_sk),
    PRIMARY KEY (matter_sk, subject_sk)
);

CREATE TABLE IF NOT EXISTS bridge_matter_sponsor (
    matter_sk    INTEGER NOT NULL REFERENCES dim_matter(matter_sk),
    person_sk    INTEGER NOT NULL REFERENCES dim_person(person_sk),
    sponsor_type TEXT    NOT NULL,            -- Primary | Co
    PRIMARY KEY (matter_sk, person_sk, sponsor_type)
);

CREATE TABLE IF NOT EXISTS bridge_matter_document (
    matter_sk   INTEGER NOT NULL REFERENCES dim_matter(matter_sk),
    document_sk INTEGER NOT NULL REFERENCES dim_document(document_sk),
    PRIMARY KEY (matter_sk, document_sk)
);

CREATE TABLE IF NOT EXISTS bridge_meeting_document (
    meeting_sk  INTEGER NOT NULL REFERENCES dim_meeting(meeting_sk),
    document_sk INTEGER NOT NULL REFERENCES dim_document(document_sk),
    PRIMARY KEY (meeting_sk, document_sk)
);
