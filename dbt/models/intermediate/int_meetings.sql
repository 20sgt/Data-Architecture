-- Latest-wins dedup for meetings: one row per meeting_id, newest scrape only.
with ranked as (
    select
        *,
        row_number() over (partition by meeting_id order by ingest_date desc) as _rn
    from {{ ref('stg_meetings') }}
)
select * except (_rn)
from ranked
where _rn = 1
