-- One row per matter (bills, resolutions, etc.). This is the simplest staging
-- model: no list-flattening, just picking columns, renaming a few, and parsing
-- the date strings. It mirrors the old notebook's `stg_matters` exactly so we
-- can diff the two.
--
-- The scraped dates look like "1/9/2024", so we parse them with the format
-- 'M/d/yyyy'. We keep the raw string too (…_raw) in case a date fails to parse.

select
    file_number                                as matter_file,
    detail_url,
    name,
    title,
    type,
    status,

    introduced                                 as introduced_raw,
    to_date(introduced, 'M/d/yyyy')            as introduced_date,
    on_agenda                                  as on_agenda_raw,
    to_date(on_agenda, 'M/d/yyyy')             as on_agenda_date,
    final_action                               as final_action_raw,
    to_date(final_action, 'M/d/yyyy')          as final_action_date,
    enactment_date                             as enactment_date_raw,
    to_date(enactment_date, 'M/d/yyyy')        as enactment_date,

    enactment_number,
    in_control,
    related_files,

    -- counts of the nested lists (0 when the list is missing, matching the
    -- notebook's coalesce-to-empty behavior; plain size(null) would give -1)
    case when actions  is null then 0 else size(actions)  end as n_actions,
    case when sponsors is null then 0 else size(sponsors) end as n_sponsors,

    -- lineage (carried straight through from bronze)
    ingest_date,
    source_file,
    loaded_at
from {{ source('bronze', 'matters') }}
