-- One row per meeting. Like stg_matters: no unnesting, just pick/rename columns,
-- parse the date, and count the nested lists.

select
    meeting_id,
    event_guid,
    body_name,

    meeting_date                                   as meeting_date_raw,
    to_date(meeting_date, 'M/d/yyyy')              as meeting_date,

    meeting_time,
    location,
    meeting_subtype,
    agenda_status,
    minutes_status,
    agenda_url,
    minutes_url,
    video_clip_id,

    case when agenda_items is null then 0 else size(agenda_items) end as n_agenda_items,
    case when documents    is null then 0 else size(documents)    end as n_documents,

    ingest_date,
    source_file,
    loaded_at
from {{ source('bronze', 'meetings') }}
