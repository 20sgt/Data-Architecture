-- One row per acting body (committees, the full Board, President, Mayor, Clerk).
-- Committee names show up in three places, so we pool them: the body that took
-- an action, the body that held a meeting, and the body currently controlling a
-- matter. UNION (not UNION ALL) de-duplicates across the three sources.
with names as (
    select body       as committee_name from {{ ref('int_actions') }}
    union
    select body_name  as committee_name from {{ ref('int_meetings') }}
    union
    select in_control as committee_name from {{ ref('int_matters') }}
)
select
    {{ sk('committee_name') }} as committee_sk,
    committee_name
from names
where committee_name is not null
