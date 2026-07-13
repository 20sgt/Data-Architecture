-- Attachments belonging to each matter's latest scrape.
select x.*
from {{ ref('stg_attachments') }} x
left semi join {{ ref('int_matters') }} m
    on x.matter_file = m.matter_file
   and x.ingest_date = m.ingest_date
