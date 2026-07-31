-- One row per matter, as an accumulating snapshot (status + milestone dates,
-- updated in place as a bill progresses).
--
-- final_disposition maps the free-text `status` into a small controlled set.
-- Anything not in the known lists lands as 'UNMAPPED' on purpose — a tripwire so
-- new/unexpected statuses get noticed and added, rather than silently mislabeled.
-- lifecycle is a coarser rollup of final_disposition.

with first_cmte as (
    -- earliest committee action per matter -> first_committee_date milestone
    select
        matter_file,
        min(action_date) as first_committee_date
    from {{ ref('int_actions') }}
    where lower(body) like '%committee%'
    group by matter_file
),

base as (
    select
        m.*,
        regexp_extract(m.detail_url, '[?&]ID=(\\d+)', 1) as matter_id,
        case
            when m.status is null then 'in_progress'
            when lower(trim(m.status)) in (
                'passed', 'approved', 'adopted', 'finally passed',
                'ordinance enacted', 'mayor approved', 'final passage, consent'
            ) then 'passed'
            when lower(trim(m.status)) in ('filed', 'discussed and filed', 'completed') then 'filed'
            when lower(trim(m.status)) = 'killed' then 'killed'
            -- distinct terminal-not-passed outcomes (kept separate for dashboard detail):
            when lower(trim(m.status)) in ('failed', 'disapproved') then 'failed'
            when lower(trim(m.status)) = 'vetoed'    then 'vetoed'
            when lower(trim(m.status)) = 'withdrawn' then 'withdrawn'
            when lower(trim(m.status)) in (
                '30 day rule', 'consent agenda', 'first reading', 'first reading, consent',
                'mayors office', 'new business', 'pending committee action',
                'scheduled for committee hearing', 'unfinished business-final passage',
                'pending board action', 'assigned', 'continued', 'special order', 'in committee',
                -- added from the 26-year backfill:
                'unfinished business', 'unfinished business-first reading', 'introduced', 'heard',
                -- JUDGMENT CALLS (low volume; revisit if a domain owner disagrees):
                --   litigation-attorney -> treated as still-active (with City Attorney)
                --   for immediate adoption -> queued to adopt, not yet adopted
                'litigation-attorney', 'for immediate adoption'
            ) then 'in_progress'
            else 'UNMAPPED'
        end as final_disposition
    from {{ ref('int_matters') }} m
)

select
    {{ sk('b.matter_file') }} as matter_sk,
    b.matter_file,
    b.matter_id,
    b.name                    as matter_name,
    b.type                    as matter_type,
    b.title                   as matter_title,
    b.in_control,
    b.status,
    case
        when b.final_disposition = 'passed' then 'passed'
        when b.final_disposition in ('filed', 'killed', 'failed', 'vetoed', 'withdrawn')
            then 'terminal_other'
        when b.final_disposition = 'in_progress' then 'in_progress'
        else 'UNMAPPED'
    end as lifecycle,
    b.final_disposition,
    b.introduced_date,
    fc.first_committee_date,
    b.final_action_date,
    b.enactment_date,
    b.enactment_number
from base b
left join first_cmte fc on b.matter_file = fc.matter_file
