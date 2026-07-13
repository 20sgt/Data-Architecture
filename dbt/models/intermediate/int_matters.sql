-- Latest-wins dedup: one row per matter, keeping only its most recent scrape.
-- A matter is re-scraped weekly as it moves through the legislative process, so
-- silver holds several versions; gold should reflect only the newest snapshot.
--
-- row_number() numbers a matter's scrapes newest-first (1 = latest); we keep #1.
with ranked as (
    select
        *,
        row_number() over (partition by matter_file order by ingest_date desc) as _rn
    from {{ ref('stg_matters') }}
)
select * except (_rn)
from ranked
where _rn = 1
