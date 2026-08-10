-- Composite uniqueness for the two bridge tables.
--
-- Neither bridge has a single unique column — their identity is the COMBINATION
-- (matter_sk, person_sk, sponsor_type) and (matter_sk, document_sk). dbt's built-in
-- `unique` test only works on one column, so this is a singular test instead.
--
-- Written by hand rather than pulling in dbt_utils.unique_combination_of_columns:
-- two small checks are not worth adding a package dependency to the project and to
-- every Job run's `dbt deps`. Revisit if we need a third one.
--
-- Why it matters: a duplicate pair here silently multiplies rows in any join, so a
-- matter with a doubled sponsor row would count twice in "bills sponsored by X".

select 'bridge_matter_sponsor' as table_name, cast(matter_sk as string) as k1,
       cast(person_sk as string) as k2, sponsor_type as k3, count(*) as n
from {{ ref('bridge_matter_sponsor') }}
group by matter_sk, person_sk, sponsor_type
having count(*) > 1

union all

select 'bridge_matter_document', cast(matter_sk as string),
       cast(document_sk as string), null, count(*)
from {{ ref('bridge_matter_document') }}
group by matter_sk, document_sk
having count(*) > 1
