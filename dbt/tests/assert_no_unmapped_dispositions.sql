-- TRIPWIRE: no matter should end up with an UNMAPPED disposition or lifecycle.
--
-- 'UNMAPPED' is a legal value in the schema (that's why accepted_values allows it)
-- but it should never actually occur. It means dim_matter saw a raw `status` string
-- that its CASE expression has no branch for, so the matter silently falls outside
-- every real category — invisible in any dashboard that filters on lifecycle.
--
-- This is not hypothetical: the 26-year backfill introduced 13 previously-unseen
-- statuses affecting 249 matters, which is exactly how the mapping got expanded.
-- SF invents new status wording occasionally, so this WILL fire again. When it does,
-- the fix is to classify the new value in dim_matter.sql — not to relax this test.
--
-- A singular test rather than a column test because the point is "the count is zero",
-- which accepted_values cannot express while UNMAPPED remains a legal value.

select
    matter_sk,
    matter_file,
    status,
    final_disposition,
    lifecycle
from {{ ref('dim_matter') }}
where final_disposition = 'UNMAPPED'
   or lifecycle = 'UNMAPPED'
