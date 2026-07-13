-- Agenda items belonging to each meeting's latest scrape.
select ai.*
from {{ ref('stg_agenda_items') }} ai
left semi join {{ ref('int_meetings') }} m
    on ai.meeting_id = m.meeting_id
   and ai.ingest_date = m.ingest_date
