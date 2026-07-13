-- Sponsors belonging to each matter's latest scrape.
select s.*
from {{ ref('stg_sponsors') }} s
left semi join {{ ref('int_matters') }} m
    on s.matter_file = m.matter_file
   and s.ingest_date = m.ingest_date
