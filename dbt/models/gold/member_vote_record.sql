{{ config(materialized='view') }}

-- SERVING VIEW for the dashboard: ONE row per recorded vote, denormalized so the
-- dashboard can filter by member and time period without writing joins itself.
--
-- Joins each vote to:
--   dim_person   — the member who voted
--   dim_matter   — the legislation and its current outcome
--   dim_committee— the acting body
--   dim_meeting  — the meeting it happened in (LEFT: meeting_sk is NULL when a
--                  vote doesn't resolve to a known meeting)
-- All joins are LEFT so a vote is never dropped if a lookup is unexpectedly
-- missing. Each dimension is unique on its surrogate key, so no row fan-out —
-- the output stays one row per vote.
select
    v.vote_sk,
    v.vote_date,
    v.vote_value,

    -- member
    p.person_id,
    p.full_name              as member_name,

    -- legislation
    m.matter_file,
    m.matter_name,
    m.matter_title,
    m.matter_type,

    -- outcome
    m.status                 as matter_status,
    m.lifecycle,
    m.final_disposition,

    -- acting body + meeting context
    c.committee_name,
    mt.meeting_date,

    -- surrogate keys, for any further filtering/joins downstream
    v.matter_sk,
    v.person_sk,
    v.committee_sk,
    v.meeting_sk
from {{ ref('fact_vote') }} v
left join {{ ref('dim_person') }}     p  on v.person_sk    = p.person_sk
left join {{ ref('dim_matter') }}     m  on v.matter_sk    = m.matter_sk
left join {{ ref('dim_committee') }}  c  on v.committee_sk = c.committee_sk
left join {{ ref('dim_meeting') }}    mt on v.meeting_sk   = mt.meeting_sk
