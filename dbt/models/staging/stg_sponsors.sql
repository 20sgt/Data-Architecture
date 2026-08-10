-- One row per (matter, sponsor). The sponsors list holds plain name STRINGS
-- (not structs), so posexplode hands back the name directly as sponsor_name.
--
-- sponsor_pos is the position in the list: gold treats pos 0 as the primary
-- sponsor and the rest as co-sponsors, so we preserve it exactly.

select
    m.file_number      as matter_file,
    sponsor_pos,
    sponsor_name,
    m.ingest_date,
    m.source_file,
    m.loaded_at
from {{ source('bronze', 'matters') }} m
lateral view posexplode(m.sponsors) s as sponsor_pos, sponsor_name
