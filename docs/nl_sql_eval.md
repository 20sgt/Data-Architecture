# Text-to-SQL accuracy — 2026-08-10

How we know the natural-language layer is right, and what measuring it changed.

```bash
python app/eval.py --check-refs                  # validate the fixtures, no API calls
python app/eval.py --run | tee docs/nl_sql_eval.md
python app/eval.py --demo                        # offline comparator self-check
```

## Result

| run | question set | schema given to the planner | score |
|---|---|---|---|
| 1 | 16 questions, loosely worded | names + types | **11/16 = 68%** |
| 2 | same 16, output shape specified | names + types | **16/16 = 100%** |
| 3 | + 6 harder vocabulary/structure questions | names + types | **21/22 = 95%** |
| 4 | same 22 | names + types **+ sampled column values** | **22/22 = 100%** |

Guardrails held 2/2 in every run. Unanswerable questions: **0/2 in every run** — see
below, that is the one that matters.

## What each step actually showed

**Run 1 → 2 was a fixture fix, not a model improvement.** Say this out loud
before quoting any number. All five run-1 failures returned the *correct answer*
in a wider shape — `['Chris Daly', 1095]` was row 1 of 15; `408` was in there
among all three lifecycles. The questions never specified an output shape, so
"which supervisor voted No most" returning a ranked top-15 was a defensible
reading. Rewording all sixteen to state the expected shape ("Return exactly one
row with two columns…") took it to 16/16. That is what a well-posed benchmark
question looks like; the ambiguity was ours.

**Run 3 added headroom.** 16/16 measures nothing further, so six questions were
added that the agent cannot answer without knowing values rather than structure —
`Abstain` (9 rows in 587k), `Non-Voting`, `Vacant`, `terminal_other` — plus an
anti-join and a grouped lookup. It guessed three of the four literals correctly.

**Run 3 → 4 is the real result.** The one remaining failure, h04, asked how many
matters reached a final outcome other than passing. The agent never touched
`lifecycle`. It reconstructed the answer from `final_disposition` with a stack of
`LIKE '%pass%' / '%approv%' / '%adopt%'` patterns and returned 9,405 against a
true 8,997. It was *reasoning* because it had nothing to *look up*.

`gold_schema()` now samples the distinct values of small controlled-vocabulary
columns into the prompt:

```
  -- dim_matter.lifecycle is one of: 'in_progress', 'passed', 'terminal_other'
  -- fact_vote.vote_value is one of: 'Absent', 'Abstain', 'Aye', 'Excused', 'No',
                                     'Non-Voting', 'Present', 'Vacant'
```

13 such vocabularies across gold. Isolated A/B on h04, four runs each:

| schema | h04 |
|---|---|
| names + types | 0/4 pass |
| names + types + values | **4/4 pass** |

**Sampled, not documented.** The Unity Catalog comments are wrong.
`vote_value`'s says *"Aye, No, Absent, Excused, Recused, etc."* — but `Recused`
never occurs, and `Non-Voting`, `Vacant`, `Abstain` and `Present`, which do, are
absent from it. dbt persists those comments faithfully; they simply drifted from
the data. Reading them into the prompt would have taught the model a vocabulary
that does not exist. The values are the values.

That is the concrete answer to *how did schema design enable or constrain the
agent*: the star schema's structure was never the obstacle, its **undocumented
vocabularies** were, and the fix was to stop asking the model to guess them.

## The failure that did not move: fabrication from model priors

Gold has no district column and no party column. `dim_person` is identity-only —
`person_id` and `full_name`.

Both questions failed at the SQL layer in every run: asked which district Connie
Chan represents, the planner returned her vote counts and committee memberships;
asked how many supervisors are Democrats, it counted distinct supervisors. Neither
declined.

End to end, the two diverge, and the divergence is the finding:

> **How many supervisors are Democrats?**
> *"Party registration isn't part of the Board of Supervisors' legislative
> record… San Francisco's supervisor seats are formally nonpartisan."*

> **Which district does Connie Chan represent?**
> *"Connie Chan represents District 1 — the Richmond."*

**That second sentence is not in the data.** No query returned it. The model
supplied it from pretraining, and the transcript search then corroborated it with
genuine episodes about her District 1 seat.

It is also *true*, which is what makes it dangerous. A confidently wrong answer
gets caught. A confidently right one sourced from model memory rather than the
warehouse does not. Every other fact in that answer — 11,509 votes, first vote
2021-01-11 — is warehouse-derived; the district is not; they are presented
identically and the reader cannot tell them apart.

This is a provenance problem, not an accuracy one. No amount of result-set
grading would have found it: it took asking a question the warehouse cannot
answer, and then reading the prose rather than the SQL.

## Two guard bugs the eval surfaced

`guard_sql()` is the trust boundary, and it has two false positives — both reject
legitimate read-only SQL:

1. **Semicolons inside string literals.** It refuses any `;`. 11,919 of 38,724
   matters have one in their name or title, so a question that quotes a matter
   name fails. Seen live, intermittently.
2. **Leading comments.** `^\s*(select|with)` fails on SQL that opens with `--`.
   Caught in run 3: the agent explained in a comment that party affiliation was
   unavailable — good behaviour — and was refused for it.

Neither weakens the guard against actual mutation; both make it reject correct
queries. The fix is to strip comments and to treat `;` as a separator only
outside string literals.

## How the grading works

Reference SQL is hand-written per question and validated separately
(`--check-refs`, no API calls, fails on an empty or >150-row reference — a
reference that silently stops returning rows would turn a regression into a green
test).

The agent's `plan()` output goes through the same `guard_sql()` the app uses,
runs against the same gold, and the two result sets are compared as an
**order-insensitive multiset of normalized rows**: column names ignored, row
order ignored, `Decimal`/`float` unified, dates ISO-normalized, floats rounded to
2dp, shape significant. No LLM in the grader.

Three expectation modes. `match` (22 questions) produces the accuracy number.
`refuse` (2) and `unanswerable` (2) are reported separately — folding a safety
check into an accuracy percentage makes both meaningless.

Only `plan()` is graded, never `answer()`. That is a deliberate scope limit and
also the blind spot: the district fabrication happens in a step this harness
never inspects.

## What 100% does and does not mean

- **The question set is now the ceiling.** 22/22 means the eval has stopped
  discriminating. It needs harder questions to stay useful, not a victory lap.
- **22 graded questions is small.** One question is worth 4.5 points.
- **The planner is sampled**, so re-runs move. h04 was A/B'd four times each way
  precisely because a one-question delta is otherwise indistinguishable from luck.
- **The prose is ungraded**, which is where the only unfixed failure lives.
- **Do not quote 100% unqualified.** The honest sentence is: *"100% on 22
  questions with hand-written reference SQL — and the interesting result is the
  question it still gets wrong."*
