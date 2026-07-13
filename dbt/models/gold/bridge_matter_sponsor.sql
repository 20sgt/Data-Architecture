-- Links each matter to its sponsors, tagging the primary sponsor (list position
-- 0) vs co-sponsors. person_sk is looked up from dim_person by name, so it lands
-- on the same key whether the sponsor is a voter or a sponsor-only person.
with name2sk as (
    select distinct
        full_name as sponsor_name,
        person_sk
    from {{ ref('dim_person') }}
),

rows as (
    select
        {{ sk('s.matter_file') }} as matter_sk,
        n.person_sk,
        case when s.sponsor_pos = 0 then 'primary' else 'co' end as sponsor_type
    from {{ ref('int_sponsors') }} s
    left join name2sk n
        on s.sponsor_name = n.sponsor_name
)

select *
from rows
-- one row per (matter, person); matches the notebook's dropDuplicates
qualify row_number() over (partition by matter_sk, person_sk order by sponsor_type) = 1
