-- One row per (meeting, agenda item). Single-level unnest of the agenda_items
-- list. Note: item_seq lives INSIDE each item (it.item_seq), so we use plain
-- explode and read that field — we don't generate a position with posexplode.
--
-- matter_status is renamed to matter_status_at_meeting to make clear it's the
-- status as printed on that agenda, not the matter's current status.

select
    m.meeting_id,
    to_date(m.meeting_date, 'M/d/yyyy')        as meeting_date,

    item.item_seq                              as item_seq,
    item.matter_file                           as matter_file,
    item.history_id                            as history_id,
    item.agenda_number                         as agenda_number,
    item.matter_name                           as matter_name,
    item.matter_type                           as matter_type,
    item.matter_status                         as matter_status_at_meeting,
    item.title                                 as title,
    item.action_raw                            as action_raw,
    item.action_result                         as action_result,
    item.matter_url                            as matter_url,
    item.history_url                           as history_url,

    m.ingest_date,
    m.source_file,
    m.loaded_at
from {{ source('bronze', 'meetings') }} m
lateral view explode(m.agenda_items) ai as item
