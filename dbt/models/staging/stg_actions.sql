-- One row per (matter, action). Each matter in bronze carries a LIST of actions;
-- we unnest that list so every action becomes its own row.
--
-- `LATERAL VIEW posexplode(m.actions) a AS action_seq, action` means: for each
-- matter, walk its actions list and emit one row per action, giving us
--   action_seq = the item's position in the list (0,1,2,…)  -- must match the
--                notebook so downstream vote facts join correctly
--   action     = the action struct itself (we read its fields as action.<field>)
--
-- posexplode (not the "outer" variant) drops matters whose actions list is
-- null/empty — same as the notebook's F.posexplode.

select
    m.file_number                                         as matter_file,
    action_seq,

    action.date                                           as action_date_raw,
    to_date(action.date, 'M/d/yyyy')                      as action_date,
    action.body                                           as body,
    action.action                                         as action_type,
    action.result                                         as action_result,
    action.history_id                                     as history_id,
    action.history_url                                    as history_url,

    -- votes-per-action count (0 when missing; size(null) would be -1)
    case when action.votes is null then 0 else size(action.votes) end as n_votes,

    m.ingest_date,
    m.source_file,
    m.loaded_at
from {{ source('bronze', 'matters') }} m
lateral view posexplode(m.actions) a as action_seq, action
