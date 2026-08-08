-- The serving view must stay exactly one row per vote.
--
-- member_vote_record LEFT joins four dimensions onto fact_vote. That is safe only
-- while every dimension is unique on its join key. If one ever gains a duplicate —
-- say two dim_person rows for the same person_sk — the view quietly fans out and
-- every dashboard number inflates, with no error anywhere.
--
-- The column-level `unique` tests on each dimension should catch that first. This is
-- the backstop that checks the actual consequence rather than its cause, because the
-- view is what the dashboard reads.

with counts as (
    select
        (select count(*) from {{ ref('member_vote_record') }}) as view_rows,
        (select count(*) from {{ ref('fact_vote') }})          as fact_rows
)

select view_rows, fact_rows, view_rows - fact_rows as difference
from counts
where view_rows != fact_rows
