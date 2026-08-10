-- Lookup: which meeting did a given history_id occur in?
-- history_id is the stable link shared between a matter's action/vote and the
-- meeting agenda item. A handful of history_ids appear under more than one
-- meeting_id; we collapse to one row per history_id by taking min(meeting_id)
-- (the notebook took an arbitrary first — min just makes it deterministic).
select
    history_id,
    {{ sk('meeting_id') }} as meeting_sk
from (
    select history_id, min(meeting_id) as meeting_id
    from {{ ref('int_agenda_items') }}
    where history_id is not null
    group by history_id
)
