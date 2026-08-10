-- Keep only the actions that belong to each matter's latest scrape.
--
-- LEFT SEMI JOIN = "keep rows of the left table that HAVE a match on the right,"
-- without adding the right table's columns and without duplicating rows. Here it
-- filters stg_actions down to the (matter_file, ingest_date) pairs that survived
-- the latest-wins dedup in int_matters.
select a.*
from {{ ref('stg_actions') }} a
left semi join {{ ref('int_matters') }} m
    on a.matter_file = m.matter_file
   and a.ingest_date = m.ingest_date
