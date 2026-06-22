-- 04_meeting_star.sql — Gold layer owned by the scrape-by-meeting slice (milestone-3 ERD).
--
-- SCOPE (decision "Silver + my uncontested gold"): this file builds ONLY the tables the
-- meeting slice owns or co-owns with no cross-slice write conflict:
--     dim_meeting, dim_action_type, dim_document (meeting docs), bridge_meeting_document,
--     dim_committee (provisional seed — see note).
-- fact_matter_action and fact_vote are deliberately NOT built here. Both slices surface the
-- SAME HistoryDetail roll-call, so the canonical facts are assembled in a later JOINT
-- reconciliation step from stg_meeting_* + the legislation staging, deduped on the shared
-- natural keys, using ONE shared label->code map (scrape/action_types.py). See the contract
-- block at the bottom and docs/meeting_pipeline_design.md.
--
-- All CREATE ... IF NOT EXISTS: on a standalone meeting branch these create the milestone-3
-- shapes; at merge with 02_star.sql the team reconciles dim_committee/dim_document (the
-- legislation slice's 02_star.sql currently has the older milestone-2 shapes for those two).
-- Surrogate keys (*_sk) are assigned by the transform via DuckDB sequences. Run 03 first.

-- ── dim_committee ─────────────────────────────────────────────────────────────
-- Authoritatively owned by the legislation slice (seeded from bodies.json with real BodyIds).
-- The meeting slice PROVISIONALLY seeds it from distinct calendar body names so dim_meeting
-- FKs resolve on a standalone branch: committee_id is left NULL, matched on name at merge.
CREATE TABLE IF NOT EXISTS dim_committee (
    committee_sk   INTEGER PRIMARY KEY,
    committee_id   BIGINT,              -- Legistar BodyId; NULL for provisional meeting-seeded rows
    committee_name TEXT    NOT NULL,
    committee_type TEXT,                -- Full Board | Standing Committee | Special
    is_active      BOOLEAN,
    UNIQUE (committee_name)
);

-- ── dim_action_type ───────────────────────────────────────────────────────────
-- Lookup that replaces the old inline CHECK enum. Seeded from scrape/action_types.py
-- (DIM_ACTION_TYPE_SEED) — the SAME module both scrapers must use to map raw labels, so
-- action_type_code stays consistent across the fact dedup key.
CREATE TABLE IF NOT EXISTS dim_action_type (
    action_type_code TEXT PRIMARY KEY,  -- e.g. PASSED_BOARD_2ND_READING, RECOMMENDED, OTHER
    action_category  TEXT,              -- introduction | referral | committee | board | amendment | disposition | other
    description      TEXT
);

-- ── dim_meeting ───────────────────────────────────────────────────────────────
-- Owned by scrape-by-meeting. Flat dimension (not SCD2) — a meeting is a fixed event;
-- re-scrapes UPDATE status/url/clip fields in place (e.g. minutes Draft -> Final).
CREATE TABLE IF NOT EXISTS dim_meeting (
    meeting_sk      INTEGER PRIMARY KEY,
    meeting_id      BIGINT  NOT NULL,   -- Legistar EventId
    event_guid      TEXT    NOT NULL,   -- mandatory for refetch (ID alone -> HTTP 410)
    committee_sk    INTEGER REFERENCES dim_committee(committee_sk),  -- nullable until resolved
    meeting_date    DATE,
    meeting_time    TIME,
    location        TEXT,
    meeting_subtype TEXT,
    agenda_status   TEXT,
    minutes_status  TEXT,
    agenda_url      TEXT,
    minutes_url     TEXT,
    video_clip_id   TEXT,               -- Granicus clip id -> video/audio/transcript
    UNIQUE (meeting_id)
);

-- ── dim_document ──────────────────────────────────────────────────────────────
-- Co-owned: matter attachments (legislation slice) AND meeting docs (this slice).
-- document_source disambiguates; document_id is NULL for meeting docs (no MatterAttachmentId).
-- No UNIQUE on document_id (nullable for meeting docs) — the transform dedups meeting docs
-- on (document_source, document_url).
CREATE TABLE IF NOT EXISTS dim_document (
    document_sk     INTEGER PRIMARY KEY,
    document_id     BIGINT,             -- MatterAttachmentId; NULL for meeting docs
    document_source TEXT    NOT NULL,   -- matter_attachment | meeting_agenda | meeting_minutes | transcript
    document_title  TEXT,
    document_url    TEXT,
    document_type   TEXT,
    body_text       TEXT,               -- extracted text; NULL unless --with-text
    scraped_at      TIMESTAMP
);

-- ── bridge_meeting_document ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bridge_meeting_document (
    meeting_sk  INTEGER NOT NULL REFERENCES dim_meeting(meeting_sk),
    document_sk INTEGER NOT NULL REFERENCES dim_document(document_sk),
    PRIMARY KEY (meeting_sk, document_sk)
);

-- ════════════════════════════════════════════════════════════════════════════
-- CROSS-SLICE FACT CONTRACT (NOT built here — assembled at the joint merge)
-- ════════════════════════════════════════════════════════════════════════════
-- fact_matter_action  natural key: (matter_id, meeting_id, action_type_code)
-- fact_vote           natural key: (matter_id, meeting_id, person_id)
--
-- The meeting slice supplies, per agenda item, everything the merge needs:
--   matter_file -> dim_matter.matter_id   (join on matter_file)
--   meeting_id  -> dim_meeting.meeting_sk  (the meeting slice is the ONLY source of a clean
--                                           EventId, so it fills meeting_sk at the merge)
--   action_raw  -> action_type_code        (via scrape/action_types.normalize_action)
--   action_result, action_text, votes(person_id, vote_value), body_scope(from body_name)
-- The legislation slice supplies the same rows with meeting_sk NULL; the merge dedups on the
-- natural keys. body_scope = 'board' when body_name = 'Board of Supervisors' else 'committee'.
-- vote_value: "No" -> "Nay" in this step. See docs/meeting_pipeline_design.md §Cross-slice.
