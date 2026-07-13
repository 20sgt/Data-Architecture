-- One row per meeting. committee_sk is built from body_name with the SAME sk()
-- formula dim_committee uses, so a meeting joins cleanly to its committee.
select
    {{ sk('meeting_id') }}   as meeting_sk,
    meeting_id,
    {{ sk('body_name') }}    as committee_sk,
    body_name,
    meeting_date,
    meeting_time,
    meeting_subtype,
    agenda_status,
    agenda_url
from {{ ref('int_meetings') }}
