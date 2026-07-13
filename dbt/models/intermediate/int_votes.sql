-- Votes belonging to each matter's latest scrape (see int_actions for the
-- left-semi-join pattern).
select v.*
from {{ ref('stg_votes') }} v
left semi join {{ ref('int_matters') }} m
    on v.matter_file = m.matter_file
   and v.ingest_date = m.ingest_date
