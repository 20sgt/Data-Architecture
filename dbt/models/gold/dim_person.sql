-- One row per person. Two populations, two key formulas:
--   voters       — people with a recorded vote, so we have a stable person_id.
--                  Key = sk(person_id).  id_source = 'vote'.
--   sponsor_only — people who only appear as a sponsor name (no person_id).
--                  Key = xxhash64('NAME:' + name).  id_source = 'sponsor_only'.
-- The left-anti-join drops sponsor names that already exist as a voter, so a
-- person who both voted and sponsored is kept once (as the voter row).
--
-- sponsor_only uses xxhash64(concat(...)) directly rather than the sk() macro,
-- because its formula (a 'NAME:' prefix, no '|' separator) differs from sk().

with voters as (
    select distinct
        person_id,
        person_name as full_name
    from {{ ref('int_votes') }}
    where person_id is not null
),

sponsor_only as (
    select distinct s.sponsor_name as full_name
    from {{ ref('int_sponsors') }} s
    left anti join voters v
        on s.sponsor_name = v.full_name
)

select
    {{ sk('person_id') }}   as person_sk,
    person_id,
    full_name,
    'vote'                  as id_source
from voters

union all

select
    xxhash64(concat('NAME:', full_name)) as person_sk,
    cast(null as string)                 as person_id,
    full_name,
    'sponsor_only'                       as id_source
from sponsor_only
