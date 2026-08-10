-- One row per (matter, action, vote) — the per-member roll call.
-- This is a TWO-level unnest: first walk each matter's actions list, then walk
-- each action's votes list. We chain two LATERAL VIEWs; the second one reads the
-- `action` struct produced by the first.
--
--   action_seq comes from posexplode(m.actions), so it matches stg_actions
--              exactly (that's the join key the gold vote facts rely on).
--   explode(action.votes) (not posexplode) — the notebook didn't number votes,
--              and it drops actions with no votes, same as F.explode.

select
    m.file_number                              as matter_file,
    action_seq,
    to_date(action.date, 'M/d/yyyy')           as action_date,
    action.history_id                          as history_id,

    vote.person_id                             as person_id,
    vote.person_name                           as person_name,
    vote.vote_value                            as vote_value_raw,

    m.ingest_date,
    m.source_file,
    m.loaded_at
from {{ source('bronze', 'matters') }} m
lateral view posexplode(m.actions) a as action_seq, action
lateral view explode(action.votes) v as vote
