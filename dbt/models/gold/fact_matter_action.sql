-- One row per (matter, action).
--
-- matter_action_sk is a STABLE key: history_id when the action has one, else a
-- fallback built from matter_file + action_date + action_type. Stable = it does
-- NOT depend on the array position (which shifts when a matter is re-scraped), so
-- the same action keeps the same key run to run — essential for safe re-runs.
-- This differs from the sk() macro (single hashed value, no '|' wrapper), so it's
-- spelled out here.
with actions as (
    select
        a.*,
        xxhash64(coalesce(
            a.history_id,
            concat_ws('|', a.matter_file, cast(a.action_date as string), a.action_type)
        )) as matter_action_sk
    from {{ ref('int_actions') }} a
),

rows as (
    select
        ac.matter_action_sk,
        {{ sk('ac.matter_file') }} as matter_sk,
        {{ sk('ac.body') }}        as committee_sk,
        h.meeting_sk,                              -- NULL when the action wasn't in a meeting
        ac.action_type   as action_type_code,
        ac.action_date,
        ac.action_result,
        ac.history_id
    from actions ac
    left join {{ ref('int_history_to_meeting') }} h
        on ac.history_id = h.history_id
)

select *
from rows
-- keep one row per key (matches the notebook's dropDuplicates)
qualify row_number() over (partition by matter_action_sk order by action_date) = 1
