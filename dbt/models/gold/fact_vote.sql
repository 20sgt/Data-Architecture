-- One row per (matter, action, person): the per-member roll call.
--
-- vote_sk = sk(history_id, person_id) — a stable key (same reasoning as
-- fact_matter_action). person_sk = sk(person_id), the SAME formula dim_person
-- uses for voters, so a vote joins to its person. committee_sk comes from the
-- action's body, looked up by (matter_file, action_seq) — the join key silver
-- was careful to reproduce exactly.
with body as (
    select matter_file, action_seq, body
    from {{ ref('int_actions') }}
),

rows as (
    select
        {{ sk('v.history_id', 'v.person_id') }} as vote_sk,
        {{ sk('v.matter_file') }}               as matter_sk,
        {{ sk('v.person_id') }}                 as person_sk,
        {{ sk('b.body') }}                      as committee_sk,
        h.meeting_sk,                            -- NULL when the vote wasn't in a known meeting
        v.action_date    as vote_date,
        v.vote_value_raw as vote_value,
        v.history_id
    from {{ ref('int_votes') }} v
    left join body b
        on v.matter_file = b.matter_file
       and v.action_seq  = b.action_seq
    left join {{ ref('int_history_to_meeting') }} h
        on v.history_id = h.history_id
)

select *
from rows
qualify row_number() over (partition by vote_sk order by vote_date) = 1
